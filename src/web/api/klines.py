import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from datetime import date, datetime
from zoneinfo import ZoneInfo
import concurrent.futures
import threading
import time as _time

from pydantic import BaseModel, Field

from src.collectors.kline_collector import KlineCollector
from src.models.market import MarketCode
from src.core.trading_calendar import is_trading_day
from src.web.api.auth import get_current_user
from src.db.models import User

_APP_TZ = "Asia/Shanghai"

# ── K 线主数据缓存 + 单飞（2026-10-21）──────────────────────────────────
# 事故：某用户的图表页把 GET /api/klines/600111 打到 **1264 次/分钟**（≈21 次/秒），
# 触发限流中间件 300/min → 图表上出现「错误: HTTP 429」并空白。
# 日线在盘中不会按秒变化，但每次请求都会重新走一遍取数/回退链；这里加 60s 进程内缓存 +
# 同 key 单飞（并发相同请求只算一次），把风暴的代价从"每次重算"降到"一次"。
_KL_TTL_S = 60.0
_kl_cache: dict[str, tuple[float, object]] = {}
_kl_locks: dict[str, threading.Lock] = {}
_kl_guard = threading.Lock()


def _kl_lock(key: str) -> threading.Lock:
    with _kl_guard:
        lock = _kl_locks.get(key)
        if lock is None:
            lock = _kl_locks[key] = threading.Lock()
        return lock

# L4 事件里 wencai 查询的硬超时秒数(两条查询串行, 行情服务不通时不拖垮 summary)
WENCAI_TIMEOUT_S = 10.0

logger = logging.getLogger(__name__)

router = APIRouter()

# 盘口(thsdk 实时快照)硬超时秒数: 行情服务不通时单次可卡 30s, 超时即放弃并显式"无数据"
ORDERBOOK_TIMEOUT_S = 8.0

# 2026-08-20: summary 接口开盘后 20-30s(主力意图逐笔翻页), 与前端并发请求叠加
# 撞 Caddy 30s 反代超时 → 502 Bad Gateway。加 5min 进程内缓存 + PG 落库,
# 单次冷启动后秒回; 进程重启/容器迁移靠 summary_cache 表兜底(v0.4.77)。
from src.core.summary_cache import (  # noqa: E402
    get_cached_summary,
    put_cached_summary,
    clear_summary_cache as _clear_summary_cache,
)
from src.web.cache.biz_cache import biz_cache  # noqa: E402

_SUMMARY_CACHE: dict = {}  # noqa: F841 — 兼容旧测试引用; 实际存储已迁到 biz_cache
_SUMMARY_TTL = 300  # biz_cache / PG 双层 TTL 一致(秒)
_SUMMARY_PG_TTL = 300  # PG 落库 TTL 同 biz_cache, 双层一致
# P2-7 (2026-09-05 28号审计): 上限保留语义; biz_cache 自带 L1 上限(2000)
_SUMMARY_MAX = 500
# summary/batch 并发上限(2026-10-14 缓存收口): 每只 _compute_kline_summary 内部再开
# 4 条线程(主力意图/暗盘/三口径/图层并发), 上限 4 → 峰值 ~16 线程, 不把线程池打满。
_SUMMARY_BATCH_MAX_WORKERS = 4

# single-flight: cache_key -> Lock。并发 miss 时只有一个真正重算, 其余等锁后读缓存。
_summary_locks: dict[str, threading.Lock] = {}
_summary_locks_guard = threading.Lock()


def _summary_lock(cache_key: str) -> threading.Lock:
    with _summary_locks_guard:
        lk = _summary_locks.get(cache_key)
        if lk is None:
            lk = threading.Lock()
            _summary_locks[cache_key] = lk
        return lk


def _summary_cache_set(key: str, value) -> None:
    """写 L1 内存 + L2 Redis(biz_cache)。保留旧函数名以兼容既有调用方/测试。"""
    biz_cache.set_json(key, value, ttl=_SUMMARY_TTL)


def _clear_summary_caches() -> None:
    """清 summary 两层缓存(biz_cache L1+L2 Redis 与 PG summary_cache)。

    供测试隔离、数据源热切/补数后强刷使用。任一层不可用不影响另一层。
    """
    try:
        _clear_summary_cache()
    except Exception:  # noqa: BLE001
        pass


def clear_summary_biz_cache() -> None:
    """只清 summary 在 biz_cache 里的条目(不误伤其他业务缓存)。"""
    try:
        biz_cache.clear()
    except Exception:  # noqa: BLE001
        pass


class KlineItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")
    days: int | None = Field(default=60, description="K线天数")
    interval: str | None = Field(default="1d", description="周期: 1d/1w/1m")


#: sparkline 用的批量收盘: 一次最多几只(保护 DB)与结果缓存秒数(列表页 2 分钟足够新)
MAX_CLOSES_SYMBOLS = 60
_CLOSES_TTL = 120


class KlineBatchRequest(BaseModel):
    items: list[KlineItem]


class KlineSummaryItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")


class KlineSummaryBatchRequest(BaseModel):
    items: list[KlineSummaryItem]


def _parse_market(market: str) -> MarketCode:
    # 兼容数据源返回的交易所代码(SH/SZ/BJ 均属 A股 CN 市场)
    if (market or "").upper() in ("SH", "SZ", "BJ"):
        market = "CN"
    try:
        return MarketCode(market)
    except ValueError:
        raise HTTPException(400, f"不支持的市场: {market}")


