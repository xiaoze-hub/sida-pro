"""TQ 快照活性观测：用**跨轮累计量推进**补上快照缺失的报时字段。

背景（2026-10-30 网关只读实测定论）：
- `get_market_snapshot` 的 26 个字段里没有任何 time/date 字段，单次快照无法自证"这一刻的价"；
- 订阅类方法 `get_subscribe_hq_stock_list` / `get_subscribe_stock_list` / `get_hq_stock_list`
  在网关上**不存在**（JSON-RPC `-32601 MCP不支持该tqcenter方法名`），而
  `get_stock_list` / `get_market_data` / `get_zdt_data` 只回参数错误（`ErrorId=10`）⇒
  方法名有效。**结论：我们既查不到订阅列表、也建不了订阅**，标的由 TdxW 客户端自己决定；
  未被订阅的标的会返回"最后已知值 + ErrorId=0"，看起来完全正常 —— 这正是盘中"部分快照
  不刷新"且不报错的成因。

单次快照补不了这一层（`prove_quote_date` 只证明到**天**）。本模块用**跨轮观测**判定：
交易时段内累计成交量（手）或累计笔数（`ItemNum`）必须推进；窗口内不推进 ⇒ 判陈旧，
由调用方把该标的从本批剔除，让引擎整批换下一个源（绝不逐票拼源）。

五条不变量（均有测试钉死）：
1. **首次观测 = UNPROVEN**：不声称新鲜，也不冤枉成陈旧；
2. **只认累计量/笔数的推进**：价格变化不能证明有新成交（挂单撤改同样改价）；
3. **计数器回退/重置**（客户端盘中重启）⇒ 证据作废回 UNPROVEN，不得当成"有推进"；
4. **时段标识由墙钟给出**（`cn_session_state` 的 `20261030:am` / `:pm`）：时段一变就
   重新计时。**不能**依赖"上次观测到的是休市"——取数是按需触发的，午休可能一次观测都没有，
   那样第一笔午后观测仍带着上午的基线，跨了整个午休必然超窗 ⇒ 整批误判陈旧。
5. 非交易时段不下结论（收盘后不推进是正常的）。
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, time as dtime
from enum import Enum


class Liveness(str, Enum):
    """一次观测的结论。只有 STALE 会让调用方放弃这份快照。"""

    UNPROVEN = "unproven"  # 证不出来（首次/窗口内尚无推进/计数器重置）
    FRESH = "fresh"  # 窗口内确有推进
    STALE = "stale"  # 交易时段内超过窗口无推进
    NOT_APPLICABLE = "not_applicable"  # 非交易时段：不判（无推进是正常的）


def _as_nonnegative(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


# A 股连续竞价时段。集合竞价（09:15-09:25）刻意**不算**：撮合前累计量本就不动，
# 把那段算成交易时段会让所有标的在开盘前被判陈旧。
_SESSIONS: tuple[tuple[dtime, dtime, str], ...] = (
    (dtime(9, 30), dtime(11, 30), "am"),
    (dtime(13, 0), dtime(15, 0), "pm"),
)


def cn_session_state(moment: datetime) -> tuple[str, bool]:
    """返回 `(时段标识, 是否连续竞价中)`。

    **不判节假日**：法定假日被当成开市 ⇒ 全批无推进 ⇒ 整批降级到腾讯（数据正确，
    代价是一次多余取数）。反过来把交易日误判成休市才会真的漏掉陈旧快照，所以偏向
    "宁可多降级，不放过陈旧"。节假日日历属交易所口径数据，不在这里猜。
    """
    day = moment.strftime("%Y%m%d")
    clock = moment.time()
    if moment.weekday() < 5:
        for start, end, label in _SESSIONS:
            if start <= clock < end:
                return f"{day}:{label}", True
    return f"{day}:closed", False


def cn_continuous_session_open(moment: datetime) -> bool:
    """是否处于 A 股连续竞价时段（`cn_session_state` 的布尔部分）。"""
    return cn_session_state(moment)[1]


@dataclass(frozen=True)
class _Observation:
    volume: float
    items: float | None
    advanced_at: float | None  # 最近一次累计量/笔数推进的**单调**时刻
    seen_at: float  # 本时段内首次观测的单调时刻（"该有推进"的起点）
    last_seen: float  # 最近一次观测（判定单调时钟回拨）
    session: str  # 交易时段标识（墙钟派生，见 cn_session_state）


class TqLivenessGate:
    """进程内、有界的跨轮观测器。

    淘汰口径 = `OrderedDict` 的插入/访问顺序（命中即 `move_to_end`），容量满时丢最久
    未见的那条。全市场扫描（数千标的）会轻易超过上限，若改成"拒绝新标的"，门禁对绝大
    多数票静默失效；被淘汰标的重新出现时回到 UNPROVEN（本就不声称新鲜），代价只是重计时。
    """

    def __init__(self, *, max_symbols: int, stale_seconds: float):
        if max_symbols < 1 or not math.isfinite(stale_seconds) or stale_seconds <= 0:
            raise ValueError("invalid liveness bounds")
        self._limit = max_symbols
        self._stale_seconds = float(stale_seconds)
        self._rows: OrderedDict[str, _Observation] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def tracked(self) -> int:
        with self._lock:
            return len(self._rows)

    def reset(self) -> None:
        with self._lock:
            self._rows.clear()

    def observe(
        self,
        symbol: str,
        volume: object,
        items: object,
        *,
        now: float,
        session: str,
        market_open: bool,
    ) -> Liveness:
        """记录一次快照并给出结论。

        `now` = 单调时钟（只用于算间隔），`session` = 墙钟派生的时段标识，
        `market_open` = 是否连续竞价中。三者都不由本模块猜。
        """
        vol = _as_nonnegative(volume)
        ticks = _as_nonnegative(items)
        if not symbol or vol is None or not math.isfinite(now) or not session:
            return Liveness.UNPROVEN
        with self._lock:
            prev = self._rows.get(symbol)
            restart = (
                prev is None
                or now < prev.last_seen  # 单调时钟回拨：旧证据作废
                or session != prev.session  # 新交易时段（午休后/次日）：重新计时
                or vol < prev.volume  # 计数器重置（客户端重启）
                or (ticks is not None and prev.items is not None and ticks < prev.items)
            )
            if restart:
                while len(self._rows) >= self._limit:
                    self._rows.popitem(last=False)
                self._rows[symbol] = _Observation(vol, ticks, None, now, now, session)
                self._rows.move_to_end(symbol)
                return Liveness.NOT_APPLICABLE if not market_open else Liveness.UNPROVEN
            assert prev is not None  # restart 分支已处理 prev is None
            # 累计成交量（手）或累计笔数任一推进都算证据：笔数推进说明有新的逐笔事件，
            # 报价已经不是上一次那份（成交量不变只可能是撤单/挂单类事件）。
            advanced = vol > prev.volume or (
                ticks is not None and prev.items is not None and ticks > prev.items
            )
            advanced_at = now if advanced else prev.advanced_at
            self._rows[symbol] = _Observation(
                vol, ticks, advanced_at, prev.seen_at, now, session
            )
            self._rows.move_to_end(symbol)
        if not market_open:
            # 非交易时段不下结论（不推进是正常的）；观测仍记录，供开盘后继续判定。
            return Liveness.NOT_APPLICABLE
        if advanced_at is not None and now - advanced_at <= self._stale_seconds:
            return Liveness.FRESH
        baseline = advanced_at if advanced_at is not None else prev.seen_at
        return Liveness.STALE if now - baseline > self._stale_seconds else Liveness.UNPROVEN
