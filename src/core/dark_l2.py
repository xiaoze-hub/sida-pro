# -*- coding: utf-8 -*-
"""
L2 逐笔数据源(thsdk 同花顺 + 通达信 .tck, 双实现)
========================================

把 dark_flow.py 预留的 `fetch_l2_ticks(code, source)` 占位替换为真实实现:
- `thsdk`   : 同花顺 thsdk 游客模式 tick_super_level1(约 3 秒累计条), 差分还原
- `tdx_tck` : 通达信 .tck 超盘回放落盘(36字节委托号级, 官方方向 2B/2S, 2026-08-31 接入)

返回与腾讯逐笔**同构**的列表:

    [{"d": "B"/"S"/"M", "amt": 金额(元), "vol": 手数, "price": 价格, "t": "HH:MM:SS"}]

接入方式: 设置环境变量 `PANWATCH_DARK_SOURCE=thsdk`(或 tdx_tck) 后 dark_flow 自动分发。
本模块惰性加载 thsdk/解析器: 主进程导入不报错, dark_flow 捕获异常后自动回退腾讯逐笔。

**thsdk 实测口径(2026-08-19 国内服务器 101.35.244.238, thsdk 1.7.18 游客模式)**:
- 总金额单位是【元】、成交量单位是【股】(非旧文档记录的"厘/手")
  - 神剑股份 集合竞价匹配行: 5,357,500 股 × 12.53 = 67,129,475 元(精确)
  - 贵州茅台 集合竞价匹配行: 31,500 股 × 1300 = 40,950,000 元(精确)
- tick_super_level1 按约 3 秒条返回, 成交量/总金额/交易笔数 均为**当日累计**口径,
  逐笔(区间增量) = 相邻行差分; 尾行可能带有非 epoch 的日期标记行(全字段 None), 需过滤
- 时间字段为 epoch 秒(如 1787102700 → 2026-08-19 09:15:00)
- 成交方向取值 0/1/5/15/17/21/4294967295(4294967295 = 无效, 且常伴随成交量=0 的竞价快照行)
- 方向判断按任务规范用 委托买入价/委托卖出价:
  价格 >= 委托卖出价 → "B"(主动买); 价格 <= 委托买入价 → "S"(主动卖); 其余 → "M"(中性)

**限频/重试**: 复用 data_source.thsdk_l2.THSDKL2 的 `_query` 包装
(游客 50ms 限频 + 3 次指数退避重试 + 60s 熔断), 每次调用之间 sleep 50ms。

**.tck 口径(2026-08-30 实测, 见 tdx-client-l2-extraction skill)**:
- 官方方向 2B主买/2S主卖是交易所级标记, 比腾讯/thsdk 自解析方向准。
- 仅主动侧委托号(被动 maker 未落盘, 委托量==成交量 1:1)。
- 盘后数据(超盘回放落盘 zst_cache), 非盘中实时。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

# thsdk 无效方向标记(uint32 上限)
THS_INVALID_DIR = 4294967295

# 非 epoch 时间标记下限: 真实 epoch 秒约 1.7e9(2023-12 起), 8 位整数的纯日期标记(如 20260819)必然小于 1.6e9
EPOCH_FLOOR = 1_600_000_000

# 腾讯代码前缀 → thsdk 前缀映射
_PREFIX_MAP = {
    "sz": "USZA",  # 深 A
    "sh": "USHA",  # 沪 A
    "bj": "USTM",  # 北交所
    "hk": "UHKG",  # 港股
    "us": "UNQQ",  # 美股
}


def _ths_code(code: str) -> str:
    """把腾讯风格代码(sz002361/sh600519)转成 thsdk 代码(USZA002361/USHA600519)。

    - 已带 thsdk 前缀(USZA/USHA/USZM/USTM/UHKG/UNQQ...)的直接透传
    - 六位纯数字按 A 股习惯推断: 6/9 开头 → 沪A(USHA), 0/2/3/4/8 开头 → 深A(USZA)
    - 带 sz/sh/bj/hk/us 小写前缀的按映射转换
    """
    if code is None:
        raise ValueError("code 不能为空")
    code = code.strip()
    if code.upper()[:4] in (
        "USZA", "USHA", "USZM", "USTM", "UHKG", "UHKM", "UNQQ", "USHI", "USZI", "UFXB",
    ):
        return code.upper()
    if code[:2].lower() in _PREFIX_MAP:
        return _PREFIX_MAP[code[:2].lower()] + code[2:]
    if code.isdigit() and len(code) == 6:
        return ("USHA" if code[0] in ("6", "9") else "USZA") + code
    raise ValueError(f"无法解析股票代码: {code!r}(支持 sz002361 / sh600519 / USZA002361 等)")


def _ts_to_hms(epoch_sec: int) -> str:
    """epoch 秒 → 'HH:MM:SS'。"""
    if not isinstance(epoch_sec, int) or epoch_sec < EPOCH_FLOOR:
        raise ValueError(f"非法时间字段: {epoch_sec!r}(应为 epoch 秒)")
    return time.strftime("%H:%M:%S", time.localtime(epoch_sec))


def _infer_direction(price: float, bid: float, ask: float, raw_dir: Optional[int]) -> str:
    """按 委托买入价/委托卖出价 判断逐笔方向(任务规范)。

    优先级:
      1. 委托买卖价可用(>0 且非 4294967295)时:
         价格 >= 委托卖出价 → "B"; 价格 <= 委托买入价 → "S"; 其余 → "M"
      2. 档位缺失(如尾盘委托买价为 0)回退 thsdk 方向编码: 1=主买, 5=主卖, 其余中性
    """
    def _valid(v):
        return v is not None and v > 0 and v != THS_INVALID_DIR

    bid_ok, ask_ok = _valid(bid), _valid(ask)
    if bid_ok or ask_ok:
        if ask_ok and price >= ask:
            return "B"
        if bid_ok and price <= bid:
            return "S"
        return "M"
    # 档位全部缺失: 回退方向编码
    if raw_dir == 1:
        return "B"
    if raw_dir == 5:
        return "S"
    return "M"


def _fetch_raw_rows(code: str) -> list[dict]:
    """惰性拉取 thsdk tick_super_level1 原始 dict 行(带限频/重试/熔断)。"""
    try:
        from data_source.thsdk_l2 import THSDKL2
        l2 = THSDKL2()
        raw = l2._query("tick_super_level1", code)
    except ImportError:
        # 仓库主环境无 pandas/thsdk 时降级为裸 THS 上下文(仅限频)
        import time as _t
        from thsdk import THS
        config = {}
        for k in ("THS_USERNAME", "THS_PASSWORD", "THS_MAC"):
            import os
            if os.environ.get(k):
                config[k.lower() if k != "THS_MAC" else "mac"] = os.environ.get(k)
        _t.sleep(0.05)
        for attempt in range(3):
            try:
                with THS(config) if config else THS() as ths:
                    raw = ths.tick_super_level1(code)
                break
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"thsdk.tick_super_level1 失败 3 次: {str(e)[:80]}") from e
                _t.sleep(1.0 * (2 ** attempt))
    return [r for r in getattr(raw, "data", None) or [] if isinstance(r, dict)]


def _fetch_thsdk(code: str) -> list[dict]:
    """从 thsdk 拉取全天逐笔, 返回与腾讯逐笔同构的列表。

    处理要点:
        - 过滤 成交方向=4294967295 和 成交量=0 的无效行(集合竞价快照行)
        - 过滤尾部的日期标记行(时间非 epoch 秒 / 全字段 None)
        - 成交量/总金额为日累计 → 相邻行差分还原为区间增量(股→手 ÷100)
        - 方向用 委托买入价/卖出价 判断(价格>=卖价=买, <=买价=卖, 其余中性)

    实测(2026-08-19 收盘后): 神剑股份 USZA002361 原始 4899 行 → 差分后 4830 条,
    方向有效(B+S)占比约 98.6%, 全部金额 > 0。
    """
    ths_code = _ths_code(code)

    # 1) 拉取原始行并按时间升序
    rows = _fetch_raw_rows(ths_code)
    if not rows:
        raise RuntimeError(f"thsdk.tick_super_level1 返回空数据({ths_code})")

    # 2) 过滤无效行
    valid_rows = []
    for r in rows:
        ts = r.get("时间")
        # 丢弃日期标记行(20260819 这类 8 位整数, 非 epoch)和缺字段行
        if not isinstance(ts, int) or ts < EPOCH_FLOOR:
            continue
        vol, amt, price = r.get("成交量"), r.get("总金额"), r.get("价格")
        if vol is None or amt is None or price is None:
            continue
        if vol == 0 or amt == 0:          # 竞价快照等无成交行
            continue
        if r.get("成交方向") is None or r.get("成交方向") == THS_INVALID_DIR:
            continue
        valid_rows.append(r)
    valid_rows.sort(key=lambda r: r["时间"])

    # 3) 日累计 → 区间增量差分, 组装同构 tick
    ticks: list[dict] = []
    prev_vol, prev_amt = 0.0, 0.0
    for r in valid_rows:
        vol, amt = float(r["成交量"]), float(r["总金额"])
        d_vol = vol - prev_vol   # 股
        d_amt = amt - prev_amt   # 元
        prev_vol, prev_amt = vol, amt
        if d_vol <= 0 and d_amt <= 0:
            continue  # 该采样条无新成交(数据重复/无增量)
        price = float(r["价格"])
        # 竞价时段保护(对齐 dark_flow 2026-08-11 教训): 9:15-9:30 集合竞价撮合
        # 无主动买卖方向, 委托买卖价被撮合价覆盖, 直接判定会污染成 B(主动买)。
        ts = r["时间"]
        hm = _ts_to_hms(ts)
        if "09:15" <= hm < "09:30":
            d = "M"
        else:
            d = _infer_direction(
                price, r.get("委托买入价") or 0, r.get("委托卖出价") or 0, r.get("成交方向")
            )
        ticks.append(
            {
                "d": d,
                "amt": round(d_amt, 2),
                "vol": int(round(d_vol / 100.0)),  # 股 → 手
                "price": price,
                "t": _ts_to_hms(r["时间"]),
            }
        )

    if not ticks:
        raise RuntimeError(
            f"thsdk 逐笔差分后无有效行({ths_code})。盘前时段(01:00-09:15)通常只有竞价快照,"
            "成交量=0, 请开盘后重试或换用 get_min_snapshot 验证数据源现状。"
        )
    return ticks


def _fetch_tdx_tck(code: str) -> list[dict]:
    """读通达信 .tck 落盘文件 → 逐笔(官方方向 2B/2S)。

    找不到目录/文件或解析失败抛异常 → dark_flow 回退腾讯。
    文件: {TDX_TCK_DIR}/{code}_YYYYMMDD.tck, 取最新一份(可能跨日)。
    """
    from src.core.tdx_tick_parser import parse_tck, ticks_from_tck

    tck_dir = os.environ.get("TDX_TCK_DIR", "/app/data/tdx_tck")
    base = Path(tck_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"TDX_TCK_DIR 不存在: {tck_dir}")

    files = sorted(base.glob(f"{code}_*.tck"), reverse=True)
    if not files:
        raise FileNotFoundError(f"{tck_dir} 下无 {code}_*.tck")

    trades, _orders, _cancels = parse_tck(str(files[0]))
    ticks = ticks_from_tck(trades)
    if not ticks:
        raise ValueError(f"{files[0].name} 解析出 0 条有效逐笔(连续竞价)")
    return ticks


def fetch_l2_ticks(code: str, source: str = "thsdk") -> list[dict]:
    """按 source 拉取 L2 逐笔, 返回与腾讯逐笔同构的列表。

    Args:
        code: 股票代码(腾讯风格 sz002361 / sh600519, 或 thsdk 风格 USZA002361)
        source: 数据源标识, 支持 "thsdk"(同花顺 L2 3 秒逐笔) / "tdx_tck"(通达信 .tck 官方方向)

    Returns:
        [{"d": "B"/"S"/"M", "amt": 金额(元), "vol": 手数, "price": 价格(元),
          "t": "HH:MM:SS"}] 按时间升序

    抛异常 = 数据源不可用, dark_flow 捕获后回退腾讯逐笔。
    """
    if source == "tdx_tck":
        return _fetch_tdx_tck(code)
    if source != "thsdk":
        raise NotImplementedError(
            f"L2 数据源 {source} 未接入。当前支持 source='thsdk'(同花顺 L2 3 秒逐笔)"
            "、'tdx_tck'(通达信 .tck 官方方向)。其他付费 L2 需先购买(见 dark_flow.DARK_SOURCE 注释)。"
        )
    return _fetch_thsdk(code)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, "/home/ubuntu/sida-src")

    print("=" * 70)
    print("dark_l2 自测: thsdk L2 逐笔 → 腾讯同构 ticks")
    print("=" * 70)

    for demo_code, demo_name in [("sz002361", "神剑股份"), ("sh600519", "贵州茅台")]:
        print(f"\n--- {demo_name} {demo_code} ---")
        ticks = fetch_l2_ticks(demo_code, "thsdk")
        n = len(ticks)
        buy = sum(1 for t in ticks if t["d"] == "B")
        sell = sum(1 for t in ticks if t["d"] == "S")
        mid = sum(1 for t in ticks if t["d"] == "M")
        total_amt = sum(t["amt"] for t in ticks)
        valid_ratio = (buy + sell) / n if n else 0.0
        print(f"  ticks={n}  B={buy} S={sell} M={mid} 方向有效率={valid_ratio*100:.1f}%")
        print(f"  总金额(元)={total_amt:,.0f}  首条={ticks[0] if n else None}")
        if n > 1:
            print(f"  末条={ticks[-1]}")
        # 任务自测门槛: 神剑 ≥4000 行, 方向有效 ≥35%, 金额>0
        assert n >= 4000, f"{demo_name} 行数不足: {n} < 4000"
        assert valid_ratio >= 0.35, f"{demo_name} 方向有效率不足: {valid_ratio:.1%}"
        assert all(t["amt"] > 0 for t in ticks), f"{demo_name} 存在金额<=0 的行"
        assert all(len(t["t"]) == 8 and t["t"][2] == ":" for t in ticks), "时间格式非 HH:MM:SS"
        print(f"  ✅ {demo_name} 自测门槛通过(≥4000 行 / 方向有效≥35% / 金额>0)")

    print("\n" + "=" * 70)
    print("✅ dark_l2 自测完成")