def _fmt_date(d) -> str:
    """统一日期格式为 YYYY-MM-DD(前端 parseBusinessDay 正则要求带横杠)。

    fallback 联网源(腾讯/新浪/TQ)返回 '20260828' 8位无横杠, PG 路径返回 '2026-08-28'。
    klines 表缺失走 fallback 时, 8位日期若不转横杠 → 前端 parseBusinessDay 全 null
    → 日K被过滤空白(2026-08-30 线上 bug)。
    """
    s = str(d or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _serialize_klines(klines) -> list[dict]:
    return [
        {
            "date": _fmt_date(k.date),
            "open": k.open,
            "close": k.close,
            "high": k.high,
            "low": k.low,
            "volume": k.volume,
        }
        for k in klines
    ]


def _aggregate_klines(klines, interval: str) -> list:
    """Aggregate daily klines to week/month."""

    iv = (interval or "1d").lower()
    if iv in ("1d", "day", "d"):
        return klines
    if iv not in ("1w", "1m", "week", "month", "w", "m"):
        return klines

    parsed = []
    for k in klines or []:
        try:
            dt = datetime.strptime(k.date, "%Y-%m-%d")
        except Exception:
            continue
        parsed.append((dt, k))

    parsed.sort(key=lambda x: x[0])
    buckets: dict[str, list] = {}
    for dt, k in parsed:
        if iv in ("1w", "week", "w"):
            y, w, _ = dt.isocalendar()
            key = f"{y:04d}-W{w:02d}"
        else:
            key = f"{dt.year:04d}-{dt.month:02d}"
        buckets.setdefault(key, []).append((dt, k))

    out = []
    for _, items in buckets.items():
        items.sort(key=lambda x: x[0])
        first = items[0][1]
        last = items[-1][1]
        high = max(it[1].high for it in items)
        low = min(it[1].low for it in items)
        # 诚实口径(与同文件 KlineData.volume None 约定一致): 聚合区间内任一根
        # volume 缺失(None) → 聚合量也 None, 绝不把"无数据"当 0 求和(0 会伪装成缩量)。
        vols = [it[1].volume for it in items]
        vol = sum(vols) if vols and all(v is not None for v in vols) else None
        out.append(
            type(first)(
                date=items[-1][0].strftime("%Y-%m-%d"),
                open=first.open,
                close=last.close,
                high=high,
                low=low,
                volume=vol,
            )
        )
    out.sort(key=lambda k: k.date)
    return out


# 同一交易日多源时取优先级更高的一支(不混源成一条序列 —— 混源就是口径污染)。
_CLOSES_SOURCE_PREF = {"tencent": 0, "tq": 1, "sina": 2, "eastmoney": 3}


def _closes_from_rows(wanted: list[str], rows, days: int) -> tuple[list[dict], list[str]]:
    """(symbol, ts, close, source) 行 → 每只最后 days 个收盘价。

    纯函数(无 DB/网络): 便于逐条钉住口径 —— ① 同日多源只取一支; ② 少于 2 个点算"没有";
    ③ 缺数据进 missing 而不是补 0。
    """
    by_symbol: dict[str, dict[str, tuple[float, str]]] = {}
    for sym, ts, close, source in rows:
        if close is None:
            continue
        day = str(ts)[:10]
        cur = by_symbol.setdefault(sym, {})
        src = str(source or "?")
        prev = cur.get(day)
        if prev is None or _CLOSES_SOURCE_PREF.get(src, 9) < _CLOSES_SOURCE_PREF.get(prev[1], 9):
            cur[day] = (float(close), src)

    items: list[dict] = []
    missing: list[str] = []
    for sym in wanted:
        days_map = by_symbol.get(sym)
        if not days_map:
            missing.append(sym)
            continue
        ordered = sorted(days_map.items())[-days:]
        closes = [round(v[0], 3) for _, v in ordered]
        if len(closes) < 2:
            missing.append(sym)
            continue
        items.append(
            {
                "symbol": sym,
                "closes": closes,
                "asof": ordered[-1][0],
                "source": ordered[-1][1][1],
                "caliber": "pg_klines_hypertable",
            }
        )
    return items, missing


@router.get("/closes")
def get_closes_batch(
    symbols: str,
    market: str = "CN",
    days: int = 20,
):
    """批量"只要收盘价"的轻量接口(2026-09-22) —— 给列表行 sparkline 用。

    为什么单独开一个(而不是复用现成的两条):
    - `GET /klines/{symbol}` 一次一只 ⇒ 持仓/自选 73 行 = **73 次请求**, 列表页不能这么干;
    - `POST /klines/batch` 每项返回完整 OHLCV(含量与额), 但我们只要 20 个收盘价 ⇒ 传输/解析都浪费。

    口径(诚实优先):
    - **只读 PG hypertable**, 不触发联网抓取 —— 列表页不能因为某一行没缓存就拖慢整页(几十个请求串起来
      会变成几十秒)。库里没有的标的进 `missing`, **绝不补 0 / 绝不编造平线**(sparkline 会因此不画);
    - 一次最多 `MAX_CLOSES_SYMBOLS` 只(保护 DB); 结果按 (代码集, 天数, 市场) 缓存 `_CLOSES_TTL` 秒。
    """
    wanted = [x.strip() for x in (symbols or "").split(",") if x.strip()]
    if not wanted:
        raise HTTPException(400, "symbols 不能为空(逗号分隔)")
    if len(wanted) > MAX_CLOSES_SYMBOLS:
        raise HTTPException(400, f"一次最多查 {MAX_CLOSES_SYMBOLS} 只(收到 {len(wanted)})")
    days = max(2, min(days, 120))
    market_code = _parse_market(market)

    def _fetch() -> dict:
        """一次批量查完所有标的(2026-09-22 实测: 逐只查 3-7s/只, 批量 ANY() 3 只 0.02s)。

        口径三条(与测试一致, 改了就是 bug):
        1. **只读 PG, 绝不联网** —— 列表页几十行, 逐行联网会把页面拖成几十秒;
        2. **不挑单一数据源** —— 库里 tq 375 万行 / tencent 75 万行, 按 `source='tencent'` 过滤会让
           只有 tq 的票(如 002361)整只丢失; 同一交易日两源都有时按 tencent > tq > 其它 取一支
           (同一时刻只认一个来源, 不混源成一条序列 —— 混源就是口径污染);
        3. **缺数据的标的显式进 missing**, 绝不补 0 / 不编造平线。
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import bindparam, text

        try:
            from src.web.database import engine as _engine
        except Exception:
            return {"items": [], "missing": list(wanted), "days": days, "market": market.upper()}

        # 日历天 → 交易日: 20 个交易日大约跨 28-30 个自然日(周末+节假日); 取 3 倍留足余量,
        # 再截最后 days 个。**不是**直接拿 days 当自然日查(那样 20 天窗口只有 ~14 根 K 线)。
        lookback = datetime.now(timezone.utc) - timedelta(days=days * 3)
        try:
            with _engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT symbol, ts, close, source FROM klines "
                        "WHERE symbol IN :ss AND market=:m AND period='1d' AND adjust='qfq' "
                        "  AND ts >= :c ORDER BY symbol, ts ASC"
                    ).bindparams(bindparam("ss", expanding=True)),
                    {"ss": list(wanted), "m": market_code.value, "c": lookback},
                ).fetchall()
        except Exception:
            # 库不可用/表不存在 → 全部按"没有"处理(诚实: 不编造)
            return {"items": [], "missing": list(wanted), "days": days, "market": market.upper()}

        items, missing = _closes_from_rows(list(wanted), rows, days)
        return {"items": items, "missing": missing, "days": days, "market": market.upper()}

    key = f"kline_closes:{market.upper()}:{days}:{','.join(sorted(wanted))}"
    try:
        from src.web.cache.biz_cache import BizCache

        data = BizCache.instance().get_or_fetch(key, ttl=_CLOSES_TTL, fetch=_fetch)
    except Exception:  # 缓存不可用不影响主流程
        data = _fetch()
    return data


# ── 桩 bar 剔除 + 今日实时 bar 补齐 (2026-09-23 hotfix) ─────────────────────────
# 报障: "K线图不显示今日最新的日K"。
# 根因(生产实测): 库里当日那根日线是**盘前网关占位**——603629 的 09-23 行是
#   O=H=L=C=115.96(=前收), volume=0, source=tq。零振幅+零量的 bar 画出来是一条看不见的
#   横线 ⇒ 用户以为"今日日K没画"。且这根源数据还顶掉了"回落联网"的机会(它日期是今天,
#   厚度/新鲜度检查都通过), 于是图表永远停在这根假 bar 上。
#
# 处理: ① 末根若为桩(量 0/空 + 零振幅 + 且等于前收) → 剔除;
#       ② 剔掉后若"今天还没有真 bar"且今天是交易日 → 读时**合成**一根真实今日 bar;
#       ③ 拿不到实时数据就**不补**(宁缺勿造), 响应里用 today_bar/live_bar 显式说明性质。
#
# 2026-10-12 缺口治根: 合成口径下沉到 src/core/live_daily_bar(① PG 分钟线聚合当日
#   → ② 实时日线源(TQ get_market_data 1d)今日 bar → ③ 都缺不合成)。此前此处只走
#   KlineCollector(联网日线), 分钟线全天在库却没人读 → 盘后 15:00-18:00 仍无当日 K。
_LIVE_TODAY_CACHE: dict[str, tuple[float, object | None, dict | None]] = {}
_LIVE_TODAY_TTL = 60.0


def _is_stub_bar(cur, prev_close: float | None = None) -> bool:
    """桩 bar = 无量 + 零振幅(且价等于前收, 若给了前收)。停牌日的真 bar 也会零振幅,
    但它不满足"等于前收且量为 0 且是盘前写入"的全部特征 —— 所以只在**末根**上做这个判定。

    唯一实现下沉到 src/core/live_daily_bar.is_stub_bar(与读时合成同源, 避免两处漂移)。
    """
    from src.core.live_daily_bar import is_stub_bar

    return is_stub_bar(cur, prev_close)


def _is_degenerate_today_bar(cur, today: str) -> bool:
    """今日退化占位行(零振幅 + 零量, **不要求价等于前收**)。

    与 `_is_stub_bar` 的区别: 2026-10-15 生产实测 600111 的 10-15 行 O=H=L=C=34.08、
    量 0, 而前收 34.86 —— 网关盘前/集合竞价写入的占位价是**竞价指示价**, 不等于前收,
    故旧的「等于前收」子句漏判(实测 147 行中 144 行如此)。

    唯一实现下沉到 src/core/live_daily_bar.is_degenerate_placeholder(与写侧同源,
    避免两处漂移)。是否**真退化**还需排除停牌日, 见 `_finalize_daily_bars`。
    """
    from src.core.live_daily_bar import is_degenerate_placeholder

    return is_degenerate_placeholder(cur, today)


def _is_halted_today(symbol: str, market_code, today: str) -> bool:
    """symbol 在 today 是否停牌(`trading_halts` 表)。任何异常 → False(当作未停牌)。

    只在末根 = 今日且形态退化时被调用(见 `_finalize_daily_bars`), 不在热路径上;
    停牌表缺失/库不可达时**仍按退化剔除** —— 宁显"今日无真 bar"也绝不端出占位值。
    """
    try:
        from src.core.halts import is_halted

        return bool(is_halted(symbol, today, market=market_code.value))
    except Exception as e:  # noqa: BLE001
        logger.debug("[kline-degrade] 停牌判定失败 %s/%s: %r", symbol, today, e)
        return False


def _live_today_bar(symbol: str, market_code):
    """今日真实日 bar —— 读时合成(分钟线聚合 → 实时日线源), 60s 进程内缓存。

    合成口径见 src/core/live_daily_bar。取不到 → None(不编造)。合成 bar 的
    partial/source/as_of 标注经 _live_today_meta 读出, 供响应显式标『盘中未定型』。
    """
    key = f"{market_code.value}:{symbol}"
    hit = _LIVE_TODAY_CACHE.get(key)
    now = _time.monotonic()
    if hit and now - hit[0] < _LIVE_TODAY_TTL:
        return hit[1]
    bar = None
    meta = None
    try:
        from src.collectors.kline_collector import KlineData
        from src.core.live_daily_bar import synthesize_live_daily_bar

        lb = synthesize_live_daily_bar(symbol, market_code.value)
        if lb is not None:
            bar = KlineData(
                date=lb.date, open=lb.open, close=lb.close,
                high=lb.high, low=lb.low, volume=lb.volume,
            )
            meta = {
                "partial": bool(lb.partial),
                "source": lb.source or "",
                "as_of": lb.as_of or "",
                "amount": lb.amount,
            }
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).debug(f"[live-today-bar] {symbol}: {e!r}")
        bar = None
        meta = None
    _LIVE_TODAY_CACHE[key] = (now, bar, meta)
    return bar


def _live_today_meta(symbol: str, market_code) -> dict | None:
    """最近一次 _live_today_bar 的合成标注(partial/source/as_of); 未合成为 None。"""
    hit = _LIVE_TODAY_CACHE.get(f"{market_code.value}:{symbol}")
    if not hit or len(hit) < 3:
        return None
    return hit[2]


def _attach_live_bar(resp: dict, symbol: str, market_code, today_state) -> None:
    """today_state=='live' 时把合成标注挂到响应与末根 K(诚实标注『盘中未定型』)。

    读时合成值(分钟聚合/实时日线 forming)未定型, 前端据 ``live_bar.partial`` 显小字;
    18:00 批量定型后走 'pg' 分支, 不再附标注 —— 合成值被真实值取代(idempotent)。
    """
    if today_state != "live":
        return
    meta = _live_today_meta(symbol, market_code)
    if not meta:
        return
    resp["live_bar"] = {
        "partial": bool(meta.get("partial")),
        "source": meta.get("source") or "",
        "as_of": meta.get("as_of") or "",
    }
    ks = resp.get("klines") or []
    if ks:
        ks[-1]["partial"] = bool(meta.get("partial"))
        ks[-1]["source"] = meta.get("source") or ""
        ks[-1]["as_of"] = meta.get("as_of") or ""
        if meta.get("amount") is not None:
            ks[-1]["amount"] = meta["amount"]


def _annotate_index_today(klines, interval: str, market_code) -> tuple[str | None, dict | None]:
    """指数/板块日 K 的**同口径**今日标注。

    指数日 K 走腾讯/新浪实时源(无 PG 定型库, 不落 'pg'): 末根日期==今天 → 标
    ``today_bar='live'`` + ``live_bar``。``partial`` 只表示"当日仍在形成中"
    (交易时段内) —— 收盘后指数当日 K 已是终值, 标 partial=False, 不误报『未定型』。
    """
    iv = (interval or "1d").lower()
    if iv not in ("1d", "day", "d") or not klines:
        return None, None
    today = datetime.now(ZoneInfo(_APP_TZ)).date().isoformat()
    if str(getattr(klines[-1], "date", ""))[:10] != today:
        return None, None
    now = datetime.now(ZoneInfo(_APP_TZ))
    try:
        from src.models.market import MARKETS

        forming = MARKETS[market_code].is_trading_time(now)
    except Exception as e:  # noqa: BLE001 交易日历未覆盖年份等 → 不确定则不标 partial
        _log_idx = logging.getLogger(__name__)
        _log_idx.debug(f"[index live-bar] 时段判定异常, 按已定型处理: {e!r}")
        forming = False
    return "live", {
        "partial": bool(forming),
        "source": "live_index",
        "as_of": now.isoformat(timespec="seconds"),
    }


def _finalize_daily_bars(
    klines: list, symbol: str, market_code, interval: str, today: str | None = None
):
    """日报/日线序列收尾: 剔末根桩 + 剔今日退化占位行 + 补今日实时 bar。

    返回 (bars, today_state)。today_state ∈ {"pg"(库里有真今日bar), "live"(盘中补的实时bar),
    "missing"(今天还没有真bar且补不到 —— 前端应显式标注滞后, 不许当已收盘)}。

    剔除两类末根(2026-10-15 加固): ① `is_stub_bar`(量0+零振幅+**等于前收**);
    ② `is_degenerate_placeholder`(今日+零振幅+量0, **不要求等于前收**, 非停牌日) ——
    后者是网关盘前/集合竞价占位行(价=竞价指示价)的防线, 绝不把「0 量冒充真值」端出去。

    today: 可注入的"今天"(ISO 日期), 默认取 _APP_TZ 当天。
    **单测必须显式注入** —— 否则断言随运行日期漂移: 2026-09-23 实测, fixture 写死 09-23,
    跨到 09-24 后该文件一条用例永久红, 直接把发版堵死(CI 门禁红灯 → build 被 skip → 无镜像)。
    生产调用点不传, 行为不变。
    """
    iv = (interval or "1d").lower()
    if iv not in ("1d", "day", "d") or not klines:
        return klines, None

    bars = list(klines)
    prev_close = float(getattr(bars[-2], "close", 0) or 0) if len(bars) >= 2 else None
    dropped_stub = False
    if _is_stub_bar(bars[-1], prev_close if prev_close else None):
        bars = bars[:-1]
        dropped_stub = True

    today = today or datetime.now(ZoneInfo(_APP_TZ)).date().isoformat()
    last_date = str(getattr(bars[-1], "date", ""))[:10] if bars else ""

    # 2026-10-15 退化今日行防线(「0 量冒充真值」红线): 末根 = 今日 且 零振幅零量 =
    # 网关盘前/集合竞价占位行(**价 = 竞价指示价, 不等于前收**, 故 _is_stub_bar 的
    # 「等于前收」子句漏判; 生产实测 600111 的 10-15 行 O=H=L=C=34.08、量 0、前收 34.86)。
    # 非停牌日一律按「今日无真 bar」剔除 ⇒ 走下面的 live 合成(拿不到则显式 missing),
    # 绝不让占位价 + volume=0 以 today_bar='pg' 端出去。停牌日保留其真实(零成交)bar。
    if last_date == today and _is_degenerate_today_bar(bars[-1], today):
        if not _is_halted_today(symbol, market_code, today):
            bars = bars[:-1]
            dropped_stub = True
            last_date = str(getattr(bars[-1], "date", ""))[:10] if bars else ""

    if last_date == today:
        return bars, ("live" if dropped_stub else "pg")

    # 库里今天还没有真 bar: 交易日就补一根实时的(拿不到 → missing, 不编造)
    if not is_trading_day(date.fromisoformat(today)):
        return bars, "missing"
    live = _live_today_bar(symbol, market_code)
    if live is None:
        return bars, "missing"
    if str(getattr(live, "date", ""))[:10] <= last_date:
        return bars, "missing"
    bars.append(live)
    return bars, "live"


@router.get("/{symbol}/scenario")
def get_scenario_matrix(symbol: str, market: str = "CN", _: User = Depends(get_current_user)):
    """情景矩阵（结构化）：关键位 + 三档情景，供 K 线图上画线。

    与 chat 工具 `get_scenario_matrix` 同一套计算（`scenario_engine.build_matrix`），
    不预测方向，只给条件-反应；关键位不足时返回 available=False 并说明原因（不硬编）。
    """
    from src.core.scenario_engine import build_matrix
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        return build_matrix(db, symbol, market)
    finally:
        db.close()


@router.get("/{symbol}")
def get_klines(symbol: str, market: str = "CN", days: int = 60, interval: str = "1d", instrument: Literal["auto", "stock", "index"] = "auto"):
    """K 线主数据（60s 缓存 + 同 key 单飞，防图表轮询风暴打爆取数链）。"""
    key = f"{market}:{symbol}:{instrument}:{interval}:{days}"
    now = _time.time()
    hit = _kl_cache.get(key)
    if hit and now - hit[0] < _KL_TTL_S:
        return hit[1]
    with _kl_lock(key):
        hit = _kl_cache.get(key)          # 双重检查：等锁期间别人可能已算完
        if hit and _time.time() - hit[0] < _KL_TTL_S:
            return hit[1]
        data = (_get_klines_uncached(symbol, market, days, interval) if instrument == "auto"
                else _get_klines_uncached(symbol, market, days, interval, instrument=instrument))
        _kl_cache[key] = (_time.time(), data)
        return data


def _get_klines_uncached(symbol: str, market: str = "CN", days: int = 60, interval: str = "1d", instrument: str = "auto"):
    """获取单只股票/指数K线数据(指数代码自动识别,走指数K线源)"""
    market_code = _parse_market(market)
    # 指数识别: 已知指数代码 → 走指数K线(支持大盘详情页复用 InteractiveKline)
    from src.web.api.market import MARKET_INDICES

    is_index = instrument == "index" or (instrument == "auto" and any(idx["symbol"] == symbol for idx in MARKET_INDICES))
    if is_index:
        # 云服务器东财必失败(502) → 直接腾讯K线,避免每次白等 10s 超时
        import logging
        _log_k = logging.getLogger(__name__)

        idx_conf = next((i for i in MARKET_INDICES if i["symbol"] == symbol), None)
        tencent_code = idx_conf.get("tencent_symbol", "") if idx_conf else ""
        try:
            # 2026-08-13 修复: 裸 requests.get(timeout=8) 同步阻塞 asyncio 事件循环,
            # 海外节点 web.ifzq.gtimg.cn 偶发连接挂起(43.154.254.x HK CDN) → 事件循环堵死 → 全站无响应。
            # 改走 market_http: 短超时(5s)+ 退避重试, 失败快速抛错(不长时间卡住)。
            from src.collectors.market_http import market_get
            # v0.4.6.1: 腾讯被风控(501)会抛异常 — 单独捕获, 让新浪兜底有机会执行
            try:
                raw_resp = market_get(
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                    host_key="web.ifzq.gtimg.cn",
                    params={"param": f"{tencent_code},day,,,{days},qfq"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=5,
                    retries=1,
                    parse="json",
                    symbol=symbol,
                    log_label="腾讯指数K线",
                )
            except Exception as _tx_err:
                _log_k.warning(f"腾讯指数K线异常({tencent_code}): {_tx_err!r}, 尝试新浪兜底")
                raw_resp = None
            d = raw_resp
            data = (d.get("data") if isinstance(d, dict) else None) or {}
            data = data.get(tencent_code) or {}
            bars = data.get("day") or []
            if not bars and "." not in tencent_code and tencent_code[:2] in ("sh", "sz"):
                # v0.4.6 hotfix: 腾讯对生产云 IP 风控(501) → 新浪指数日K兜底(A股指数)
                import json as _json

                sina_resp = market_get(
                    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                    "CN_MarketData.getKLineData",
                    host_key="money.finance.sina.com.cn",
                    params={"symbol": tencent_code, "scale": "240", "ma": "no",
                            "datalen": str(min(max(days, 1), 1023))},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
                    timeout=8,
                    retries=1,
                    parse="text",
                    symbol=symbol,
                    log_label="新浪指数K线",
                )
                try:
                    sina_rows = _json.loads(sina_resp) if isinstance(sina_resp, str) else (sina_resp or [])
                except Exception:
                    sina_rows = []
                bars = [
                    [r.get("day"), r.get("open"), r.get("close"), r.get("high"),
                     r.get("low"), r.get("volume")]
                    for r in (sina_rows or [])
                    if isinstance(r, dict) and r.get("day")
                ]
            if not bars:
                raise RuntimeError(
                    f"指数K线不可用(腾讯+新浪均失败, {tencent_code})"
                )
            from src.collectors.kline_collector import KlineData

            raw = [
                KlineData(
                    date=b[0],
                    open=float(b[1]),
                    close=float(b[2]),
                    high=float(b[3]),
                    low=float(b[4]),
                    volume=float(b[5]) if len(b) > 5 else 0,
                )
                for b in bars
            ]
            klines = _aggregate_klines(raw, interval)
            body = {
                "symbol": symbol,
                "market": market_code.value,
                "days": days,
                "interval": interval,
                "klines": _serialize_klines(klines),
                "is_index": True,
            }
            # 指数同口径(2026-10-12): 末根为今日 → 标 live(+ partial 视交易时段)
            idx_state, idx_meta = _annotate_index_today(klines, interval, market_code)
            if idx_state:
                body["today_bar"] = idx_state
            if idx_meta:
                body["live_bar"] = idx_meta
                if body["klines"]:
                    body["klines"][-1].update({
                        "partial": idx_meta["partial"],
                        "source": idx_meta["source"],
                        "as_of": idx_meta["as_of"],
                    })
            return body
        except Exception as e:
            # ⚠️ 指数K线失败必须显式 fail: 否则会回退到股票K线分支,
            # 导致"上证指数"页面显示平安银行数据(代码 000001 都是它)
            _log_k.error(f"指数K线获取失败({symbol}/{tencent_code}): {e}")
            raise HTTPException(503, f"指数K线不可用({symbol}): {e}")

    # 1. 优先查 PG klines hypertable(快, ~70ms)
    pg_klines, pg_asof = _pg_klines(symbol, market_code, days)
    if pg_klines is not None:
        klines = _aggregate_klines(pg_klines, interval)
        # 2026-09-23: 剔末根桩 + 补今日实时 bar(否则当日零振幅占位 bar 让图上看不到今天的K)
        klines, today_state = _finalize_daily_bars(klines, symbol, market_code, interval)
        if klines:
            pg_asof = str(getattr(klines[-1], "date", "") or pg_asof)[:10]
        resp = {
            "symbol": symbol,
            "market": market_code.value,
            "days": days,
            "interval": interval,
            "klines": _serialize_klines(klines),
            "source": "pg_klines_hypertable",
            "asof": pg_asof,
            "today_bar": today_state,
        }
        # 2026-10-12: 合成今日 bar 显式标 partial/source/as_of(诚实区分子源)
        _attach_live_bar(resp, symbol, market_code, today_state)
        return resp

    # 2. Fallback: 联网拉 KlineCollector
    collector = KlineCollector(market_code)
    klines = collector.get_klines(symbol, days=days)
    klines = _aggregate_klines(klines, interval)
    klines, today_state_fb = _finalize_daily_bars(klines, symbol, market_code, interval)
    _fb = {
        "symbol": symbol,
        "market": market_code.value,
        "days": days,
        "interval": interval,
        "klines": _serialize_klines(klines),
    }
    if today_state_fb:
        _fb["today_bar"] = today_state_fb
    _attach_live_bar(_fb, symbol, market_code, today_state_fb)
    return _fb


def _pg_klines(symbol: str, market_code, days: int):
    """PG klines hypertable 直读(单股/batch 共用, P2-5/P2-6)。

    命中且厚度足够(≥min(30,days))且新鲜返回 (KlineData 列表, asof日期串),
    否则 (None, None) → 联网。

    2026-09-07 P2 陈旧快照 failover: 最新 bar 早于今天-12天(覆盖春节级长假)
    视为陈旧网关快照, 不再静默服务, 直接回落联网拿数。库表不存在/查询失败
    一律 (None, None)(静默 fallback)。

    2026-09-08 风险方案1.2/B1: 复权维度入列 —— 只读 adjust='qfq' 分区,
    前端图表/下游指标必须吃前复权序列, 严禁混入 none 原始价(0.7 勘查的
    混存污染形态)。旧库无 adjust 列时查询异常 → (None, None) → 联网。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    try:
        from src.web.database import engine as _engine

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with _engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume "
                    "FROM klines "
                    "WHERE symbol=:s AND market=:m AND period='1d' AND source='tencent' "
                    "  AND adjust='qfq' AND ts >= :c "
                    "ORDER BY ts ASC"
                ),
                {"s": symbol, "m": market_code.value, "c": cutoff},
            ).fetchall()
        if not rows:
            return None, None
        from src.collectors.kline_collector import KlineData

        klines = [
            KlineData(
                date=str(r[0])[:10],
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5] or 0),
            )
            for r in rows
        ]
        # v0.4.9.2: PG 命中但数据过薄视为无效 → 联网拿完整历史
        if len(klines) >= min(30, days):
            asof = max(k.date for k in klines)
            # 2026-09-23 修: 原来用 UTC 日期当"今天" ⇒ CST 00:00-08:00 期间会差一天,
            # 把滞后判定算错(lag 少 1 天)。统一用应用时区。
            today = datetime.now(ZoneInfo(_APP_TZ)).date().isoformat()
            from datetime import date as _date

            try:
                lag = (_date.fromisoformat(today) - _date.fromisoformat(asof[:10])).days
            except ValueError:
                return None, None
            if lag > 12:
                return None, None  # 陈旧快照 → 联网(与 TQ 陈旧快照同策略)
            return klines, asof
        return None, None
    except Exception:
        return None, None


