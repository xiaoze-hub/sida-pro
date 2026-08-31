# -*- coding: utf-8 -*-
"""thsdk_alert.py — 基于同花顺 SDK(thsdk) 的三类盘口预警算法。

模块定位
========
面向 A 股盘口级数据的三类"预警/候选"算法, 全部基于 thsdk(THS 上下文模式,
游客账户可用)实时拉取真实行情实现:

1. close_auction_surge —— 尾盘大单突击检测
   输入 big_order_flow 逐笔大单流(方向编码已破解: +1 主买 / +2 更强主买,
   -1 主卖 / -2 更强主卖), 限定 14:30-15:00 时段统计净额, 与全天前期
   (09:25/09:30-14:30) 对比, 判断"拉尾 / 砸尾 / 中性"并给 0-100 分数。

2. auction_snapshot —— 竞价快照分析
   输入 tick_super_level1 超级盘口(09:15-09:25 时段为 9 秒一帧的盘口快照,
   成交量=0 的占位行, 委托买入价/委托卖出价=虚拟匹配价), 计算竞价
   最高/最低/现价、09:20 前后撤单率近似、竞价方向(高开/低开/平开)。

3. ai_candidate_pool —— wencai 自然语言候选池
   调用 thsdk.wencai_nlp 跑两个量化 query, 返回候选股票列表(代码转
   USZA/USHA/USTM 格式)。

工程约束
========
- 所有 thsdk 查询必须在 ``with THS() as ths:`` 上下文内完成。
- 限频: 相邻两次请求最小间隔 50ms; 失败重试 3 次, 退避 1s/2s/4s。
- 熔断: 60 秒窗口内累计失败 10 次则熔断 60 秒。
- 非交易时段调用时尾盘/竞价数据可能为空, 如实输出并附带说明字段。
- 不伪造结果: 数据缺失时输出空结构并在说明字段中标注原因。
"""

from __future__ import annotations

import math
import time
from datetime import datetime, time as dtime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

from thsdk import THS, Response

# ---------------------------------------------------------------------------
# 全局常量
# ---------------------------------------------------------------------------

# 东方时区(北京时间, 无夏令时)
TZ_CN = timezone(timedelta(hours=8))

# 限频: 相邻两次请求最小间隔(秒)
RATE_LIMIT_SEC = 0.05

# 重试: 最多尝试次数 + 指数退避基数(秒)
MAX_RETRIES = 3
RETRY_BACKOFF = [1.0, 2.0, 4.0]

# 熔断: 60 秒窗口内失败 10 次 -> 熔断 60 秒
CIRCUIT_WINDOW_SEC = 60.0
CIRCUIT_MAX_FAIL = 10
CIRCUIT_OPEN_SEC = 60.0

# 大单方向编码(已破解): +1 主买 / +2 更强主买 / -1 主卖 / -2 更强主卖
SIGNED_DIRECTION: Dict[int, int] = {1: 1, 2: 1, -1: -1, -2: -1}

# 尾盘判定阈值: 尾盘净买/净卖超过 200 万(元) 才判定拉尾/砸尾
TAIL_NET_THRESHOLD_WAN = 200.0

# 高开/低开判定的相对阈值(0.1%)
GAP_THRESHOLD = 0.001

# 竞价时段/尾盘时段(北京时间 HH:MM)
AUCTION_START = dtime(9, 15)
AUCTION_END = dtime(9, 25)
TAIL_START = dtime(14, 30)
TAIL_END = dtime(15, 0)
DAY_START = dtime(9, 25)   # big_order_flow 首行通常为 09:25 集合竞价大单

# wencai 候选池的两个 query
WENCAI_QUERIES: List[str] = [
    "主力净流入为负,但股价逆势上涨,非ST",
    "近5日主力净流入为正,非ST",
]


