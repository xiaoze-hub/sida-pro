"""腾讯五档盘口快照采样 + 托单/压单识别(算法4, 2026-08-14 新建, 未部署)。

背景
----
主力意图分析需要识别"托单诱多 / 压单吸筹"(主力挂大单不动, 制造假象)。
没有 L2 逐笔委托数据, 采用腾讯免费五档行情接口做间隔采样近似:
- 间隔采样(默认同 code 两次采样 >=30s), 每只股票在内存中保留最近 5 次采样;
- 对比"首次采样"与"最新采样"的买一/卖一挂单量与价格变化, 识别操纵迹象。

腾讯五档接口
------------
GET https://qt.gtimg.cn/q=v_sh600519   (前缀 v_ + 腾讯代码, 如 sh600519 / sz002361)
注: 部分网络出口(如海外节点)对 v_ 前缀形式会返回 v_pv_none_match="1" 拦截,
    模块内置降级: 主请求失败/被拦截时自动改用 https://qt.gtimg.cn/q=sh600519(无 v_ 前缀)。
响应 GBK 编码, 形如 v_sh600519="1~贵州茅台~600519~..."; 以 '~' 分隔的字段数组:
  parts[1]  = 名称
  parts[3]  = 现价
  parts[4]  = 昨收
  parts[5]  = 今开
  parts[9]  = 买一价   parts[10] = 买一量(手)
  parts[11] = 买二价   parts[12] = 买二量(手)
  parts[13] = 买三价   parts[14] = 买三量(手)
  parts[15] = 买四价   parts[16] = 买四量(手)
  parts[17] = 买五价   parts[18] = 买五量(手)
  parts[19] = 卖一价   parts[20] = 卖一量(手)
  parts[21] = 卖二价   parts[22] = 卖二量(手)
  parts[23] = 卖三价   parts[24] = 卖三量(手)
  parts[25] = 卖四价   parts[26] = 卖四量(手)
  parts[27] = 卖五价   parts[28] = 卖五量(手)

判定规则(算法4, 以该 code 首次采样为基准)
------------------------------------------
- 托单诱多: 最新买一量 >= 首次买一量 * _BOARD_VOL_GROWTH(1.5) 且
            价格涨幅 < _BOARD_PRICE_MOVE(0.5%)  → 挂单增大但价格滞涨
- 压单吸筹: 最新卖一量 >= 首次卖一量 * _BOARD_VOL_GROWTH(1.5) 且
            价格跌幅 < _BOARD_PRICE_MOVE(0.5%)  → 压单增大但价格抗跌

注: 首次挂单量为 0 时增长倍数无意义, 不触发(挂单量需 >0 才有比较基础)。
"""
from __future__ import annotations

import logging
import re
import time
import urllib.request

logger = logging.getLogger(__name__)

# ---- 接口与请求参数 ----
_QUOTE_URL = "https://qt.gtimg.cn/q=v_{code}"  # 主请求: v_ 前缀形式
_QUOTE_URL_FALLBACK = "https://qt.gtimg.cn/q={code}"  # 降级: 无 v_ 前缀(海外节点可用)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}
_TIMEOUT = 8  # 秒

# ---- 算法阈值(算法4) ----
_BOARD_VOL_GROWTH = 1.5  # 挂单量增长倍数阈值: 最新 >= 首次 * 1.5
_BOARD_PRICE_MOVE = 0.5  # 价格变动阈值(%): 托单看涨幅 <0.5%, 压单看跌幅 <0.5%

# ---- 采样控制 ----
_SAMPLE_INTERVAL = 30.0  # 同 code 两次采样的最小间隔(秒)
_MAX_SAMPLES = 5         # 每 code 内存中保留的最大采样条数

# 模块级采样缓存: {code: [sample_dict, ...]} 按时间先后排列, 最新在末尾
_CACHE: dict[str, list[dict]] = {}


def _tencent_code(code: str) -> str | None:
    """把 6 位数字代码或已带前缀代码统一为腾讯代码(如 sh600519)。"""
    c = (code or "").strip().lower()
    if re.fullmatch(r"(sh|sz|bj)\d{6}", c):
        return c
    if re.fullmatch(r"\d{6}", c):
        if c[0] in ("6", "9") or c.startswith("688"):
            return f"sh{c}"
        if c[0] in ("0", "2", "3"):
            return f"sz{c}"
        return f"bj{c}"  # 北交所 4/8 开头
    logger.warning("[board_snapshot] 无法识别的代码: %s", code)
    return None


def _to_float(v) -> float:
    """安全转 float: 空串/非法值返回 0.0。"""
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return 0.0