@router.post("/batch")
def get_klines_batch(payload: KlineBatchRequest):
    """批量获取K线数据(P2-6: 与单股同口径, PG 优先再联网)"""
    if not payload.items:
        return []

    results = []
    for item in payload.items:
        market_code = _parse_market(item.market)
        days = item.days or 60
        interval = item.interval or "1d"
        # P2-6 (2026-09-05 28号审计): batch 与单股同口径, 先 PG 再联网
        pg_klines, pg_asof = _pg_klines(item.symbol, market_code, days)
        source = None
        asof = None
        if pg_klines is not None:
            klines = pg_klines
            source = "pg_klines_hypertable"
            asof = pg_asof
        else:
            collector = KlineCollector(market_code)
            klines = collector.get_klines(item.symbol, days=days)
        klines = _aggregate_klines(klines, interval)
        klines, today_state_b = _finalize_daily_bars(klines, item.symbol, market_code, interval)
        row = {
            "symbol": item.symbol,
            "market": market_code.value,
            "days": days,
            "interval": interval,
            "klines": _serialize_klines(klines),
        }
        if today_state_b:
            row["today_bar"] = today_state_b
        _attach_live_bar(row, item.symbol, market_code, today_state_b)
        if source:
            row["source"] = source
        if asof:
            row["asof"] = asof
        results.append(row)

    return results