# ---------------------------------------------------------------------------
# 限频 / 重试 / 熔断基础设施
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """简单的失败熔断器: 窗口内失败次数超限则短路, 冷却后自动半开。"""

    def __init__(self) -> None:
        self._fail_times: List[float] = []
        self._opened_until: float = 0.0

    def is_open(self) -> bool:
        """熔断是否处于打开(拒绝请求)状态。"""
        if self._opened_until > time.monotonic():
            return True
        # 窗口滑动: 丢弃窗口外的旧失败记录
        now = time.monotonic()
        self._fail_times = [t for t in self._fail_times if now - t < CIRCUIT_WINDOW_SEC]
        return len(self._fail_times) >= CIRCUIT_MAX_FAIL

    def record_failure(self) -> None:
        """记录一次失败; 若窗口内失败次数越限则打开熔断。"""
        now = time.monotonic()
        self._fail_times.append(now)
        self._fail_times = [t for t in self._fail_times if now - t < CIRCUIT_WINDOW_SEC]
        if len(self._fail_times) >= CIRCUIT_MAX_FAIL:
            self._opened_until = now + CIRCUIT_OPEN_SEC

    def record_success(self) -> None:
        """成功调用后清空失败计数(熔断恢复)。"""
        self._fail_times = []
        self._opened_until = 0.0


class ThrottledCaller:
    """对 thsdk 调用的统一包装: 限频 50ms + 重试 3 次(1s/2s/4s) + 熔断。"""

    def __init__(self) -> None:
        self._last_call_ts: float = 0.0
        self._breaker = CircuitBreaker()

    def _throttle(self) -> None:
        """保证相邻两次发请求的最小间隔, 实现 50ms 限频。"""
        wait = RATE_LIMIT_SEC - (time.monotonic() - self._last_call_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.monotonic()

    def call(self, fn: Callable[..., Response], *args: Any, **kwargs: Any) -> Optional[Response]:
        """带限频/重试/熔断地调用 thsdk 方法, 返回 Response 或 None。"""
        if self._breaker.is_open():
            return None  # 熔断打开, 直接短路

        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                resp = fn(*args, **kwargs)
                if resp is None or not getattr(resp, "success", False):
                    err = getattr(resp, "error", "unknown") if resp else "None response"
                    raise RuntimeError(f"thsdk 调用失败: {err}")
                self._breaker.record_success()
                return resp
            except Exception as exc:  # noqa: BLE001 - 统一按失败重试
                self._breaker.record_failure()
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF[attempt])
                else:
                    # 最后一次失败: 记录但不抛出, 上层按数据为空处理
                    print(f"[thsdk_alert] 重试 {MAX_RETRIES} 次后仍失败: {exc!r}")
                    return None
        return None  # 不可达, 防御返回


_CALLER = ThrottledCaller()


# ---------------------------------------------------------------------------
# 时间辅助
# ---------------------------------------------------------------------------

def _epoch(hh: int, mm: int, ss: int = 0, ref_ts: Optional[float] = None) -> int:
    """构造北京时间 HH:MM:SS 对应的 epoch 秒。

    ref_ts 用于确定"日期": 默认取数据行内的时间(或当前时间), 保证与
    big_order_flow/tick_super_level1 返回的 epoch 时间处于同一交易日。
    """
    base = datetime.fromtimestamp(ref_ts or time.time(), TZ_CN)
    return int(datetime(base.year, base.month, base.day, hh, mm, ss, tzinfo=TZ_CN).timestamp())


def _hm(ts: int) -> str:
    """epoch 秒 -> 北京时间 HH:MM:SS 字符串。"""
    return datetime.fromtimestamp(ts, TZ_CN).strftime("%H:%M:%S")


def _is_trade_hours_now() -> bool:
    """当前北京时间是否在交易时段内(尾盘/竞价检测有效的前提)。"""
    now = datetime.now(TZ_CN)
    hm = dtime(now.hour, now.minute, now.second)
    # 连续竞价 09:30-11:30 / 13:00-15:00, 竞价 09:15-09:25
    in_am = dtime(9, 30) <= hm <= dtime(11, 30)
    in_pm = dtime(13, 0) <= hm <= dtime(15, 0)
    in_auction = AUCTION_START <= hm <= AUCTION_END
    return in_am or in_pm or in_auction


# ---------------------------------------------------------------------------
# 数据获取(THS 上下文模式)
# ---------------------------------------------------------------------------

