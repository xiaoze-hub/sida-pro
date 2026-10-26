"""今日形成中日 K 的实时合成(2026-10-12)。

设计缺口: 日线入库只有 `KlineBackfillScheduler` 一条链(收盘后 18:00 批量),
全天(含盘后 15:00-18:00)没有任何『当前形成中日 K』的合成逻辑 → `klines`
period='1d' 最新只到前一交易日, 前端只能显『今日尚无K线数据』。

本模块在**读取侧**合成今日 bar(**不写库**, 不污染 qfq 分区), 诚实标注 partial/as_of:

  ① PG 分钟线(period='1m')聚合当日:
     open=首分钟开 / high=最高 / low=最低 / close=末分钟收 / volume=Σ / amount=Σ
     → ``source='live_synthesized'``
  ② 分钟线缺 → 实时日线源(daily_fetcher; 生产=marketdata engine/TQ get_market_data 1d)
     的**最后一根**, 若其日期 == 今天 → ``source='live_<vendor>'``
  ③ 都缺 → ``None``(显式无数据, 禁止编造)

18:00 定型: backfill 入库真实当日 bar 后, 读取链见 last_date==today 即走 'pg' 分支
不再合成 → 合成 partial bar 被真实值**自然取代**(读时合成, 幂等, 无需写库覆盖)。

单位标定(2026-10-12 **实测**, 600000):
  · 腾讯 m1 源单位=手; vendor ``fetch_tencent_minute_kline`` 已 ×100 归股,
    ``klines_minute`` 落库即 ``b.volume``(股)。日线同一 vendor 同样 ×100 归股
    ⇒ **Σ分钟量 == 日线量(股), 禁二次换算**。
    实测: Σ(m1 2026-10-12)=66,287,900 ≈ 1d=66,288,000(股), OHLC 逐项吻合。
  · TQ ``get_market_data`` Volume **原生即股**(vendors/tq.py P2 标定) ⇒ 无换算。
  · 快照 Volume=手 是**另一套契约**(Quote.volume), 与 K 线列无关, 不得混用。

禁引 src.web(core 层铁律 B4.1): 本模块只依赖 src.db / marketdata / src.models。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

#: PG 分钟线的 period 标签(与 src/core/klines_minute.py PERIOD 一致)。
MINUTE_PERIOD = "1m"
#: 分钟聚合合成 bar 的来源标签(诚实标注: 合成值, 非定型)。
SOURCE_MINUTE = "live_synthesized"


@dataclass
class LiveDailyBar:
    """读取侧合成的『今日形成中』日 K。

    与业务硬约束一致: ``partial`` 恒为 True(本类是"未定型"语义), ``as_of`` 是
    数据截至时点(分钟源=末分钟时间; 日线源=取数时点), 下游必须据此显式标注。
    """

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    amount: float | None = None
    partial: bool = True
    source: str = SOURCE_MINUTE
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "partial": self.partial,
            "source": self.source,
            "as_of": self.as_of,
        }


# ── 取值助手(Mapping / 对象 双兼容) ─────────────────────────────────────────


def _field(bar: Any, *names: str) -> Any:
    """从 Mapping 或对象取第一个存在的字段(缺失 → None)。"""
    for n in names:
        if isinstance(bar, dict):
            if n in bar:
                return bar[n]
        else:
            v = getattr(bar, n, None)
            if v is not None:
                return v
    return None


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _day_of(ts: Any) -> str:
    """时间戳/日期对象 → 'YYYY-MM-DD'(仅取前 10 位, 与全仓口径一致)。"""
    return str(ts or "")[:10]


def _norm_iso(d: Any) -> str:
    """日期字面量归一为 'YYYY-MM-DD'。

    全仓多次踩坑: 紧凑 8 位(`20261015`)与 ISO(`2026-10-15`)混用时直接 `[:10]` 比较
    **永不相等**(静默漏匹配, 无报错)。此处两形态都归一到 ISO 再比; 其余原样取前 10 位。
    """
    s = str(d or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _iso_ts(ts: Any) -> str:
    """时间戳 → ISO 秒级字符串(用于 as_of)。

    datetime → ISO(带时区偏移); 'YYYY-MM-DD HH:MM[:SS]' 字符串 → 'YYYY-MM-DDTHH:MM'
    (分钟 K ts 的字符串形态); 其余原样返回。
    """
    if isinstance(ts, datetime):
        return ts.isoformat(timespec="seconds")
    s = str(ts or "")
    if len(s) >= 16 and s[4] == "-" and s[7] == "-" and s[10] == " ":
        return f"{s[:10]}T{s[11:16]}"
    return s


# ── 纯函数: 分钟聚合 ────────────────────────────────────────────────────────


def aggregate_minute_bars(minute_bars: Iterable[Any], day: str) -> dict[str, Any] | None:
    """分钟 bar 列表 → 当日日 K 聚合 dict; 无当日 bar → None。

    纯函数(无 DB/网络): 便于逐条钉住口径。
    单位: 输入 volume/amount 已是**股/元**(见模块头单位标定), **不做任何换算** ——
    Σ 直接给出, 二次 ×100 会错 100 倍。
    amount 诚实口径: 区间内任一分钟 amount 缺失(None) → 聚合 amount=None,
    绝不把"无数据"当 0 求和。volume 同理。

    OHLC 合法性: 非正价 / low>min(o,c) / max(o,c)>high 的脏分钟柱跳过
    (镜像 src/core/klines_minute.py 的入库过滤), 全跳过 → None。
    """
    matched: list[list[Any]] = []
    for b in minute_bars or []:
        if _day_of(_field(b, "ts", "date", "time")) != day:
            continue
        o = _num(_field(b, "open"))
        h = _num(_field(b, "high"))
        l = _num(_field(b, "low"))
        c = _num(_field(b, "close"))
        if o is None or h is None or l is None or c is None:
            continue
        if min(o, h, l, c) <= 0 or l > min(o, c) + 1e-9 or max(o, c) > h + 1e-9:
            continue
        v = _num(_field(b, "volume"))
        a = _num(_field(b, "amount"))
        matched.append([_field(b, "ts", "date", "time"), o, h, l, c, v, a])

    if not matched:
        return None

    matched.sort(key=lambda r: str(r[0]))
    first, last = matched[0], matched[-1]
    vols = [r[5] for r in matched]
    amts = [r[6] for r in matched]
    volume = float(sum(vols)) if all(v is not None for v in vols) else None
    amount = float(sum(amts)) if all(a is not None for a in amts) else None
    return {
        "date": day,
        "open": float(first[1]),
        "high": max(float(r[2]) for r in matched),
        "low": min(float(r[3]) for r in matched),
        "close": float(last[4]),
        "volume": volume,
        "amount": amount,
        "as_of": _iso_ts(last[0]),
        "bar_count": len(matched),
    }


def daily_bar_from_minutes(minute_bars: Iterable[Any], day: str) -> LiveDailyBar | None:
    """分钟线 → 合成日 K(``source='live_synthesized'``); 当日无分钟数据 → None。"""
    agg = aggregate_minute_bars(minute_bars, day)
    if agg is None:
        return None
    return LiveDailyBar(
        date=agg["date"],
        open=agg["open"],
        high=agg["high"],
        low=agg["low"],
        close=agg["close"],
        volume=agg["volume"],
        amount=agg["amount"],
        partial=True,
        source=SOURCE_MINUTE,
        as_of=agg["as_of"],
    )


# ── 纯函数: 桩 bar 判定(与 src/web/api/klines._is_stub_bar 同口径) ──────────


def is_stub_bar(cur: Any, prev_close: float | None = None) -> bool:
    """桩 bar = 无量 + 零振幅(且价等于前收, 若给了前收)。

    盘前网关占位柱(如 603629 09-23: O=H=L=C=前收, volume=0)画出来是一条看不见的
    横线, 必须识别并**不合成**(而不是当"今日实时")。停牌日的真 bar 也零振幅, 但
    不满足"等于前收"的全部特征 —— 故只在末根上做判定。
    """
    vol = _num(_field(cur, "volume"))
    if vol is not None and vol > 0:
        return False
    o = _num(_field(cur, "open"))
    h = _num(_field(cur, "high"))
    low = _num(_field(cur, "low"))
    c = _num(_field(cur, "close"))
    if None in (o, h, low, c):
        return False
    if not (o == h == low == c):
        return False
    if prev_close is not None and abs(c - float(prev_close)) > 1e-9:
        return False
    return True


def is_flat_zero_volume(bar: Any) -> bool:
    """零振幅 + 零量: OHLC 全等 且 volume 为 0/None。

    网关「盘前/集合竞价占位行」与「停牌日无成交」的共同形态。**零量**已排除有成交的
    一字板(一字板虽零振幅但有量); 单看形态仍无法与停牌日区分, 是否**退化**必须结合
    日期(今日)与停牌表判定, 见 `is_degenerate_placeholder`。
    """
    vol = _num(_field(bar, "volume"))
    if vol is not None and vol > 0:
        return False
    o = _num(_field(bar, "open"))
    h = _num(_field(bar, "high"))
    low = _num(_field(bar, "low"))
    c = _num(_field(bar, "close"))
    if None in (o, h, low, c):
        return False
    return o == h == low == c


def is_degenerate_placeholder(bar: Any, today: str) -> bool:
    """「今日退化占位行」形态: 日期 == today 且 `is_flat_zero_volume`。

    与 `is_stub_bar` 的关键区别: **不要求价等于前收**。2026-10-15 生产实测: 09:20 盘前
    预缓存写入的今日行 OHLC 全等, 但价 = 集合竞价指示价(≠前收, 144/147 行如此) ⇒ 旧
    `is_stub_bar` 的「等于前收」子句漏判, 零量假平棒被当 `today_bar='pg'` 真值端给前端
    (「0 量冒充真值」红线)。真实停牌日也零振幅零量, 由调用方用停牌表
    (`src.core.halts.is_halted`)排除 ⇒ 只在**非停牌日**才判为退化。

    日期比较经 `_norm_iso` 归一 —— 紧凑 8 位与 ISO 混用也不会漏判。
    """
    if _norm_iso(_field(bar, "date")) != _norm_iso(today):
        return False
    return is_flat_zero_volume(bar)


# ── 纯函数: 实时日线源取今日 bar ────────────────────────────────────────────


def daily_bar_from_daily_bars(
    bars: Iterable[Any],
    today: str,
    *,
    vendor: str = "",
    as_of: str = "",
) -> LiveDailyBar | None:
    """实时日线序列 → 今日 forming bar(``source='live_<vendor>'``)。

    只认**最后一根且日期 == 今天**的 bar(源可能不含今日 → None); 末根为桩 → None。
    前收由序列倒数第二根给出, 用于桩判定。
    """
    seq = [b for b in (bars or []) if _num(_field(b, "open")) is not None]
    if not seq:
        return None
    last = seq[-1]
    if _day_of(_field(last, "date")) != today:
        return None
    prev_close = _num(_field(seq[-2], "close")) if len(seq) >= 2 else None
    if is_stub_bar(last, prev_close):
        return None
    src = f"live_{vendor}" if vendor else "live_daily"
    return LiveDailyBar(
        date=today,
        open=float(_num(_field(last, "open"))),
        high=float(_num(_field(last, "high"))),
        low=float(_num(_field(last, "low"))),
        close=float(_num(_field(last, "close"))),
        volume=_num(_field(last, "volume")),
        amount=_num(_field(last, "amount")),
        partial=True,
        source=src,
        as_of=as_of or _iso_ts(_field(last, "date")),
    )


# ── I/O: PG 分钟线读取(默认实现, 可注入替换) ────────────────────────────────


def read_minute_bars_pg(
    symbol: str,
    market: str,
    day: str,
    *,
    engine: Any = None,
) -> list[dict[str, Any]]:
    """读 PG ``klines`` period='1m' 当日分钟柱(升序)。库/表缺失 → [] (fail-soft)。

    日界用 Asia/Shanghai(date 的 00:00 → 次日 00:00, 半开区间), 与 A股业务日一致。
    """
    from sqlalchemy import text

    if engine is None:
        from src.db.session import engine as _engine  # 惰性 import(避免模块加载期建连)

        engine = _engine
    try:
        start = datetime.fromisoformat(day).replace(tzinfo=_CST)
    except ValueError:
        return []
    end = start + timedelta(days=1)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume, amount FROM klines "
                    "WHERE symbol=:s AND market=:m AND period=:p "
                    "  AND ts >= :a AND ts < :b ORDER BY ts ASC"
                ),
                {"s": symbol, "m": (market or "CN").upper(), "p": MINUTE_PERIOD,
                 "a": start, "b": end},
            ).fetchall()
    except Exception as e:  # noqa: BLE001 库不可用 → 显式当"无分钟数据", 不编造
        logger.debug("[live-bar] 分钟线读取失败 %s.%s: %r", symbol, market, e)
        return []
    return [
        {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4],
         "volume": r[5], "amount": r[6]}
        for r in rows
    ]


def _default_daily_fetcher(symbol: str, market: str) -> tuple[list[Any], str]:
    """实时日线源(生产=marketdata engine; TQ 高优先, 腾讯兜底), 返回 (bars, vendor)。"""
    from src.core.marketdata_client import get_market_data

    return get_market_data().klines_with_vendor(symbol, market=market, days=5, min_count=1)


def _split_daily_result(res: Any) -> tuple[list[Any], str]:
    """daily_fetcher 返回值归一: (bars, vendor) | bars | None → (list, vendor)。"""
    if res is None:
        return [], ""
    if isinstance(res, tuple) and len(res) == 2:
        bars, vendor = res
        return list(bars or []), str(vendor or "")
    return list(res or []), ""


# ── 编排: 三源优先级 ────────────────────────────────────────────────────────


def synthesize_live_daily_bar(
    symbol: str,
    market: str | Any,
    *,
    today: str | None = None,
    minute_reader: Callable[[str, str, str], list[Any]] | None = None,
    daily_fetcher: Callable[[str, str], Any] | None = None,
    now: datetime | None = None,
) -> LiveDailyBar | None:
    """合成今日形成中日 K; 三源全缺 → None(显式无数据, 绝不编造)。

    优先级: ① PG 分钟线聚合 → ② 实时日线源今日 bar → ③ None。

    symbol/market/today 三个入参显式(生产 today 缺省取 Asia/Shanghai 当天);
    minute_reader / daily_fetcher 可注入(单测禁真网络/真库); now 可注入(锁 as_of)。
    """
    mkt = market.value if hasattr(market, "value") else str(market or "CN")
    mkt = mkt.upper()
    day = today or (now or datetime.now(_CST)).astimezone(_CST).date().isoformat()

    # ① PG 分钟线聚合
    reader = minute_reader or (lambda s, m, d: read_minute_bars_pg(s, m, d))
    try:
        rows = reader(symbol, mkt, day)
    except Exception as e:  # noqa: BLE001 读取异常 → 当"无分钟数据", 落下一源
        logger.debug("[live-bar] minute_reader 异常 %s.%s: %r", symbol, mkt, e)
        rows = []
    bar = daily_bar_from_minutes(rows or [], day)
    if bar is not None:
        return bar

    # ② 实时日线源今日 bar
    fetcher = daily_fetcher or _default_daily_fetcher
    try:
        res = fetcher(symbol, mkt)
    except Exception as e:  # noqa: BLE001 取数失败 → 显式无数据(不物化空 bar)
        logger.debug("[live-bar] daily_fetcher 异常 %s.%s: %r", symbol, mkt, e)
        res = None
    bars, vendor = _split_daily_result(res)
    as_of = (now or datetime.now(_CST)).astimezone(_CST).isoformat(timespec="seconds")
    return daily_bar_from_daily_bars(bars, day, vendor=vendor, as_of=as_of)