def _tencent_code(code: str) -> str | None:
    """6 位 A 股代码 → 腾讯风格代码(sz/sh 前缀), 无法识别返回 None。
    与 src/core/dark_pool_flow._tencent_code 同口径, 本地实现避免跨模块引用私有函数。"""
    code = (code or "").strip()
    if code[:2].lower() in ("sz", "sh", "bj"):
        return code.lower()
    if code.isdigit() and len(code) == 6:
        if code[0] in ("6", "9") or code.startswith("688"):
            return f"sh{code}"
        if code[0] in ("0", "2", "3"):
            return f"sz{code}"
    return None


def _resonance_default() -> dict:
    """resonance 字段安全默认(三指标不可得/计算失败时, 不编造)。

    与字段契约一致: row=0 / phase="无" / action_label="观望",
    action_text/tone 取 state_action_label(0) 的文案与色彩语义。
    """
    return {
        "available": False,
        "row": 0,
        "phase": "无",
        "action_label": "观望",
        "action_text": "趋势不明, 建议观望",
        "tone": "neutral",
        "bad_count": 0,
    }


def _build_resonance(symbol: str, bars: list[dict]) -> dict:
    """数智决策三指标共振状态(2026-09-02 暴露到 summary, 字段契约见前端消费方)。

    三指标当日最新值, 全部复用已有能力, 不重写算法:
      - 趋势  : gs_state.trend_label(bars) (单一事实源: 与 K 线图层/数智决策面板同源)
      - 活跃度: ai_activity.eval_activity(bars); activity_prev = eval_activity(bars[:-1])
                (砍末根重算, 纯计算无网络; 拿不到 → None, "较前日翻倍"按不满足处理)
      - 资金  : dark_pool_flow.compute_pool_flow(symbol).main_net(明+暗主力净额);
                fund_net_prev 历史明盘不可得 → 恒 None(官方口径, 不编造)

    任一指标缺失 → available=False + 安全默认, 不编造。
    全程 try/except 兜底: 任何异常都不允许拖垮 summary 接口。
    """
    out = _resonance_default()
    try:
        if not bars:
            return out

        from src.core.gs_state import trend_label
        from src.core.ai_activity import eval_activity
        from src.core.dark_pool_flow import compute_pool_flow
        from src.core.resonance import evaluate_state, state_action_label

        # ① 趋势(GS): 复用 layer 已拉的 bars —— 单一事实源 gs_state(与 K 线图层同源;
        #    末根疑似反向交叉不再抢跑区间)
        trend = trend_label(bars)

        # ② 活跃度: 当日 + 前一日(砍末根, 纯计算)
        act = eval_activity(bars)
        activity = act.get("activity") if isinstance(act, dict) else None
        activity_prev = None
        if len(bars) > 1:
            try:
                prev = eval_activity(bars[:-1])
                activity_prev = prev.get("activity") if isinstance(prev, dict) else None
            except Exception:  # noqa: BLE001
                activity_prev = None

        # ③ 资金(主力净额 = 明+暗): 独立兜底, 失败只缺资金不拖垮整体
        fund_net = None
        try:
            pf = compute_pool_flow(symbol)
            if isinstance(pf, dict):
                fund_net = pf.get("main_net")
        except Exception as e:  # noqa: BLE001
            logger.debug("resonance compute_pool_flow %s failed: %s", symbol, e)

        # 三指标必须**全部可得**才算 available(缺任一 → 安全默认, 不编造)
        available = (
            trend not in (None, "", "无数据")
            and isinstance(activity, (int, float))
            and isinstance(fund_net, (int, float))
        )
        if not available:
            return out

        st = evaluate_state(trend, activity, activity_prev, fund_net, None)
        al = state_action_label(st.get("row"))
        out.update({
            "available": True,
            "row": int(st.get("row") or 0),
            "phase": st.get("phase") or "无",
            "action_label": al.get("label"),
            "action_text": al.get("text"),
            "tone": al.get("tone"),
            "bad_count": int(st.get("bad_count") or 0),
        })
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("resonance %s failed: %s", symbol, e)
        return _resonance_default()


def _build_orderbook(symbol: str):
    """盘口队列 + 托压单(设计稿 §3.1)。

    优先后台采集的**盘口快照**(够新, 2026-10-14); 无/超龄 → .img 离线文件(完整委托队列);
    再退 thsdk 实时快照; 都拿不到 → None(显式无数据, 不编造)。

    2026-10-01 (B1 P2 首屏慢): 从 _build_layer_data 抽出成独立函数, 以便与其余
    图层计算并发(盘口 thsdk 快照硬超时 8s, 是冷启动链里最长的几段之一)。

    2026-10-14 盘口转后台采集: 读快照优先, 命中够新快照时**完全不打 thsdk**, 把 8s 硬超时
    移出请求链; 返回 `source='snapshot'` + `snapshot_age_sec` 陈旧度标注。超龄/无快照才
    回退 .img / thsdk(语义不变)。
    """
    try:
        from src.core import orderbook_engine as obe

        # ① 读快照优先: 够新 → 直接由最新帧算盘口队列(不打 thsdk)
        try:
            from src.core import orderbook_snapshot as obsnap

            snap_meta = obsnap.fresh_snapshot(symbol)
            if snap_meta:
                frames = snap_meta.get("frames") or []
                latest = obsnap.rebuild_snapshot(frames[-1]) if frames else None
                if latest is not None:
                    ob = obe.order_book_queue(latest)
                    if ob and ob.get("available"):
                        return {
                            **ob,
                            "source": "snapshot",
                            "snapshot_age_sec": snap_meta.get("snapshot_age_sec"),
                        }
        except Exception as e:  # noqa: BLE001  快照读取失败 = 未命中, 回退既有路径
            logger.debug("orderbook %s 读快照失败(回退): %s", symbol, e)

        ob = None
        img_path = obe.find_img_file(symbol, "CN")
        if img_path:
            snaps = obe.load_snapshots_from_img(img_path)
            if snaps:
                snap = snaps[-1]  # 最新一帧
                ob = obe.order_book_queue(snap)
                if ob is not None:
                    return {**ob, "img_path": img_path}
        if ob is None:
            # 盘后/非交易时段: 若未命中够新快照，直接读最近收盘快照，绝不向外打 thsdk 实时阻塞 8~25s
            from src.core.quote_snapshots import in_trading_window
            if not in_trading_window():
                try:
                    from src.core import orderbook_snapshot as _obs
                    snap_meta = _obs.read_snapshot(symbol)
                    if snap_meta:
                        frames = snap_meta.get("frames") or []
                        latest = _obs.rebuild_snapshot(frames[-1]) if frames else None
                        if latest is not None:
                            ob = obe.order_book_queue(latest)
                            if ob and ob.get("available"):
                                return {
                                    **ob,
                                    "source": "snapshot",
                                    "snapshot_age_sec": snap_meta.get("snapshot_age_sec"),
                                    "note": "非交易时段·显示历史收盘快照",
                                }
                except Exception:
                    pass
                return {"available": False, "note": "非交易时段·无可用盘口快照"}
            # 三轮退避就是 90s, 会把 summary 接口拖到反代超时。加硬超时护栏:
            # 超时即放弃并显式"无数据", 绝不阻塞主链路。
            # 2026-09-18: 走共享硬超时(并发槽+wait=False)。此前
            # `with ThreadPoolExecutor(max_workers=1)` 在 result(timeout) 超时后
            # 仍会 shutdown(wait=True) 等挂死线程, 硬超时形同虚设。
            from src.core.thsdk_breaker import call_with_hard_timeout

            snap = call_with_hard_timeout(
                lambda: obe.fetch_snapshot(obe.to_ths_code(symbol) or ""),
                default=None,
                timeout_s=ORDERBOOK_TIMEOUT_S,
            )
            ob = obe.order_book_queue(snap)
        return ob
    except Exception as e:  # noqa: BLE001
        logger.debug("orderbook %s failed: %s", symbol, e)
        return None


def _latest_settled_bar_date(bars: list[dict], now=None) -> str | None:
    """bars 里**最近一根已定型日 K** 的日期('YYYY-MM-DD'); 取不到 → None。

    定型 = 该交易日的日 K 不再变化(历史日 / 今日过了收盘定型时刻 15:05)。
    盘中今日那根 live 合成 bar 尚未定型, 故排除 —— 它是"当日实时块", 不能拿它去
    核对预落库日级块的新鲜度(见 src/core/layer_precompute.usable)。
    """
    from src.core.layer_precompute import is_settled

    for b in reversed(bars or []):
        d = _norm_date(b.get("date") if isinstance(b, dict) else b)
        if d and is_settled(d, now):
            return d
    return None