def _fetch_big_order_flow(ths: THS, symbol: str) -> List[dict]:
    """拉取当日 big_order_flow 大单流(无历史日期参数, 恒为当日数据)。"""
    resp = _CALLER.call(ths.big_order_flow, symbol)
    if resp is None:
        return []
    rows = [r for r in (resp.data or []) if isinstance(r, dict)]
    # 仅保留时间字段为合法 epoch 的行(丢弃可能的汇总行)
    return [r for r in rows if isinstance(r.get("时间"), int) and 100_000_000 < r["时间"] < 4_000_000_000]


def _fetch_tick_super_level1(ths: THS, symbol: str, date: Optional[str] = None) -> List[dict]:
    """拉取 tick_super_level1 超级盘口; date 传 YYYYMMDD 可查历史, None 为当日。

    返回时剔除末尾的汇总行(如 {'时间': 20260819, '五日成交总量': ...})。
    """
    if date:
        resp = _CALLER.call(ths.tick_super_level1, symbol, date)
    else:
        resp = _CALLER.call(ths.tick_super_level1, symbol)
    if resp is None:
        return []
    rows = [r for r in (resp.data or []) if isinstance(r, dict)]
    # 汇总行的 时间 是 YYYYMMDD(int), 远小于 epoch 秒, 直接按数值窗口过滤
    return [r for r in rows if isinstance(r.get("时间"), int) and 100_000_000 < r["时间"] < 4_000_000_000]


def _fetch_prev_close(ths: THS, symbol: str) -> Optional[float]:
    """通过日 K 线取前一日收盘价(用于竞价方向判定)。失败返回 None。"""
    resp = _CALLER.call(ths.klines, symbol, count=3, interval="day")
    if resp is None:
        return None
    rows = [r for r in (resp.data or []) if isinstance(r, dict) and "收盘价" in r]
    if len(rows) >= 2:
        return float(rows[-2]["收盘价"])
    return None


def _convert_wencai_code(raw_code: Any) -> Optional[str]:
    """wencai 返回的 300033.SZ 格式 -> thsdk 的 USZA300033 格式。

    规则: .SH(沪 60 开头) -> USHA; .SZ(深 00/30 开头) -> USZA;
          .BJ(北 8/4 开头) -> USTM; 无法识别返回 None。
    """
    if raw_code is None:
        return None
    code = str(raw_code).strip().upper()
    if "." in code:
        num, market = code.split(".", 1)
    else:
        num, market = code, ""
    if not num.isdigit() or len(num) != 6:
        return None
    if market in ("SH", "") and num.startswith(("60", "68")):
        return f"USHA{num}"
    if market in ("SZ", "") and num.startswith(("00", "30")):
        return f"USZA{num}"
    if market in ("BJ", "") and num.startswith(("8", "4")):
        return f"USTM{num}"
    return None


# ---------------------------------------------------------------------------
# 算法一: 尾盘大单突击检测
# ---------------------------------------------------------------------------

