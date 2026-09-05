"""大宗商品期货主力连续行情(批次C C5, 2026-09-06 28号)。

数据源(公开接口, 无需账户):
- 新浪期货: hq.sinajs.cn/list=nf_SC0,nf_AU0,... (需 Referer, GBK 编码)
- 历史日K: stock2.finance.sina.com.cn InnerFuturesNewService.getDailyKLine (动量用)

诚实标注(周一盘中实测清单):
- 新浪 nf_ 行情字段位置为社区已知口径, **未在本项目实测过**; 解析失败整源
  降级 available=False, 调用方(commodity_rotation)回落事件文本版——绝不用
  可疑字段编造轮动信号。
- 动力煤(ZC)流动性枯竭已停用, "煤"由煤炭板块资金流+现货指数替代(事件流)。

仅输出最可靠的三元组: 最新价/昨结算/当日涨跌幅; 20/60 日动量走日K收盘。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# 四幕商品池(主力连续合约代码: 新浪 nf_ 前缀)
FUTURES_POOL = {
    # 能源
    "SC0": ("原油", "energy"),
    # 金属
    "CU0": ("铜", "metal"),
    "AL0": ("铝", "metal"),
    "RB0": ("螺纹钢", "metal"),
    # 农产
    "M0": ("豆粕", "agri"),
    "CF0": ("棉花", "agri"),
    "C0": ("玉米", "agri"),
    # 贵金属(并行风险温度计, 不参与四幕轮动排队)
    "AU0": ("黄金", "gold"),
    "AG0": ("白银", "gold"),
}

_SINA_URL = "https://hq.sinajs.cn/list={}"
_SINA_REFERER = "https://finance.sina.com.cn"
_KLINE_URL = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/InnerFuturesNewService.getDailyKLine?symbol={}"


def _parse_sina_nf(payload: str) -> dict[str, dict]:
    """新浪 nf_ 响应 → {code: {last, prev_settle, chg_pct}}。

    字段口径(社区已知, 待周一实测): 0名称,1开,2高,3低,4?,5买,6卖,
    7最新价,8当日结算,9昨结算,10买量,11卖量,12持仓,13名称,14时间。
    取 最新[7] / 昨结算[9]; 任一非正跳过该合约(诚实降级)。
    """
    out: dict[str, dict] = {}
    for m in re.finditer(r'hq_str_nf_(\w+)="([^"]*)"', payload):
        code, body = m.group(1), m.group(2)
        parts = body.split(",")
        if len(parts) < 15:
            continue
        name = parts[0] or FUTURES_POOL.get(code, ("?", ""))[0]

        def _f(idx: int) -> float | None:
            try:
                v = float(parts[idx])
                return v if v > 0 else None
            except (IndexError, TypeError, ValueError):
                return None

        last = _f(7)
        prev_settle = _f(9)
        if last is None or prev_settle is None or prev_settle <= 0:
            continue
        chg_pct = (last - prev_settle) / prev_settle * 100
        out[code] = {"code": code, "name": name, "last": last, "prev_settle": prev_settle, "chg_pct": round(chg_pct, 2)}
    return out


def fetch_snapshot(codes: list[str] | None = None, timeout: float = 8.0) -> dict:
    """期货主力当日快照。失败 → available=False + reason(调用方降级事件版)。"""
    codes = codes or list(FUTURES_POOL.keys())
    try:
        import httpx

        url = _SINA_URL.format(",".join(f"nf_{c}" for c in codes))
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers={"Referer": _SINA_REFERER}, timeout=timeout)
            resp.raise_for_status()
            payload = resp.content.decode("gbk", errors="replace")
        items = _parse_sina_nf(payload)
        if not items:
            return {"available": False, "reason": "新浪期货响应无可解析合约(字段口径待周一实测校准)", "items": []}
        return {"available": True, "reason": None, "items": [items[c] for c in items if c in FUTURES_POOL]}
    except Exception as e:  # noqa: BLE001
        logger.warning("期货快照获取失败: %s", e)
        return {"available": False, "reason": f"新浪期货接口失败: {e}", "items": []}


def fetch_daily_close(code: str, days: int = 70, timeout: float = 10.0) -> list[float]:
    """主力连续日K收盘序列(动量用, 升序)。失败返回空(调用方降级)。"""
    try:
        import httpx

        url = _KLINE_URL.format(code)
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers={"Referer": _SINA_REFERER}, timeout=timeout)
            resp.raise_for_status()
            payload = resp.content.decode("utf-8", errors="replace")
        m = re.search(r"\((\[.*\])\)", payload, re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
        closes = []
        for row in data[-days:]:
            try:
                c = float(row.get("close"))
                if c > 0:
                    closes.append(c)
            except (TypeError, ValueError, KeyError):
                continue
        return closes
    except Exception as e:  # noqa: BLE001
        logger.warning("期货日K %s 获取失败: %s", code, e)
        return []


def momentum_score(closes: list[float], window: int = 20) -> float | None:
    """N 日动量(%)=(最新/前收窗口起点 - 1)*100。样本不足 → None。"""
    if len(closes) < window + 1 or closes[-window - 1] <= 0:
        return None
    return round((closes[-1] / closes[-window - 1] - 1) * 100, 2)