def _compute_daily_blocks(symbol: str, bars: list[dict]) -> dict:
    """**日级派生块**现算(收盘定型后即为该日定稿, 见 layer_precompute 模块头)。

    - gs_signals: 全量 GS 交叉序列(收盘定死=confirmed, 末根疑似=待确认)
    - fund_flow: 日级, 长度对齐 klines; dark_net=OHLC 分摊(L1 近似对照项),
      ming_net=当日 big_order_flow 全口径(历史逐日明盘无数据, 显式 null)
    - activity_series: 活跃度日级序列(活跃度副图柱载体), 每点 {date, activity, level}
    - events: 涨停/跌停(K线自算) + .tck 拆单撤单 + wencai 龙虎榜/公告
    - unlock_levels / chips: 标准筹码接口(腾讯当日分价表优先 / 新浪历史分价兜底)
    - resonance: 三指标共振状态, 复用本函数已拉的 bars

    仅 A 股; 任一字段计算失败独立降级为 None/默认, 不拖垮整体, 不编造。
    orderbook(实时块)不在此函数内 —— 它在 `_build_layer_data` 里与这里并发跑且从不落库。
    """
    out: dict = {"gs_signals": None, "fund_flow": None, "activity_series": None,
                 "events": None, "unlock_levels": None, "chips": None,
                 "main_accumulation_chips": None,
                 "resonance": _resonance_default()}

    # ① gs_signals(依赖 bars: bars 空时跳过, 保持 None 由前端显式"无数据")
    if bars:
        try:
            from src.core.gs_strategy import compute_gs_signals

            out["gs_signals"] = compute_gs_signals(bars)
        except Exception as e:  # noqa: BLE001
            logger.debug("gs_signals %s failed: %s", symbol, e)

    # ② fund_flow(日级, 依赖 bars)
    if bars:
        try:
            from src.core.ohlc_dark import allocate_bar

            fund_flow = []
            for b in bars:
                a = allocate_bar(
                    o=b.get("open"), h=b.get("high"), l=b.get("low"),
                    c=b.get("close"), volume=b.get("volume"),
                    date=str(b.get("date", "")),
                )
                fund_flow.append({
                    "date": _norm_date(b.get("date")),
                    "ming_net": None,  # 历史逐日明盘: 无数据(big_order_flow 仅当日)
                    "dark_net": round(a.net, 2) if a else None,
                })
            # 当日明盘(big_order_flow 全口径, 与官方扩展1一致; 失败保持 null)
            try:
                from src.core import dark_l2, dark_split

                tc = _tencent_code(symbol)
                if tc:
                    ticks = dark_l2.fetch_l2_ticks(tc, "thsdk_big_order")
                    # 09-03: 明盘大单顺手落库(回测回查; best-effort, 唯一索引去重)
                    try:
                        from src.core.history_store import persist_l2_ticks

                        persist_l2_ticks(symbol, "CN", "thsdk_big_order", ticks)
                    except Exception:  # noqa: BLE001
                        pass
                    ming = dark_split.ming_net_from_big_orders(ticks)
                    if ming["count"] > 0 and fund_flow:
                        fund_flow[-1]["ming_net"] = ming["ming_net"]
            except Exception:  # noqa: BLE001
                pass
            out["fund_flow"] = fund_flow
        except Exception as e:  # noqa: BLE001
            logger.debug("fund_flow %s failed: %s", symbol, e)

    # ②b activity_series(活跃度副图柱载体, 日级, 依赖 bars; 与 fund_flow 同门控/同降级)
    if bars:
        try:
            from src.core.ai_activity import activity_series as _activity_series

            out["activity_series"] = [
                {"date": _norm_date(p.get("date")),
                 "activity": p.get("activity"), "level": p.get("level")}
                for p in _activity_series(bars)
            ]
        except Exception as e:  # noqa: BLE001
            logger.debug("activity_series %s failed: %s", symbol, e)

    # ③ events(L4 事件标注): 涨停/跌停(K线自算) + 五类真实数据源
    #    kind 取值与前端 InteractiveKline 的 KlineEventKind 一一对应。
    #    任一数据源不可用 → 该类事件为空, 不编造(§5 诚实口径)。
    try:
        events = _build_events(symbol, bars)
        out["events"] = events
    except Exception as e:  # noqa: BLE001
        logger.debug("events %s failed: %s", symbol, e)

    # ④ 解套盘位 + 筹码结构: 复用**标准筹码接口** chip_distribution, 不自算
    #    腾讯当日分价表优先 / 新浪历史分价兜底; 取不到 → None(显式无数据)。
    try:
        from src.core.l4_events import chip_levels, unlock_levels_from_chips
        from src.core.chip_distribution import compute_main_accumulation_chips

        tc = _to_tencent(symbol)
        chips = chip_levels(tc)
        out["chips"] = chips
        out["unlock_levels"] = unlock_levels_from_chips(chips) or None

        # 主力建仓期筹码分布模型 (穿透 120 交易日建仓区间沉淀筹码)
        if bars and len(bars) >= 20:
            out["main_accumulation_chips"] = compute_main_accumulation_chips(bars, lookback_days=120)
        else:
            out["main_accumulation_chips"] = None
    except Exception as e:  # noqa: BLE001
        logger.debug("chips %s failed: %s", symbol, e)

    # ⑤ 三指标共振(依赖 bars: 趋势/活跃度纯计算复用已拉 bars; 资金走 compute_pool_flow)
    #    独立兜底在 _build_resonance 内部, 失败 → available:False 安全默认, 不拖垮整体。
    if bars:
        try:
            out["resonance"] = _build_resonance(symbol, bars)
        except Exception as e:  # noqa: BLE001
            logger.debug("resonance %s failed: %s", symbol, e)
            out["resonance"] = _resonance_default()
    return out


def build_layer_daily_blocks(
    symbol: str, market_code: MarketCode, days: int = 120
) -> tuple[str | None, dict]:
    """批算入口(供 `src.core.layer_precompute` 收盘预热): 取 bars → 算日级块 → (asof, blocks)。

    与请求期用**同一个** `_compute_daily_blocks`, 保证预落库载荷与现算逐字段一致;
    不碰 orderbook(实时块, 不落库), 故批算不会打 thsdk 盘口。
    取不到 bars → (None, {}), 调用方计 skipped(不编造)。
    """
    if market_code.value != "CN":
        return None, {}
    try:
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, "CN", days=days)
    except Exception as e:  # noqa: BLE001
        logger.debug("layer 批算 fetch_bars %s failed: %s", symbol, e)
        bars = []
    if not bars:
        logger.warning("layer 批算 %s bars 为空: 跳过(不编造)", symbol)
        return None, {}
    return _norm_date(bars[-1].get("date")), _compute_daily_blocks(symbol, bars)


def _build_layer_data(symbol: str, market_code: MarketCode, now=None) -> dict:
    """P1 图层数据: **日级派生块读库优先 + 仅当日实时块现算** + 实时盘口。

    2026-10-14 (首屏 P0 26s→秒开): 日级派生块(gs_signals/fund_flow/activity/events/
    unlock_levels/chips/resonance)此前**每请求全量现算**, 其中 chips/wencai/pool_flow
    是网络调用, 冷启动 ~26s 撞前端 20s abort。现在:

      ① 读库命中(`layer_precompute.usable`: 行已定型 且 覆盖最近已定型交易日 且
         当前不在盘中窗口)⇒ 日级块**零现算**, 直接回预落库载荷;
      ② 未命中(盘中当日块未定型 / 无行 / 行过旧 / 读库失败)⇒ 走现行全路径
         `_compute_daily_blocks` 现算(**口径逐字段不变**), 算完若该日已定型则落库,
         下次请求直接命中(见 `src/core/layer_precompute.py` 模块头)。
      ③ orderbook 是**实时块**(盘口队列/托压单), 从不落库, 与上面并发跑、末尾 join。

    仅 A 股; 任一字段计算失败独立降级为 None/默认, 不拖垮整体, 不编造。
    `now` 仅测试注入(时钟敏感: 定型判定/盘中窗口)。
    """
    out: dict = {"gs_signals": None, "fund_flow": None, "events": None,
                 "orderbook": None, "unlock_levels": None, "chips": None,
                 "activity_series": None, "main_accumulation_chips": None,
                 "resonance": _resonance_default()}
    if market_code.value != "CN":
        return out
    # ⓪ orderbook 与 bars 无关, 先用独立线程跑起来(末尾 join), 与下面整段重叠,
    #    把盘口 thsdk 的硬超时(ORDERBOOK_TIMEOUT_S)从串行链里挪走。
    _ob_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _ob_future = _ob_pool.submit(_build_orderbook, symbol)
    try:
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, "CN", days=120)
    except Exception as e:  # noqa: BLE001
        logger.debug("layer_data fetch_bars %s failed: %s", symbol, e)
        bars = []
    if not bars:
        # 2026-09-02 修: bars 为空(多 vendor 全挂)时**不再整体早退**。
        # orderbook / .tck 拆单撤单 / wencai 龙虎榜公告 / 筹码解套盘位 都不依赖 bars,
        # 早退会把它们一起拖成 None → 前端"整页无数据"(2026-09-02 生产实测)。
        # 仅 gs_signals / fund_flow 依赖 bars, 下面各自用 `if bars:` 门控跳过。
        logger.warning("layer_data %s bars 为空: gs_signals/fund_flow 跳过, 其余仍计算", symbol)

    # ⓪ orderbook: 盘口队列 + 托压单 —— 已在函数开头并发启动(_build_orderbook),
    #    末尾 join。此处不再串行调用(2026-10-01 B1 首屏慢)。

    # ①-⑤ 日级派生块: 读库优先(收盘批算/上次请求落库) → miss 才现算
    hit_row = None
    if bars:
        try:
            from src.core import layer_precompute as lp

            latest_settled = _latest_settled_bar_date(bars, now)
            row = lp.get_layer_daily(symbol, market_code.value)
            if lp.usable(row, latest_settled, now):
                hit_row = row
        except Exception as e:  # noqa: BLE001 — 读库/门禁失败 = 未命中, 回现算(不阻塞)
            logger.debug("layer_data %s 日级块读库失败(回现算): %s", symbol, e)

    if hit_row is not None:
        out.update(hit_row["blocks"])
        logger.debug(
            "layer_data %s 命中日级预落库(asof=%s source=%s): 零现算",
            symbol, hit_row.get("asof"), hit_row.get("source"),
        )
    else:
        try:
            daily = _compute_daily_blocks(symbol, bars)
        except Exception as e:  # noqa: BLE001 — 现算整体失败: 保持显式"无数据", 不阻塞
            logger.debug("layer_data %s 日级块现算失败: %s", symbol, e)
            daily = {}
        out.update(daily)
        # 落库(best-effort): 定型日才算完可作定稿; 未定型的当日行只留"最近访问"痕迹
        try:
            from src.core import layer_precompute as lp

            asof = _norm_date(bars[-1].get("date")) if bars else None
            if asof:
                lp.put_layer_daily(
                    symbol, market_code.value, asof, daily,
                    source=lp.SOURCE_REQUEST, settled=True, now=now,
                )
        except Exception as e:  # noqa: BLE001 — 落库失败不影响响应
            logger.debug("layer_data %s 日级块落库失败(不影响响应): %s", symbol, e)

    # ⓪ join 并发跑的 orderbook(与上面整段重叠, 冷启动从串行相加变为取较长者)
    try:
        out["orderbook"] = _ob_future.result()
    except Exception as e:  # noqa: BLE001
        logger.debug("orderbook %s failed: %s", symbol, e)
        out["orderbook"] = None
    finally:
        _ob_pool.shutdown(wait=False)
    return out