def close_auction_surge(big_orders: List[dict]) -> Dict[str, Any]:
    """尾盘大单突击检测。

    输入: big_order_flow 行列表(keys: 时间(epoch 秒), 成交方向(±1/±2),
          成交量(股), 总金额(元), 委托买入价, 委托卖出价)。
    逻辑:
      - 限定 14:30-15:00 时段, 统计 ±2 级(更强)大单净额、±1+±2 合计净额;
      - 与全天前期(09:25/09:30 - 14:30) 净额对比;
      - 方向判定: 尾盘净买 > 200 万 -> 拉尾; 尾盘净卖 > 200 万 -> 砸尾; 否则中性;
      - surge_score 0-100: 综合尾盘净额量级与占全天净额贡献度。
    输出键(机器可读): tail_net_wan, tail_level2_net_wan, tail_level1_net_wan,
      day_net_wan, prev_net_wan, direction, surge_score, rows_used, note。
    """
    if not big_orders:
        return {
            "tail_net_wan": None, "tail_level2_net_wan": None, "tail_level1_net_wan": None,
            "day_net_wan": None, "prev_net_wan": None,
            "direction": "无数据", "surge_score": None, "rows_used": 0,
            "note": "无 big_order_flow 数据(非交易时段或查询失败), 尾盘检测跳过",
        }

    # 参考日期: 取数据内部时间, 保证交易日对齐
    ref_ts = float(big_orders[0]["时间"])
    t_start = _epoch(TAIL_START.hour, TAIL_START.minute, ref_ts=ref_ts)
    t_end = _epoch(TAIL_END.hour, TAIL_END.minute, ref_ts=ref_ts)
    day_start = _epoch(DAY_START.hour, DAY_START.minute, ref_ts=ref_ts)

    tail_rows: List[dict] = []
    prev_rows: List[dict] = []
    day_net = 0.0
    for row in big_orders:
        ts = int(row["时间"])
        direction = int(row.get("成交方向", 0))
        amount = float(row.get("总金额", 0) or 0)
        signed = amount * SIGNED_DIRECTION.get(direction, 0)
        day_net += signed
        if t_start <= ts <= t_end:
            tail_rows.append(row)
        elif day_start <= ts < t_start:
            prev_rows.append(row)

    tail_net = sum(float(r["总金额"]) * SIGNED_DIRECTION.get(int(r["成交方向"]), 0) for r in tail_rows)
    tail_net_wan = tail_net / 1e4
    # ±2 级(更强大单)与 ±1 级净额拆分
    tail_l2 = sum(
        float(r["总金额"]) * SIGNED_DIRECTION.get(int(r["成交方向"]), 0)
        for r in tail_rows if abs(int(r["成交方向"])) == 2
    )
    tail_l1 = sum(
        float(r["总金额"]) * SIGNED_DIRECTION.get(int(r["成交方向"]), 0)
        for r in tail_rows if abs(int(r["成交方向"])) == 1
    )
    prev_net = sum(float(r["总金额"]) * SIGNED_DIRECTION.get(int(r["成交方向"]), 0) for r in prev_rows)
    day_net_wan = day_net / 1e4
    prev_net_wan = prev_net / 1e4

    # 方向判定: 净买/净卖超过 200 万
    if tail_net_wan > TAIL_NET_THRESHOLD_WAN:
        direction = "拉尾"
    elif tail_net_wan < -TAIL_NET_THRESHOLD_WAN:
        direction = "砸尾"
    else:
        direction = "中性"

    # surge_score: 0-100, 50 为中性
    #   - 量级项: tanh(尾盘净额/400万) -> ±400 万后饱和, 贡献 ±25 分
    #   - 贡献项: tanh(尾盘净额 / max(|全天净额|, 10万) * 2) -> 尾盘对全天方向的
    #     主导程度, 贡献 ±25 分
    denom = max(abs(day_net), 100_000.0)
    mag_comp = math.tanh(tail_net / 4_000_000.0)
    contrib_comp = math.tanh((tail_net / denom) * 2.0)
    score = round(50.0 + 25.0 * mag_comp + 25.0 * contrib_comp)
    surge_score = max(0, min(100, score))

    note = (
        f"尾盘窗口 {_hm(t_start)}-{_hm(t_end)} 共 {len(tail_rows)} 笔大单; "
        f"前期 {_hm(day_start)}-{_hm(t_start)} 共 {len(prev_rows)} 笔; "
        f"全天共 {len(big_orders)} 笔(含 09:25 集合竞价大单)"
    )
    return {
        "tail_net_wan": round(tail_net_wan, 2),          # 尾盘(14:30-15:00)大单净额, 万元
        "tail_level2_net_wan": round(tail_l2 / 1e4, 2),  # 其中 ±2 级(更强)净额, 万元
        "tail_level1_net_wan": round(tail_l1 / 1e4, 2),  # 其中 ±1 级净额, 万元
        "day_net_wan": round(day_net_wan, 2),            # 全天大单净额, 万元
        "prev_net_wan": round(prev_net_wan, 2),          # 前期(开盘-14:30)净额, 万元
        "direction": direction,                          # 拉尾 / 砸尾 / 中性
        "surge_score": surge_score,                      # 0-100, >60 偏拉尾, <40 偏砸尾
        "rows_used": len(big_orders),
        "note": note,
    }


# ---------------------------------------------------------------------------
# 算法二: 竞价快照分析
# ---------------------------------------------------------------------------