def _fetch_board(code: str) -> dict | None:
    """拉取腾讯五档盘口, 返回快照 dict; 失败返回 None(日志 warning)。

    返回字段:
      price       现价
      buy1_price  买一价   buy1_vol  买一量(手)
      sell1_price 卖一价   sell1_vol 卖一量(手)
      ts          采样时间戳(time.time())
    """
    tcode = _tencent_code(code)
    if not tcode:
        return None
    # 主请求用 v_ 前缀形式; 若被拦截(v_pv_none_match)或字段不足, 降级用无前缀形式重试一次
    for url in (_QUOTE_URL.format(code=tcode), _QUOTE_URL_FALLBACK.format(code=tcode)):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = resp.read().decode("gbk", "replace")
            m = re.search(r'"([^"]+)"', body)
            if not m:
                logger.warning("[board_snapshot] %s 响应无有效数据: %s", tcode, url)
                continue
            parts = m.group(1).split("~")
            if len(parts) < 29 or "none_match" in body:
                # 被拦截或字段不足 -> 尝试下一个 URL
                continue
            return {
                "price": _to_float(parts[3]),
                "buy1_price": _to_float(parts[9]),
                "buy1_vol": _to_float(parts[10]),
                "sell1_price": _to_float(parts[19]),
                "sell1_vol": _to_float(parts[20]),
                "ts": time.time(),
            }
        except Exception as e:  # noqa: BLE001 网络/解析异常统一降级
            logger.warning("[board_snapshot] %s 五档拉取失败(%s): %s", tcode, url, e)
    logger.warning("[board_snapshot] %s 主/降级接口均无有效五档数据", tcode)
    return None


def sample_board(code: str) -> dict | None:
    """带间隔控制的采样入口。

    - 同 code 距上次采样 < _SAMPLE_INTERVAL(30s): 直接返回上次采样结果, 不重复请求;
    - 否则调用 _fetch_board 拉新快照并写入缓存(每 code 最多保留 _MAX_SAMPLES 条, 淘汰最旧);
    - 拉取失败返回 None。
    """
    tcode = _tencent_code(code)
    if not tcode:
        return None
    now = time.time()
    samples = _CACHE.get(tcode)
    if samples:
        last = samples[-1]
        if now - last.get("ts", 0.0) < _SAMPLE_INTERVAL:
            return last  # 间隔内直接复用上次结果
    snap = _fetch_board(tcode)
    if snap is None:
        return None
    samples = _CACHE.setdefault(tcode, [])
    samples.append(snap)
    if len(samples) > _MAX_SAMPLES:
        del samples[: len(samples) - _MAX_SAMPLES]  # 丢弃最旧, 保持最多 _MAX_SAMPLES 条
    return snap


def get_board_manipulation(code: str) -> dict | None:
    """核心判定(算法4): 根据缓存采样识别 托单诱多 / 压单吸筹。

    需要该 code 至少 2 次采样(不足返回 None, 表示采样中)。
    以"首次采样"为基准对比"最新采样":
      - 托单诱多: 最新买一量 >= 首次买一量 * _BOARD_VOL_GROWTH 且
                  现价涨幅 < _BOARD_PRICE_MOVE% (挂单增大但价格滞涨)
      - 压单吸筹: 最新卖一量 >= 首次卖一量 * _BOARD_VOL_GROWTH 且
                  现价跌幅 < _BOARD_PRICE_MOVE% (压单增大但价格抗跌)
    返回示例:
      {'type': '托单诱多', 'buy1_vol': 最新, 'buy1_vol0': 首次,
       'price_pct': 涨跌幅%, 'detail': '买一挂单持续增大但价格滞涨, 托单诱多嫌疑'}
      {'type': '压单吸筹', ...}
      {'type': None, 'detail': '盘口无明显挂单操纵迹象'}
      采样不足 2 次或代码无效时返回 None。
    """
    tcode = _tencent_code(code)
    if not tcode:
        return None
    samples = _CACHE.get(tcode) or []
    if len(samples) < 2:
        return None  # 采样中, 样本不足(至少需要 2 次采样)
    first, latest = samples[0], samples[-1]

    price0 = first.get("price") or 0.0
    price = latest.get("price") or 0.0
    pct = (price - price0) / price0 * 100.0 if price0 else 0.0

    # 托单诱多: 买一挂单量放大 1.5 倍以上, 但价格涨幅 < 0.5%(滞涨)
    buy1_vol0 = first.get("buy1_vol") or 0.0
    buy1_vol = latest.get("buy1_vol") or 0.0
    if buy1_vol0 > 0 and buy1_vol >= buy1_vol0 * _BOARD_VOL_GROWTH and pct < _BOARD_PRICE_MOVE:
        return {
            "type": "托单诱多",
            "buy1_vol": buy1_vol,
            "buy1_vol0": buy1_vol0,
            "price_pct": round(pct, 2),
            "detail": "买一挂单持续增大但价格滞涨, 托单诱多嫌疑",
        }

    # 压单吸筹: 卖一挂单量放大 1.5 倍以上, 但价格跌幅 < 0.5%(抗跌)
    sell1_vol0 = first.get("sell1_vol") or 0.0
    sell1_vol = latest.get("sell1_vol") or 0.0
    if sell1_vol0 > 0 and sell1_vol >= sell1_vol0 * _BOARD_VOL_GROWTH and pct > -_BOARD_PRICE_MOVE:
        return {
            "type": "压单吸筹",
            "sell1_vol": sell1_vol,
            "sell1_vol0": sell1_vol0,
            "price_pct": round(pct, 2),
            "detail": "卖一压单增大但价格抗跌, 压单吸筹嫌疑",
        }

    return {"type": None, "detail": "盘口无明显挂单操纵迹象"}