def _norm_date(d) -> str | None:
    """日期统一规范化为 'YYYY-MM-DD'。

    2026-09-02 v0.4.55 生产实测: 同一 date 字段在不同数据源下**格式不一致** ——
      - 通达信 TQ 日K  → `20260827`
      - 东财 / 新浪日K → `2026-08-27`
      - .tck 文件名    → `20260827`(由 `_date_from_tck_name` 提取)
    混用会让前端做日期匹配/排序时错乱, 且主源从 TQ 降级到东财时**日期样式会突然变化**。
    故统一在此规范化; 无法识别的输入原样返回(不猜、不补假日期)。
    """
    s = str(d or "").strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _date_from_tck_name(path: str) -> str | None:
    """.tck 文件名里的交易日(YYYYMMDD) → 'YYYY-MM-DD'; 文件名无日期返回 None。

    .tck 是盘后落盘文件(如 `sz002361_20260827.tck`), 文件名带交易日。
    事件日期必须以它为准: 否则沿用 bars 末根日期(今日)会把 8-27 的拆单/撤单
    标成今天, 前端把历史事件画到今日 K 线上(日期错位)。
    """
    import os
    import re

    m = re.search(r"(\d{8})", os.path.basename(path or ""))
    if not m:
        return None
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _build_events(symbol: str, bars: list[dict]) -> list[dict]:
    """汇总 L4 事件: 涨停/跌停(K线自算) + 四类真实数据源.

    数据源与缺数处理(全部"缺即空", 不编造):
      - 涨停/跌停  : bars 自算, 阈值 ±9.8%(与前端 InteractiveKline 同口径)
      - 拆单簇/撤单: .tck 文件(需 SIDA_TCK_DIR), 无文件 → 空
      - 龙虎榜/公告: wencai(thsdk), 不可用 → 空
      - 我的买卖点: paper_trading 交割单(DB), 无记录 → 空

    ⚠️ wencai 走 thsdk, 行情服务不通时会卡住(实测单次 30s)。
    summary 接口有 5 分钟缓存, 但仍加硬超时护栏避免拖垮反代。
    """
    events: list[dict] = []

    # (1) 涨停 / 跌停(K线自算)
    from_idx = max(0, len(bars) - 60)
    for i in range(from_idx, len(bars)):
        prev = bars[i - 1] if i > 0 else None
        if not prev or not prev.get("close"):
            continue
        chg = (bars[i]["close"] - prev["close"]) / prev["close"] * 100
        if chg >= 9.8:
            events.append({"date": _norm_date(bars[i].get("date")), "kind": "limit_up", "label": "涨停"})
        elif chg <= -9.8:
            events.append({"date": _norm_date(bars[i].get("date")), "kind": "limit_down", "label": "跌停"})

    from src.core import l4_events

    # (2) 拆单簇 / 撤单异常(.tck)—— 不依赖 bars, bars 为空时按最新 .tck 文件照算
    try:
        from src.core.tdx_tick_parser import parse_tck

        tck_date = str(bars[-1].get("date", "")) if bars else ""
        tck_path = l4_events.find_tck_file(symbol, tck_date) or l4_events.find_tck_file(symbol)
        if tck_path:
            ev_date = _date_from_tck_name(tck_path) or tck_date
            trades, _orders, cancels = parse_tck(tck_path)
            events.extend(l4_events.split_clusters(trades, ev_date))
            events.extend(l4_events.cancel_anomalies(cancels, ev_date))
    except Exception as e:  # noqa: BLE001
        logger.debug(".tck 事件 %s failed: %s", symbol, e)

    # (3) 龙虎榜 / 公告(wencai)—— 加硬超时, 行情服务不通时不拖垮接口
    # 2026-09-18: 同上, 走共享硬超时护栏(防 with-TPE 超时后仍等挂死线程)
    try:
        from src.core.thsdk_breaker import call_with_hard_timeout

        today = datetime.now().strftime("%Y-%m-%d")
        pairs = call_with_hard_timeout(
            lambda: _wencai_event_pairs(symbol, today),
            default=None,
            timeout_s=WENCAI_TIMEOUT_S,
        )
        if pairs:
            events.extend(pairs)
    except Exception as e:  # noqa: BLE001
        logger.debug("wencai 事件 %s failed: %s", symbol, e)

    # (4) 我的买卖点: 按用户要求**先不做**(2026-09-01)
    #     交割单是账户级数据, summary 接口不区分 user_id, 多用户会串号;
    #     等接口透传 user_id 后再接。暂不产出 my_trade 事件。

    # 过滤掉日期为空的事件(数据库脏数据不该污染前端)
    return [e for e in events if e.get("date")]


def _wencai_event_pairs(symbol: str, date_: str) -> list[dict]:
    """一次线程里跑完龙虎榜 + 公告两条 wencai 查询(供超时护栏包裹)."""
    from src.core import l4_events

    out: list[dict] = []
    out.extend(l4_events.dragon_tiger_events(symbol, date_))
    out.extend(l4_events.announcement_events(symbol, date_))
    return out


def _to_tencent(symbol: str) -> str:
    """A 股 symbol → 腾讯代码(sh/sz 前缀). 已在 main 项目内, 简单实现即可."""
    s = (symbol or "").strip().lower()
    if not s:
        return s
    if s.startswith(("sh", "sz")):
        return s
    if s.startswith("6"):
        return "sh" + s
    if s.startswith(("0", "3")):
        return "sz" + s
    return s


@router.get("/{symbol}/l2-ticks")
def get_l2_ticks_history(
    symbol: str,
    market: str = "CN",
    days: int = 5,
    fetch: int = 0,  # v0.4.77: 默认 0(只读库), 实时拉走后台 cron(5min);
                    #       前端按 1=触发主动抓取, 不再默认每请求实时拉
):
    """L2 逐笔落库回查(v0.4.77 fetch 默认 0, 只读库)。

    fetch=1: 先实时拉一份落库再返回库里序列(主动抓取, 前端"刷新"按钮用)。
    fetch=0(默认): 只读库, 不触发外部拉取(纯回查, 防每请求都拉 thsdk 撞 30s 超时)。
    空=尚无落库, 不编造。
    """
    from src.core.history_store import persist_l2_ticks, query_l2_ticks

    market_code = _parse_market(market)
    stored = []
    if fetch:
        try:
            from src.core import dark_l2

            tc = _tencent_code(symbol)
            if tc:
                ticks = dark_l2.fetch_l2_ticks(tc, "thsdk_big_order")
                persist_l2_ticks(symbol, market_code.value, "thsdk_big_order", ticks)
        except Exception:  # noqa: BLE001
            pass
    stored = query_l2_ticks(symbol, market_code.value, days)
    return {"symbol": symbol, "market": market_code.value, "count": len(stored), "rows": stored}


def _enrich_main_intent(symbol: str, market_code: MarketCode):
    """主力意图+筹码(腾讯逐笔口径) 字符串 + 结构化; 非 A 股 / 失败 → (None, None)。

    2026-08-12 性能优化: 一次 compute_dark_flow 同时产出字符串+结构化,
    避免 summary+structured 各调一次(逐笔翻页/分价表各跑一遍)。
    """
    if market_code.value != "CN":
        return None, None
    try:
        from src.agents.intraday_monitor import _main_intent_both
        return _main_intent_both(symbol)
    except Exception:  # noqa: BLE001
        main_intent = None
        main_intent_structured = None
        try:
            from src.agents.intraday_monitor import _main_intent_summary
            main_intent = _main_intent_summary(symbol)
        except Exception:  # noqa: BLE001
            main_intent = None
        try:
            from src.agents.intraday_monitor import _main_intent_structured
            main_intent_structured = _main_intent_structured(symbol)
        except Exception:  # noqa: BLE001
            main_intent_structured = None
        return main_intent, main_intent_structured


def _enrich_dark_clusters(symbol: str) -> dict:
    """A4 拆单簇暗盘资金(委托号级拆单簇)。数据源不可用 → available:false(§12, 不编造)。"""
    dark_clusters: dict = {"available": False, "note": "未接入"}
    try:
        from src.core.postmarket_review import dark_review_from_tck

        review = dark_review_from_tck(symbol)
        dark_ = review.get("dark") or {}
        ming_ = review.get("ming") or {}
        dark_clusters = {
            "available": bool(review.get("available")),
            "dark_net": dark_.get("net"),        # 拆单簇暗盘净额(元)
            "dark_buy": dark_.get("buy"),
            "dark_sell": dark_.get("sell"),
            "cluster_count": len(review.get("clusters") or []),
            "ming_net": ming_.get("net"),        # 明盘(单笔>30万)净额(元)
            "main_net": review.get("main_net"),  # 明+暗主力净额(元)
            "cancel_rate": review.get("cancel_rate"),
            "active_passive_ratio": review.get("active_passive_ratio"),
        }
        if review.get("note"):
            dark_clusters["note"] = review.get("note")
    except Exception as e:  # noqa: BLE001
        logger.debug("dark_clusters summary 失败 %s: %s", symbol, e)
    return dark_clusters


def _enrich_mainflow_tri(symbol: str, market_code: MarketCode) -> dict:
    """A4 明盘三源交叉验证置信度徽章(A/B/C)。失败 → 安全默认, 不编造。"""
    mainflow_tri: dict = {"agree": None, "consensus_wan": None, "spread_pct": None, "n_ok": 0, "sources": {}}
    if market_code.value != "CN":
        return mainflow_tri
    try:
        from src.core.mainflow_tri import triangulate
        mainflow_tri = triangulate(symbol)
    except Exception as e:  # noqa: BLE001
        logger.debug("mainflow_tri summary 失败 %s: %s", symbol, e)
    try:
        from src.core.confidence import payload as _confidence_payload
        mainflow_tri = {**mainflow_tri, **_confidence_payload(mainflow_tri)}
    except Exception:  # noqa: BLE001
        pass
    return mainflow_tri


def _enrich_cancel_order_intent(symbol: str) -> dict:
    """微观高频撤单意图分析 (基于通达信 TQ get_more_info 与广发金工方法论)。"""
    try:
        from marketdata.vendors.tq import _rpc
        from src.core.cancel_order_intent import calc_cancel_order_intent

        tq_code = symbol if ("." in symbol) else (f"{symbol}.SH" if symbol.startswith("6") else f"{symbol}.SZ")
        more_info = _rpc("get_more_info", {"stock_code": tq_code})
        chg = None
        if isinstance(more_info, dict) and "ZAF" in more_info:
            try:
                chg = float(more_info["ZAF"])
            except Exception:
                chg = None
        return calc_cancel_order_intent(more_info, pct_chg=chg)
    except Exception as e:
        return {
            "available": False,
            "error": str(e)[:120],
            "intent_tag": "无数据",
            "intent_desc": "撤单盘口数据不可用",
            "warning_level": "safe",
        }