def auction_snapshot(ticks: List[dict], prev_close: Optional[float]) -> Dict[str, Any]:
    """竞价快照分析(09:15-09:25)。

    输入: tick_super_level1 行列表(keys: 时间(epoch 秒), 价格, 成交方向, 成交量,
          总金额, 委托买入价, 委托卖出价, 买1量 等档位字段)。
    要点:
      - 09:15-09:25 期间行多为成交量=0、成交方向=4294967295 的盘口快照
        (9 秒一帧), 委托买入价/委托卖出价 等于虚拟匹配价, 真实信号是 买1量/卖1量
        (虚拟匹配量)。
      - 09:25:00 行是真实集合竞价撮合(成交量>0, 方向=5), 其价格即开盘价。
      - 撤单率近似: 用虚拟匹配量 09:20 前的峰值 vs 09:20 首帧/竞价末值 的衰减
        比例近似(09:20 后交易所禁止撤单, 因此 09:20 前的量能坍缩即撤单行为)。
      - 竞价量能对比昨日: 暂无昨日竞价数据源, 输出 None。
    输出键: auction_high/low/price(现价=开盘价), 撤单率近似, 竞价方向
      (高开/低开/平开, 基于 昨收 vs 竞价价), gap_pct, 相关说明字段。
    """
    if not ticks:
        return {
            "auction_high": None, "auction_low": None, "auction_price": None,
            "prev_close": prev_close, "direction": "无数据", "gap_pct": None,
            "withdraw_rate_pre0920": None, "withdraw_rate_full": None,
            "peak_match_vol": None, "final_match_vol": None,
            "volume_vs_prev": None, "rows_used": 0,
            "note": "无 tick_super_level1 数据(非交易时段或查询失败), 竞价分析跳过",
        }

    ref_ts = float(ticks[0]["时间"])
    t_auction_start = _epoch(AUCTION_START.hour, AUCTION_START.minute, ref_ts=ref_ts)
    t_auction_end = _epoch(AUCTION_END.hour, AUCTION_END.minute, ref_ts=ref_ts)
    t_0920 = _epoch(9, 20, ref_ts=ref_ts)

    auction_rows = [
        r for r in ticks
        if t_auction_start <= int(r["时间"]) <= t_auction_end
    ]

    if not auction_rows:
        return {
            "auction_high": None, "auction_low": None, "auction_price": None,
            "prev_close": prev_close, "direction": "无数据", "gap_pct": None,
            "withdraw_rate_pre0920": None, "withdraw_rate_full": None,
            "peak_match_vol": None, "final_match_vol": None,
            "volume_vs_prev": None, "rows_used": 0,
            "note": "竞价时段(09:15-09:25)无快照行, 竞价分析跳过",
        }

    # 竞价最高/最低/现价: 价格字段全程为虚拟成交价, 09:25 行为真实撮合价
    prices = [float(r["价格"]) for r in auction_rows if r.get("价格")]
    auction_high = max(prices)
    auction_low = min(prices)
    # 现价 = 竞价结束时的撮合价(最后一帧价格); 若 09:25 行有成交量则为真实开盘价
    auction_price = float(auction_rows[-1]["价格"])
    opening_trade = None
    for r in reversed(auction_rows):
        if int(r.get("成交量", 0) or 0) > 0:
            opening_trade = r
            break
    if opening_trade is not None:
        auction_price = float(opening_trade["价格"])

    # 撤单率近似: 虚拟匹配量(买1量)序列。
    # 注意: 09:25:00 的真实撮合行(成交量>0)的 买1量/卖1量 是撮合后的残余量
    # (如 200 股), 不能当作匹配量, 因此只统计 成交量==0 的快照帧。
    trade_volume = None  # 若存在真实撮合行, 记录其成交量
    for r in reversed(auction_rows):
        if int(r.get("成交量", 0) or 0) > 0:
            trade_volume = int(r["成交量"])
            break
    snapshot_rows = [r for r in auction_rows if int(r.get("成交量", 0) or 0) == 0]
    vol_series = [
        (int(r["时间"]), float(r.get("买1量", 0) or 0) if r.get("买1量") else float(r.get("当前量", 0) or 0))
        for r in snapshot_rows
    ]
    vol_series = [(ts, v) for ts, v in vol_series if v > 0]
    peak_pre = max((v for ts, v in vol_series if ts < t_0920), default=0.0)
    first_after = next((v for ts, v in vol_series if ts >= t_0920), None)
    final_vol = vol_series[-1][1] if vol_series else 0.0  # 最后一帧快照的匹配量
    # 最后价格移动(委托买入价/卖出价 在快照中等于虚拟价, 用其位移近似)
    last_price = float(auction_rows[-1]["价格"])
    price_at_0920 = next(
        (float(r["价格"]) for r in auction_rows if int(r["时间"]) >= t_0920 and r.get("价格")),
        last_price,
    )

    if peak_pre > 0:
        withdraw_pre = max(0.0, (peak_pre - (first_after or 0.0)) / peak_pre)
        withdraw_full = max(0.0, (peak_pre - final_vol) / peak_pre)
    else:
        withdraw_pre, withdraw_full = None, None
    price_move = (last_price - price_at_0920) / price_at_0920 if price_at_0920 else None

    # 竞价方向: 昨收 vs 竞价价
    direction, gap_pct = "无数据", None
    if prev_close is not None and auction_price:
        gap_pct = (auction_price - prev_close) / prev_close * 100.0
        if gap_pct > GAP_THRESHOLD * 100:
            direction = "高开"
        elif gap_pct < -GAP_THRESHOLD * 100:
            direction = "低开"
        else:
            direction = "平开"

    note = (
        f"竞价时段 {len(auction_rows)} 帧({len(snapshot_rows)} 帧快照 + "
        f"{'1' if trade_volume else '0'} 帧真实撮合, 撮合量 {trade_volume or 0:,}); "
        f"09:20 前匹配量峰值 {int(peak_pre):,}, 09:20 首帧 {int(first_after or 0):,}; "
        f"竞价末帧快照 {int(final_vol):,}; 撤单率为基于虚拟匹配量衰减的近似"
        f"(委托买入/卖出价与虚拟价重合, 无真实档位)"
    )
    return {
        "auction_high": auction_high,        # 竞价期间虚拟成交价最高
        "auction_low": auction_low,          # 竞价期间虚拟成交价最低
        "auction_price": auction_price,      # 竞价现价(= 开盘价, 09:25 撮合价)
        "prev_close": prev_close,            # 昨收基准
        "direction": direction,              # 高开 / 低开 / 平开 / 无数据
        "gap_pct": round(gap_pct, 3) if gap_pct is not None else None,  # 相对昨收涨跌幅 %
        "withdraw_rate_pre0920": round(withdraw_pre, 4) if withdraw_pre is not None else None,  # 09:20 前撤单率近似
        "withdraw_rate_full": round(withdraw_full, 4) if withdraw_full is not None else None,    # 全程撤单率近似(对竞价末帧)
        "peak_match_vol": int(peak_pre),     # 09:20 前虚拟匹配量峰值(股)
        "final_match_vol": int(final_vol),   # 竞价末帧快照虚拟匹配量(股)
        "trade_volume": trade_volume,        # 09:25 真实撮合量(股), 无则 None
        "price_move_from_0920": round(price_move, 5) if price_move is not None else None,
        "volume_vs_prev": None,              # 竞价量能对比昨日: 暂无数据源, 按要求输出 None
        "rows_used": len(auction_rows),
        "note": note,
    }