def _compute_kline_summary(symbol: str, market_code: MarketCode) -> dict:
    """真正重算 summary(冷启动链)。

    2026-10-01 (B1 P2 首屏慢): 把多个**互不依赖**的重活并发化。原实现串行相加:
        main_intent(逐笔翻页, 硬超时 12s) + dark_review + mainflow_tri + cancel_intent
        + layer(orderbook 硬超时 8s + wencai 硬超时 10s)
      ⇒ 冷启动实测 ~26s, 超过前端 fetchAPI 的 20s abort ⇒ /portfolio sparkline 空列。
    并发后冷启动 ≈ 最长的一条。各块内部逻辑与降级路径不变(返回值逐字段一致)。
    """
    collector = KlineCollector(market_code)
    summary = collector.get_kline_summary(symbol)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        f_intent = ex.submit(_enrich_main_intent, symbol, market_code)
        f_dark = ex.submit(_enrich_dark_clusters, symbol)
        f_tri = ex.submit(_enrich_mainflow_tri, symbol, market_code)
        f_cancel = ex.submit(_enrich_cancel_order_intent, symbol)
        f_layer = ex.submit(_build_layer_data, symbol, market_code)
        main_intent, main_intent_structured = f_intent.result()
        dark_clusters = f_dark.result()
        mainflow_tri = f_tri.result()
        cancel_order_intent = f_cancel.result()
        layer = f_layer.result()

    return {
        "symbol": symbol,
        "market": market_code.value,
        "summary": summary,
        "main_intent": main_intent,
        "main_intent_structured": main_intent_structured,
        # 微观高频撤单意图 (对标广发金工与通达信盘口)
        "cancel_order_intent": cancel_order_intent,
        # A4 置信度徽章(批次A)
        "mainflow_tri": mainflow_tri,
        # P1 图层数据(2026-09-01): gs_signals / fund_flow / events / orderbook / chips / resonance
        **layer,
        # A4 拆单簇暗盘(2026-09-01 接入 summary,前端资金面板双口径展示)
        "dark_clusters": dark_clusters,
    }


def _get_or_compute_summary(
    symbol: str, market_code: MarketCode, refresh: bool = False
) -> dict:
    """单只 summary 三层缓存取数: biz_cache(内存+Redis) → PG → 重算。

    命中顺序与单只端点一致(2026-10-01 B1 P2): 
      ① biz_cache(L1 内存 + L2 Redis, 跨 worker/重启共享);
      ② PG summary_cache(L3 持久兜底, 进程重启/容器迁移);
      ③ 都 miss → _compute_kline_summary 真正重算, 回填两层。
    重算段用 per-key single-flight 锁: 并发相同 symbol 只真算一次, 其余等锁后读缓存。

    **单只端点和 /summary/batch 共用此入口** —— 2026-10-14 缓存收口: 批量端点此前
    逐只裸调 collector.get_kline_summary()(绕开全部缓存, N 只 N 次现算), 现改为
    逐只走本函数, 与单只端点命中同一份 `summary:{market}:{symbol}` 缓存。
    """
    cache_key = f"summary:{market_code.value}:{symbol}"

    # L1/L2: biz_cache(内存 + Redis); 命中秒回, 跨进程/重启共享(refresh 跳过)
    if not refresh:
        hit = biz_cache.get_json(cache_key)
        if hit is not None:
            return hit
        # L3: PG 落库(进程重启/容器迁移兜底, 与 fundamentals-detail 同一持久层)
        pg_hit = get_cached_summary(symbol, market_code.value, ttl_s=_SUMMARY_PG_TTL)
        if pg_hit:
            biz_cache.set_json(cache_key, pg_hit, ttl=_SUMMARY_TTL)
            return pg_hit

    # miss: single-flight —— 并发相同 symbol 只真正重算一次, 其余等锁后读缓存。
    # (B1 实测 /portfolio 30s 后仍有多条请求 in-flight = 线程被 26s 冷链占死; 单飞止血。)
    with _summary_lock(cache_key):
        if not refresh:
            hit = biz_cache.get_json(cache_key)
            if hit is not None:
                return hit
        result = _compute_kline_summary(symbol, market_code)
        biz_cache.set_json(cache_key, result, ttl=_SUMMARY_TTL)
        # L3 落库(进程重启/容器迁移兜底, 失败永不抛)
        try:
            put_cached_summary(symbol, market_code.value, result, ttl_s=_SUMMARY_PG_TTL)
        except Exception:  # noqa: BLE001
            pass
        return result


@router.get("/{symbol}/summary")
def get_kline_summary(symbol: str, market: str = "CN", refresh: bool = False):
    """获取单只股票K线摘要

    2026-08-20: 加 30s 进程内缓存(主力意图+筹码逐笔翻页冷启动 ~20-30s 撞 502)。
    2026-09-03 (v0.4.77): 加 PG summary_cache 落库, 进程重启后冷启动也命中;
       进程内 L1 5min, PG L2 5min, 双层一致。冷启动命中顺序 L1 → L2 → 计算。
    2026-09-04: refresh=1 跳过 L1+L2 强制重算(数据源热切/补数后立即验证用)。
    2026-10-01 (B1 P2 首屏慢): L1 从进程内 dict 迁到 biz_cache(L1 内存 + L2 Redis),
       跨 worker 共享 + 重启不丢; 加 single-flight 防并发冷启动把线程池打满;
       冷链四个重活并发化(见 _compute_kline_summary) —— 冷启动 ~26s → ~max(单条),
       低于前端 fetchAPI 的 20s abort。命中顺序: biz_cache(内存→Redis) → PG → 重算。
    2026-10-14 缓存收口: 取数逻辑抽到 _get_or_compute_summary, 与 /summary/batch 共用。
    """
    market_code = _parse_market(market)
    return _get_or_compute_summary(symbol, market_code, refresh=refresh)


@router.post("/summary/batch")
def get_kline_summary_batch(payload: KlineSummaryBatchRequest):
    """批量获取K线摘要

    2026-10-14 缓存收口(审计: 放着已有缓存设施不用): 原实现是**逐只裸调
    `collector.get_kline_summary()` 现算** —— 既绕过 biz_cache/PG 两层缓存(同一只
    股票从单只端点刚算过, 批量端点仍从头再算一遍), 又是纯串行(N 只 = N 次现算串联,
    冷启动必然撞前端 abort)。现改为**逐只走 _get_or_compute_summary**(与单只端点同
    一份 `summary:{market}:{symbol}` 缓存, 命中即零重算)+ 线程池并发(上限
    _SUMMARY_BATCH_MAX_WORKERS, 防把线程池打满)。返回结构与单只端点一致(逐只的
    完整 summary 载荷), 顺序与请求 items 一致。
    """
    items = payload.items or []
    if not items:
        return []

    def _one(item: KlineSummaryItem) -> dict:
        market_code = _parse_market(item.market)
        return _get_or_compute_summary(item.symbol, market_code)

    # ex.map 保序(与请求 items 一致); 坏市场 _parse_market 抛 HTTPException → 原样上抛 400。
    workers = max(1, min(_SUMMARY_BATCH_MAX_WORKERS, len(items)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_one, items))


# ── K线形态识别(图层标注): 严格规则, 证据非建议 ────────────────────────────────
#: 形态识别的 bars 根数夹取(太短识别不出; 太长无谓开销)
PATTERNS_MIN_DAYS = 30
PATTERNS_MAX_DAYS = 250
#: 默认"近 N 根内"确认根窗口
PATTERNS_DEFAULT_LOOKBACK = 60


@router.get("/{symbol}/patterns")
def get_kline_patterns(
    symbol: str,
    market: str = "CN",
    days: int = 120,
    lookback: int = PATTERNS_DEFAULT_LOOKBACK,
):
    """获取单只股票的 K 线形态标注(供前端 K 线图层打点)。

    - 与 `/klines/{symbol}` **同源同口径**(PG hypertable 优先 → 联网兜底 + 剔桩/补今日),
      保证标注与图上 K 线一一对齐, 不是另算一条序列;
    - 形态走 `src/core/kline_patterns` 的**严格规则**(窗口/影线比/实体比/量能), 不做模糊判定;
    - `patterns` 为**证据标注**(形态名/方向/位置/置信依据), 不含买卖建议;
    - 识别不出 → **显式空数组**(不硬凑), `count` 如实为 0;
    - 边界(窗口不足/一字板/停牌缺口)由识别模块显式不识别。
    """
    market_code = _parse_market(market)
    days = max(PATTERNS_MIN_DAYS, min(int(days), PATTERNS_MAX_DAYS))
    lookback = max(1, min(int(lookback), days))

    source = None
    pg_asof = None
    today_bar = None

    pg_klines, pg_asof = _pg_klines(symbol, market_code, days)
    if pg_klines is not None:
        klines = _aggregate_klines(pg_klines, "1d")
        klines, today_bar = _finalize_daily_bars(klines, symbol, market_code, "1d")
        source = "pg_klines_hypertable"
    else:
        collector = KlineCollector(market_code)
        klines = collector.get_klines(symbol, days=days)
        klines = _aggregate_klines(klines, "1d")
        klines, today_bar = _finalize_daily_bars(klines, symbol, market_code, "1d")

    bars = _serialize_klines(klines)
    asof = str(getattr(klines[-1], "date", "") or pg_asof or "")[:10] if klines else (pg_asof or None)

    from src.core.kline_patterns import detect_pattern_marks

    marks = detect_pattern_marks(bars, lookback=lookback)

    out: dict = {
        "symbol": symbol,
        "market": market_code.value,
        "asof": asof,
        "days": days,
        "lookback": lookback,
        "count": len(marks),
        "patterns": [m.to_dict() for m in marks],
    }
    if source:
        out["source"] = source
    if today_bar:
        out["today_bar"] = today_bar
    return out


# ── 威科夫量价结构标注(本土三层滤网 P1 第三层, 与 patterns 同范式) ──────────
#: 与 PATTERNS_* 同夹取(太短识别不出; 太长无谓开销)
WYCKOFF_MIN_DAYS = 30
WYCKOFF_MAX_DAYS = 250
WYCKOFF_DEFAULT_LOOKBACK = 60


@router.get("/{symbol}/wyckoff")
def get_kline_wyckoff(
    symbol: str,
    market: str = "CN",
    days: int = 120,
    lookback: int = WYCKOFF_DEFAULT_LOOKBACK,
):
    """获取单只股票的威科夫量价结构标注(供前端 K 线图层打点)。

    - 与 `/klines/{symbol}` **同源同口径**(PG hypertable 优先 → 联网兜底 + 剔桩/补今日),
      保证标注与图上 K 线一一对齐, 不是另算一条序列;
    - 结构走 `src/core/wyckoff` 的**严格规则**(放量/缩量/影线/位置阈值), 不做模糊判定;
    - `markers` 为**证据标注**(结构名/记号配色方向/位置/置信依据), 不含买卖建议,
      文案禁用"主力/建仓/出货"等方向性词(见模块口径红线);
    - `verdict` 为阶段粗判 + `note` 免责(仅日 K 结构, 不得作主力意图结论);
    - 识别不出 → **显式空数组** + `stage=unknown`(不硬凑), `count` 如实为 0;
    - 边界(窗口不足/桩线/停牌缺口)由识别模块显式不识别。
    """
    market_code = _parse_market(market)
    days = max(WYCKOFF_MIN_DAYS, min(int(days), WYCKOFF_MAX_DAYS))
    lookback = max(1, min(int(lookback), days))

    source = None
    pg_asof = None
    today_bar = None

    pg_klines, pg_asof = _pg_klines(symbol, market_code, days)
    if pg_klines is not None:
        klines = _aggregate_klines(pg_klines, "1d")
        klines, today_bar = _finalize_daily_bars(klines, symbol, market_code, "1d")
        source = "pg_klines_hypertable"
    else:
        collector = KlineCollector(market_code)
        klines = collector.get_klines(symbol, days=days)
        klines = _aggregate_klines(klines, "1d")
        klines, today_bar = _finalize_daily_bars(klines, symbol, market_code, "1d")

    bars = _serialize_klines(klines)
    asof = str(getattr(klines[-1], "date", "") or pg_asof or "")[:10] if klines else (pg_asof or None)

    from src.core.wyckoff import analyze_wyckoff

    result = analyze_wyckoff(bars, lookback=lookback)

    out: dict = {
        "symbol": symbol,
        "market": market_code.value,
        "asof": asof,
        "days": days,
        "lookback": lookback,
        "count": len(result["markers"]),
        "markers": result["markers"],
        "verdict": result["verdict"],
    }
    if source:
        out["source"] = source
    if today_bar:
        out["today_bar"] = today_bar
    return out


# ── 暗盘资金副图序列(数智决策副图1 §2 载体) ───────────────────────────────────
#: 序列天数夹取(太短看不出红绿连续性; 太长无谓开销)
POOL_FLOW_MIN_DAYS = 1
POOL_FLOW_MAX_DAYS = 60
#: 进程缓存秒数(与 summary 同量级; 资金序列盘中变化慢, 5min 足够新)
_POOL_FLOW_TTL = 300


@router.get("/{symbol}/pool-flow-series")
def get_pool_flow_series(symbol: str, market: str = "CN", days: int = 30):
    """暗盘资金 N 日序列(供 K 线副图「暗盘资金」红绿柱)。

    数据源: `src/core/dark_pool_flow.compute_pool_flow_series`(明盘 + 暗盘 = 主力净额)。
    诚实口径(与规格 §2 一致, 不编造):
      - **历史日 = OHLC 分摊近似**(方向/符号可靠, 幅度有偏差), 逐点显式带
        `approximation=True`; 图上标注 `L1_approx`;
      - 当日(最后一根)若权威 ming+dark 可得则已叠加(`approximation=False`);
      - 无 K 线(多源全挂) → `available:false` + 显式 note(绝不返 0 冒充真实值);
      - 非 CN 无明/暗盘资金源 → `available:false` 结构化降级。
    红柱=流入(net>0)/绿柱=流出(net<0) 由前端按 net 符号分色, 后端不预分。
    """
    market_code = _parse_market(market)
    if market_code.value != "CN":
        return {
            "symbol": symbol, "market": market_code.value, "available": False,
            "series": [], "days": 0, "confidence": None,
            "note": "仅 A 股支持暗盘资金序列(明/暗盘为 A 股口径)",
        }
    days = max(POOL_FLOW_MIN_DAYS, min(int(days), POOL_FLOW_MAX_DAYS))
    cache_key = f"poolflow:{market_code.value}:{symbol}:{days}"
    hit = biz_cache.get_json(cache_key)
    if hit is not None:
        return hit

    from src.core.dark_pool_flow import DARK_CONFIDENCE, compute_pool_flow_series

    try:
        d = compute_pool_flow_series(symbol, days=days)
    except Exception as e:  # noqa: BLE001 计算失败显式无数据, 不编造
        logger.warning("pool-flow-series %s failed: %s", symbol, e)
        return {
            "symbol": symbol, "market": market_code.value, "available": False,
            "series": [], "days": days, "confidence": DARK_CONFIDENCE,
            "note": "无数据(暗盘资金序列计算失败)",
        }
    series = (d or {}).get("series") or []
    out = {
        "symbol": symbol,
        "market": market_code.value,
        "available": bool(series),
        "days": days,
        "series": series,
        # 历史日=OHLC 分摊近似(方向可靠/幅度有偏差), 显式标 confidence 供 UI 诚实标注
        "confidence": DARK_CONFIDENCE,
        "note": (d or {}).get("note") if series else "无数据",
    }
    biz_cache.set_json(cache_key, out, ttl=_POOL_FLOW_TTL)
    return out


# ── 图表说明(chart-notes): 逐标记「为什么」, 人可读 / AI 可引 ──────────────────
#: chart-notes 取 bars 的天数(与图层 `_build_layer_data` 同窗口 —— 窗口不同等于两套口径)
CHART_NOTES_DAYS = 120


def _collect_chart_note_sources(symbol: str, market_code: MarketCode) -> dict:
    """收集 chart-notes 的**标记来源** —— 全部与图层/指标端点**同函数**, 不另算标记。

    | 标记 | 来源(与图层同一份计算结果) |
    |---|---|
    | gs_signals / events | `_build_layer_data`(读库优先 + 现算, 与 K 线图逐条一致) |
    | patterns            | `kline_patterns.detect_pattern_marks`(与 `/patterns` 同函数) |
    | trend / niuxiong    | `fetch_trend_line` / `fetch_niuxiong`(与 `/indicators/*` 同函数) |
    | gs_state            | `gs_state.resolve_gs_state`(与色带/图例同源) |

    任一来源失败独立降级为 None(不编造); 全程 try/except, 绝不抛。`bars` 取一次复用
    (与图层同窗口 `CHART_NOTES_DAYS`, 避免窗口不同导致 「两张脸」)。
    """
    out: dict = {
        "bars": [], "gs_signals": None, "events": None,
        "patterns": None, "trend": None, "niuxiong": None, "gs_state": None,
    }
    # ① bars(与图层同一份取数 `decision_pioneer.fetch_bars`)
    try:
        from src.core.decision_pioneer import fetch_bars

        out["bars"] = fetch_bars(symbol, "CN", days=CHART_NOTES_DAYS) or []
    except Exception as e:  # noqa: BLE001
        logger.debug("chart-notes fetch_bars %s failed: %s", symbol, e)

    # ② GS 买卖点 / L4 事件: 图层**同一份计算结果**(读库优先 + 现算)
    try:
        layer = _build_layer_data(symbol, market_code)
        out["gs_signals"] = layer.get("gs_signals")
        out["events"] = layer.get("events")
    except Exception as e:  # noqa: BLE001
        logger.debug("chart-notes layer_data %s failed: %s", symbol, e)

    bars = out["bars"]
    if bars:
        # ③ GS 区间/待确认(与 K 线图色带/图例同源)
        try:
            from src.core.gs_state import resolve_gs_state

            out["gs_state"] = resolve_gs_state(bars)
        except Exception as e:  # noqa: BLE001
            logger.debug("chart-notes gs_state %s failed: %s", symbol, e)
        # ④ 形态(与 /klines/{symbol}/patterns 同函数)
        try:
            from src.core.kline_patterns import detect_pattern_marks

            out["patterns"] = detect_pattern_marks(bars, lookback=PATTERNS_DEFAULT_LOOKBACK)
        except Exception as e:  # noqa: BLE001
            logger.debug("chart-notes patterns %s failed: %s", symbol, e)

    # ⑤ 趋势操盘线 / 牛熊线(与 /indicators/* 同函数)
    try:
        from src.core.trend_pilot_line import fetch_trend_line

        out["trend"] = fetch_trend_line(symbol, market=market_code.value)
    except Exception as e:  # noqa: BLE001
        logger.debug("chart-notes trend %s failed: %s", symbol, e)
    try:
        from src.core.niuxiong_line import fetch_niuxiong

        out["niuxiong"] = fetch_niuxiong(symbol, market=market_code.value)
    except Exception as e:  # noqa: BLE001
        logger.debug("chart-notes niuxiong %s failed: %s", symbol, e)
    return out


@router.get("/{symbol}/chart-notes")
def get_chart_notes(symbol: str, market: str = "CN"):
    """图表说明文本: 逐标记「为什么在那」(触发/时点/失效/置信度), 人读图 / AI 备料共用。

    **看图决策产品件(2026-10-14)**: 图表标注是人机共用的证据语言 —— 本端点把图层上
    **每个标记**转成一条稳定可引用的说明, 供①前端图下说明条(一行式小字, 非卡片)②AI
    备料直接引用。

    **单一事实源(禁第二套口径)**: 标记列表不由本端点重算 —— 全部取**与图层渲染同一份
    计算结果**(GS 买卖点/事件走 `_build_layer_data`; 形态走 `detect_pattern_marks`;
    操盘线/牛熊线走 `/indicators/*` 同函数; 区间走 `resolve_gs_state`)。逐条对应关系由
    `tests/test_chart_notes.py::test_same_source_contract` 钉死。

    **三字段**: 触发条件/数据时点/失效条件复用 `evidence_chain.build_evidence_chain`
    (与 AI 建议出口同一套字段) —— 失效条件永不为空(缺失显式标 `invalidation_defaulted`);
    数据时点缺失显式「时点缺失」, 非今日显式标基准日(不默认成今日)。置信度缺样本显式
    「未校准」, 不给数字(不编造)。

    非 A 股 / 无标记 → `available=False` + `notes=[]` + 显式 note(不编造)。
    """
    market_code = _parse_market(market)
    if market_code.value != "CN":
        return {
            "symbol": symbol, "market": market_code.value, "available": False,
            "count": 0, "counts": {}, "notes": [], "notes_text": [], "zone_note": None,
            "note": "仅 A 股支持图表标记说明(图层为 A 股口径)",
        }

    from src.core.chart_notes import build_chart_notes, gs_state_note

    src = _collect_chart_note_sources(symbol, market_code)
    bars = src.get("bars") or []
    asof = _norm_date(bars[-1].get("date")) if bars else None

    out = build_chart_notes(
        symbol=symbol,
        as_of=asof,
        gs_signals=src.get("gs_signals"),
        events=src.get("events"),
        patterns=src.get("patterns"),
        trend=src.get("trend"),
        niuxiong=src.get("niuxiong"),
        gs_state=src.get("gs_state"),
    )
    out["market"] = market_code.value
    # 当前 GS 标记(区间/待确认)的说明: 与推送/战报**同源取句**(单一事实源)
    out["zone_note"] = gs_state_note(src.get("gs_state"), as_of=asof)
    return out


# ── 日级块现算入口注册(2026-10-14 首屏 P0, 依赖倒置) ──────────────────────────
# `src/core/layer_precompute` 的收盘批算需要本模块的 `build_layer_daily_blocks`,
# 但 core **禁止**反向 import web(B4.1/KI-039 棘轮门禁
# tests/test_w41_core_web_dependency.py)。故由本层在 import 期把入口注册过去;
# 应用启动必加载本模块(app.py 挂 klines.router), 调度器因此总能拿到入口。
# 未注册时批算显式记 skipped + 告警, 绝不编造(见 layer_precompute.precompute)。
from src.core.layer_precompute import (  # noqa: E402
    register_daily_block_builder as _register_layer_daily_builder,
)

_register_layer_daily_builder(build_layer_daily_blocks)