# ---------------------------------------------------------------------------
# 算法三: wencai 候选池
# ---------------------------------------------------------------------------

def ai_candidate_pool(ths: THS) -> Dict[str, Any]:
    """通过 wencai_nlp 查询两个量化 query, 返回候选股票列表。

    返回结构: {queries: [原始 query...], candidates: [{code, name, query}...],
    query_rows: {query: 行数}, errors: []}。股票代码统一转 USZA/USHA/USTM 格式。
    """
    candidates: List[dict] = []
    seen: set = set()
    query_rows: Dict[str, int] = {}
    errors: List[str] = []

    for q in WENCAI_QUERIES:
        resp = _CALLER.call(ths.wencai_nlp, q)
        if resp is None:
            errors.append(f"{q} -> 查询失败/熔断")
            query_rows[q] = 0
            continue
        rows = [r for r in (resp.data or []) if isinstance(r, dict)]
        query_rows[q] = len(rows)
        for r in rows:
            code = _convert_wencai_code(r.get("股票代码"))
            name = str(r.get("股票简称") or r.get("名称") or "").strip()
            if code and code not in seen:
                seen.add(code)
                candidates.append({"code": code, "name": name, "query": q})

    note = f"共 {len(candidates)} 个去重候选; 两个 query 原始行数: {query_rows}"
    if errors:
        note += "; 失败: " + " | ".join(errors)
    return {
        "queries": list(WENCAI_QUERIES),
        "candidates": candidates,
        "query_rows": query_rows,
        "errors": errors,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run(symbol: str, date: Optional[str] = None, prev_close: Optional[float] = None) -> Dict[str, Any]:
    """统一主入口: 拉取真实数据并跑三个算法。

    Args:
        symbol: thsdk 代码格式, 如 USZA002361(神剑股份)。
        date: YYYYMMDD 历史日期; big_order_flow 无历史参数恒为当日,
              传入非当日日期时 tick 用历史、大单仅当日(在说明中注明)。
        prev_close: 昨收价; 缺省时尝试通过日 K 线自动获取。

    Returns:
        {"close_surge": {...}, "auction": {...}, "wencai_pool": {...}}
    """
    with THS() as ths:
        # 1) 尾盘大单
        bof_rows = _fetch_big_order_flow(ths, symbol)
        # 2) 竞价快照(支持历史日期)
        tick_rows = _fetch_tick_super_level1(ths, symbol, date)
        # 3) 昨收: 显式传入优先; 否则日 K 线推算
        if prev_close is None:
            prev_close = _fetch_prev_close(ths, symbol)
        # 4) wencai 候选池(游客可用, 与个股无关)
        pool = ai_candidate_pool(ths)

    close_surge = close_auction_surge(bof_rows)
    auction = auction_snapshot(tick_rows, prev_close)

    # 交易时段提示: 非交易时段数据可能为空, 如实标注
    if not _is_trade_hours_now():
        close_surge["note"] += "; 当前为北京时间非交易时段(收盘后), 若数据为空属正常"
        auction["note"] += "; 当前为北京时间非交易时段(收盘后), 若数据为空属正常"

    # big_order_flow 无历史参数: 传入非当日 date 时注明口径
    if date:
        close_surge["note"] += f"; 注: big_order_flow 无历史参数, 返回的是当日({date})数据口径即当日行情"

    return {
        "symbol": symbol,
        "date": date,
        "prev_close": prev_close,
        "close_surge": close_surge,
        "auction": auction,
        "wencai_pool": pool,
    }


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    print("=" * 70)
    print("thsdk_alert 自测: 神剑股份 USZA002361 + wencai 双查询")
    print("=" * 70)

    result = run("USZA002361", prev_close=12.12)

    # 自动从 K 线取到的昨收做对照(仅作参考, 不再覆盖自测基准)
    print("\n【主入口汇总】")
    print(f"symbol   : {result['symbol']}")
    print(f"date     : {result['date']}  (None=当日)")
    print(f"prev_close: {result['prev_close']}  (任务给定基准 12.12)")

    print("\n【算法一: 尾盘大单突击检测 close_auction_surge】")
    print(json.dumps(result["close_surge"], ensure_ascii=False, indent=2))

    print("\n【算法二: 竞价快照分析 auction_snapshot】")
    print(json.dumps(result["auction"], ensure_ascii=False, indent=2))

    print("\n【算法三: wencai 候选池 ai_candidate_pool】")
    pool = result["wencai_pool"]
    print(f"note: {pool['note']}")
    print(f"query_rows: {pool['query_rows']}")
    print(f"errors: {pool['errors']}")
    cands = pool["candidates"]
    print(f"候选数: {len(cands)}")
    for i, c in enumerate(cands[:10], 1):
        print(f"  {i:>3}. {c['code']}  {c['name']}")
    if len(cands) > 10:
        print(f"  ... 其余 {len(cands) - 10} 条略")

    print("\n自测完成。")