"""ETF 分钟「TQ 空源」自动补缺（调度侧，2026-11-10）。

## 为什么需要

TQ 的 1m 分钟线全部来自客户端 `vipdoc/*/minline/*.lc1`。2026-11-05 晚间起不再下载，
`get_market_data(period="1m")` 对这些交易日返回 `ErrorId:"0"` + `Value:[]` —— **不报错**，
于是 `run_close`（15:35）与启动自愈都"成功"地写入了 0 行，库里静默留洞。东财 `trends2`
仍在服务最近 5 个交易日，是唯一可用的兜底源（口径/对账见 `src.collectors.em_etf_minute`）。

本模块只做**调度装配**，不重写取数/校验/写入逻辑：真正落库一律走
`em_etf_minute.backfill(..., require_verified=True, daily_ref=tq_daily_ref(...))`。

## 五条硬纪律（违反任一条都会静默地把库弄脏）

1. **只补「已闭市」的交易日**：`day < today`。当日盘中/收盘后都不补（东财当日形态未验证，
   且 `em_etf_minute._plan` 本身就是 `not_closed_day` 拒绝）。当日 TQ 空源只能观测，
   由次日的盘前/收盘后任务补 —— 这是硬缺口，不靠"再试一次"绕过。
2. **只补「TQ 完全 0 行」的日期**：该日已有任何行（任何来源，含用户自己落的行）就
   **一次 backfill 都不调**，绝不覆盖、也不"修补"残缺日。残缺日只报失败。
3. **必须可证实对账**：`require_verified=True` 写死，`daily_ref` 缺省就地构造
   `em_etf_minute.tq_daily_ref(codes)`。本模块**不提供**跳过对账的开关（测试会检查
   源码里不存在这类字面量）。拿不到 TQ 日线 ⇒ 当天不写、报 `unverified`。
   对账粒度是**全篮子逐只**：20 只里差一只都叫 `insufficient_etfs`，不接受 80%/16 只。
4. **假日/残缺日/取数失败都是显式失败**：`ok=False` + 结构化原因，不吞、不当成功。
   `deferred_today`（未闭市）与 `complete`（本来就有完整全篮子数据）是唯一不算失败的两类跳过；
   `complete` 只进 `complete_days`，不再混进 `deferred_days`。
   **「有行」不等于「完整」**：`amount_wan` 为 NULL 的行算不出成交额，`existing_overview`
   按非空金额口径判完整 —— NULL 金额日一律 partial（不补、不覆盖、必上报），否则这类
   历史脏数据会被永久当成 complete 跳过。
5. **不阻塞启动、不抢 TQ 客户端**：`register()` 只挂 cron；启动恢复在 daemon 线程里
   延迟执行，交易时段直接跳过。补缺请求是 20 只 ETF 一次批量（EM 侧）+ 一次日线 RPC。

## 有限窗口重试

- **调度级**：盘前 08:40 / 收盘后 16:50 / 补漏 18:50，三个 cron 各一次机会；
  `misfire_grace_time=900` 让发布/重启造成的短暂耽误仍能执行。
- **单次运行内**：只对 `fetch_failed` / `write_failed` / `unverified` 这几类**可能瞬时**
  的失败重试，`max_attempts=2`、退避 20s、整轮预算 480s；不对 `out_of_window` /
  `non_trading_day` / 残缺日做无意义重试。
- **预算不是硬上界**：`deadline_s` 只保证「预算没了就不再开新的取数/重试/退避」，
  在途的那次 HTTP/RPC 由它自己的 timeout 收手，所以整轮可能越界。报告里
  `deadline_s` / `elapsed_s` / `deadline_exhausted` / `deadline_breached` 四个字段
  一起给出真相；因预算耗尽未处理的日期一律 `deadline_exceeded` 失败（`ok=False`），
  绝不当成功。TQ 日线参照（逐只对账用）与本轮**共用同一个绝对截止时刻**，不会
  跨日各续 90s 把预算撑破。
- **错过 15:35 的收盘定型**：`catch_up_close()` 在 [15:35, 22:00] 窗口内、当日 0 行或
  只有本源的残缺行时重跑一次 `tq_etf_watch.run_close()`（幂等，同源 upsert）；
  一旦发现异源行立即停手，不去动别人的数据。22:00 之后不再尝试 —— 有限窗口。

## 空源可观测

每次运行都会：① 打 `[etf-gapfill]` 前缀的结构化日志（`empty_source` / `partial_day` /
`tq_empty_attribution` / `backfilled` / `unfillable_gaps` / `incomplete_history`）；
② 未解决的缺口写一条 `EtfMinuteSourceGap` 事件进 `error_tracker`（落
`DATA_DIR/error_events.jsonl`，即 `/api/logs` 的 `recent_errors` 面）；③ 把整轮报告
记在模块状态里，`status()` 可读（最近一次运行、每天的状态/来源/原因/归因、累计运行
次数）。不是"补不上就算了"，是"补不上要在系统日志里看得见"。

**0 行必须归因，不许直接说成「TQ 空源」**：库里的 0 行可能来自源本身为空，也可能来自
采集没落库（客户端在跑、库里却没行）。`attribute_tq_empty()` 用 TQ 日线证据把两者分开：
`tq_source_empty`（日线也没有）、`collector_missing`（日线有、分钟表 0 行 → 采集故障）、
`tq_reference_unavailable`（拿不到日线，无从判断）、`unknown`。归因结论进日志/事件的
`attribution` 字段，处置方向不同。

另外，`survey_unfillable_gaps()` 每轮巡检最近 15 个交易日：比东财滚动窗口更早的 0 行日
东财**补不到**，这类空洞单独以 `unfillable_gaps` 事件上报（不计入 `ok`，因为不在本模块
能修的范围内，但绝不能因为"不在候选列表里"就从视野里消失）；同一窗口里的**历史残缺日**
（16/20、金额 NULL、分钟不全）以 `incomplete_history` 上报 —— 按纪律不覆盖，但必须可见。
"""

from __future__ import annotations

import copy
import logging
import sys
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

# ── 时间/窗口闸门 ────────────────────────────────────────────────────────

#: 收盘定型时刻（与 startup.py 的 `etf-watch-close` 一致）。
CLOSE_HM = "1535"
#: 错过收盘定型的补跑窗口上界；过了这个点不再回溯（有限窗口，不无限重试）。
RETRY_DEADLINE_HM = "2200"
#: 交易时段（含集合竞价与尾盘 5 分钟缓冲）：启动恢复在这两段里直接跳过，不抢 TQ 客户端。
_AM_SESSION = ("0915", "1135")
_PM_SESSION = ("1255", "1505")
#: TQ 收盘定型重负载窗口（15:35 起 run_close + 启动自愈同时在跑）。启动恢复若撞进来
#: 会和分钟采集/定型抢同一个 TQ 客户端，且 catch_up_close 可能与定时 run_close 并行。
_TQ_CLOSE_WINDOW = ("1535", "1630")


def _app_now(now: datetime | None = None) -> datetime:
    """把"现在"统一成 app 时区(默认 Asia/Shanghai)的 naive 时间。

    调度器/启动线程传进来的可能是 aware UTC（`datetime.now(timezone.utc)`）。直接
    `strftime("%H%M")` 会得到 UTC 的钟点 —— 北京 10:00 被看成 02:00，交易时段判断
    整体错 8 小时，启动恢复会在盘中抢 TQ。naive 输入按现有约定视为本地钟点，不改。
    """
    now = now or datetime.now()
    if now.tzinfo is None:
        return now
    from src.core.timezone import to_beijing

    return to_beijing(now).replace(tzinfo=None)


def _in_span(hm: str, span: tuple[str, str]) -> bool:
    """闭区间判断（缓冲段的端点本身也算在内，避免 11:35/15:05 这种整点漏判）。"""
    return span[0] <= hm <= span[1]


def _in_session(now: datetime) -> bool:
    hm = _app_now(now).strftime("%H%M")
    return _in_span(hm, _AM_SESSION) or _in_span(hm, _PM_SESSION)


def _in_tq_close_window(now: datetime) -> bool:
    return _in_span(_app_now(now).strftime("%H%M"), _TQ_CLOSE_WINDOW)


DEFAULT_DAYS = 5
#: 更长的"补不到"巡检窗口：东财只能回最近 5 个交易日，超出窗口的空洞必须被看见
#: （而不是因为不在候选列表里就静默消失）。
SURVEY_DAYS = 15
#: 单轮最多尝试次数（1 = 不重试）。只对瞬时类失败生效。
DEFAULT_MAX_ATTEMPTS = 2
#: 两次尝试之间的退避秒数。
DEFAULT_RETRY_SLEEP_S = 20.0
#: 一整轮的 wall-clock **软预算**：用尽后不再开新的取数/重试，但**不是硬上界** ——
#: 在途的那次 HTTP/RPC 只能靠它自己的 timeout 收手，报告里的 `elapsed_s` /
#: `deadline_breached` 会把真实越界量如实写出来（旧注释谎称"上界"，实际能超出很多）。
DEFAULT_DEADLINE_S = 480.0
#: TQ 日线参照（逐只对账用）单次取数的预算上限；实际取 `min(这个值, 本轮剩余预算)`，
#: 多日共用同一个绝对截止时刻（`deadline_at`），不会每天各续一份。
DEFAULT_TQ_REF_BUDGET_S = 90.0
#: 启动恢复线程的初始延迟：让 lifespan 先完成装配，也让 `ensure_backfilled` 先跑。
STARTUP_DELAY_S = 150.0

# ── 日状态 ───────────────────────────────────────────────────────────────

ST_TQ_EMPTY = "tq_empty"             # 该日 0 行 → 唯一允许补缺的状态
ST_COMPLETE = "complete"             # 已有完整单源数据 → 正当跳过
ST_PARTIAL = "partial"               # 有行但残缺 → 显式失败，不补不覆盖
ST_DEFERRED_TODAY = "deferred_today"  # 未闭市 → 当日不补，明确记录
ST_HOLIDAY = "non_trading_day"       # 周末/法定节假日 → 显式失败
ST_INVALID_DATE = "invalid_date"
ST_DB_ERROR = "db_unavailable"
ST_CALENDAR_ERROR = "calendar_unavailable"

#: 这些跳过原因不算"这一天该补却没补"。
_BENIGN_STATES = {ST_COMPLETE, ST_DEFERRED_TODAY}
#: 可能瞬时的失败原因 → 允许有限重试。日期类/窗口类失败没有重试价值。
_RETRYABLE_REASONS = {"fetch_failed", "write_failed", "unverified"}

_SOURCE_TQ = "tdx_tq"

# ── 可观测状态 ───────────────────────────────────────────────────────────

_STATE_LOCK = threading.Lock()
_LAST_RUN: dict[str, Any] | None = None
_RUN_SEQ = 0


class EtfMinuteSourceGap(RuntimeError):
    """「源空缺」事件。走 `error_tracker` → `/api/logs` 的 recent_errors 面。

    单独一个类型是有意的：日志/告警里能一眼分清"采集器抛异常挂了"和"源本身返回空、
    数据补不上"，这两类问题的处置完全不同。
    """


def status() -> dict[str, Any]:
    """最近一次补缺运行的完整报告（含每天状态/来源/原因）。无运行过则返回 {}。"""
    with _STATE_LOCK:
        return copy.deepcopy(_LAST_RUN) if _LAST_RUN is not None else {}


def runs() -> int:
    with _STATE_LOCK:
        return _RUN_SEQ


def _remember(report: dict[str, Any]) -> None:
    global _LAST_RUN, _RUN_SEQ
    with _STATE_LOCK:
        _RUN_SEQ += 1
        report["run_seq"] = _RUN_SEQ
        _LAST_RUN = copy.deepcopy(report)


def reset_state() -> None:
    """清空内存状态（仅测试用）。"""
    global _LAST_RUN, _RUN_SEQ
    with _STATE_LOCK:
        _LAST_RUN = None
        _RUN_SEQ = 0


# ── 只读 engine：显式注入（core 不得在运行时 import web） ──────────────

class EngineNotInjected(RuntimeError):
    """没有可用的只读 engine。core 层必须由 startup 注入，不能自己找库。"""


_ENGINE_LOCK = threading.Lock()
_ENGINE_PROVIDER: Callable[[], Any] | None = None


def configure_engine(provider: Callable[[], Any] | None) -> None:
    """由 startup 注入 engine 提供者（返回 SQLAlchemy Engine）。

    `src/core` 不允许在运行时反向依赖 `src.web.database.engine`（B4.1）：一旦 web
    层没初始化/连到别的库，这里会把"读不到"静默降级成 `tq_empty`，然后自动补数写进
    错误的库。显式注入把"用哪个库"交给装配层，也让隔离测试能直接注入 tmp SQLite。

    `provider=None` 表示撤销注入（测试清理用）。
    """
    global _ENGINE_PROVIDER
    if provider is not None and not callable(provider):
        raise TypeError(f"engine provider 必须可调用，拿到 {type(provider).__name__}")
    with _ENGINE_LOCK:
        _ENGINE_PROVIDER = provider


def _engine_from_startup_modules():
    """复用**启动阶段已经构造好**的中立层 engine（`src.db.session`），不触发 import。

    这是未显式 `configure_engine()` 时的兜底：server 进程在 lifespan 之前就已建立
    `src.db.session`（`src.web.database` 只是 re-export），所以这里只读 `sys.modules`
    即可，不会在 core 里制造 web 依赖，也不会为测试凭空建一个新 engine。
    """
    mod = sys.modules.get("src.db.session")
    eng = getattr(mod, "engine", None) if mod is not None else None
    if eng is not None and callable(getattr(eng, "connect", None)):
        return eng
    return None


def _require_engine(engine):
    """校验/解析 engine：显式参数 → startup 注入 → 已构造的中立层 engine。

    三条路都拿不到就**直接报错**，绝不静默返回 None 让上层把"没有库"当成
    "该日 0 行"（那会触发一次盲补）。
    """
    if engine is not None:
        if not callable(getattr(engine, "connect", None)):
            raise TypeError(f"engine 需提供 connect()，拿到 {type(engine).__name__}")
        return engine
    with _ENGINE_LOCK:
        provider = _ENGINE_PROVIDER
    if provider is not None:
        eng = provider()
        if eng is not None:
            if not callable(getattr(eng, "connect", None)):
                raise TypeError(f"engine provider 返回的对象没有 connect(): {type(eng).__name__}")
            return eng
    eng = _engine_from_startup_modules()
    if eng is not None:
        return eng
    raise EngineNotInjected(
        "ETF 分钟补缺缺少只读 engine：startup 必须调用 "
        "etf_minute_gapfill.configure_engine(lambda: src.db.session.engine)，"
        "或显式把 engine 传给各入口；core 不得自行 import web 层找库"
    )


# ── 依赖缝（测试只打桩这几处） ────────────────────────────────────────────

def _em():
    from src.collectors import em_etf_minute

    return em_etf_minute


def _basket_codes() -> list[str]:
    from src.collectors.tq_etf_watch import ETF_BASKET

    return [str(c) for c, _n, _cat in ETF_BASKET]


def _coverage_gate(n: int) -> int:
    """覆盖率闸门：**全篮子**（20 只 → 20 只），不接受 80%。

    旧实现借 `src.core.etf_watch_feed.min_etfs_for_complete`（80%）：16/20 的日期
    会被 `existing_overview(min_codes=16)` 判成 complete，自动补缺于是把它当
    "已补过"跳过 —— 残缺永久留库，且报告显示成功。审计口径是每只齐全 + 逐只对账，
    所以这里与 `em_etf_minute._gate` 一起收紧为 100%，不再有按比例放行的入口。
    """
    return max(1, int(n))


def _is_trading_day(d: date) -> bool:
    from src.core.trading_calendar import is_trading_day

    return d.weekday() < 5 and bool(is_trading_day(d))


def _day_to_date(day: Any) -> date | None:
    try:
        return datetime.strptime(str(day).strip(), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _emit(event: str, **fields: Any) -> None:
    """结构化日志：字段固定 `k=v`，便于 grep/告警规则按事件名匹配。

    `backfilled` 也是 WARNING：它意味着 TQ 主源对那天是空的（东财只是兜住了数据），
    降级本身就要在日志里看得见，而不是等兜底也失败才发现。
    """
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    if event in ("empty_source", "partial_day", "unverified", "backfilled", "unfillable_gaps",
                 "tq_empty_attribution", "incomplete_history"):
        logger.warning("[etf-gapfill] %s %s", event, body)
    else:
        logger.info("[etf-gapfill] %s %s", event, body)


def _publish_gap_event(day: str, kind: str, detail: str, **context: Any) -> None:
    """把未解决的缺口写进既有可观测面（error_tracker → /api/logs recent_errors）。

    `error_tracker.capture_exception` 自己吞掉所有异常，这里再包一层只是防御未来改动。
    同一 (type, message) 5 分钟内只落一条，10 分钟内 ≥3 次才升级为站内通知，
    所以"每天一条日级事件"不会把通知面刷爆。
    """
    msg = f"ETF 分钟数据源空缺 {day}（{kind}）：{detail}"
    try:
        from src.core.error_tracker import capture_exception

        capture_exception(EtfMinuteSourceGap(msg), {"module": "etf_minute_gapfill", "date": day, **context})
    except Exception:  # noqa: BLE001
        logger.warning("[etf-gapfill] 缺口事件落盘失败（不影响主流程）", exc_info=True)


# ── 逐日分类（只读，不联网） ──────────────────────────────────────────────

def classify_day(
    day: str,
    *,
    today: date,
    engine,
    gate: int,
    codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """判断一天属于哪种状态。**只读库 + 只读交易日历，不联网、不写库。**

    `ST_TQ_EMPTY` 是唯一会触发补缺的状态；`ST_PARTIAL` 永远只报失败 ——
    「有 1 行」不等于「已采完」，也不等于「可以覆盖重写」。`codes` 传入全篮子时，
    「完整」要求**每只篮子标的都在库且都是 240 根**；旧的 16/20 只是 `ST_PARTIAL`。
    """
    out: dict[str, Any] = {
        "date": str(day),
        "state": ST_INVALID_DATE,
        "rows": 0,
        "codes": 0,
        "sources": [],
        "null_source_rows": 0,
        "null_amount_rows": 0,
        "minutes": 0,
        "missing_codes": [],
        "detail": "日期格式必须是 YYYYMMDD",
    }
    d = _day_to_date(day)
    if d is None:
        return out

    try:
        trading = _is_trading_day(d)
    except Exception as e:  # noqa: BLE001 - 交易日历缺年份时必须显式失败
        out.update(state=ST_CALENDAR_ERROR, detail=f"交易日历不可用: {e}"[:200])
        return out
    if not trading:
        out.update(state=ST_HOLIDAY, detail="非交易日（周末/法定节假日），不补")
        return out
    if d >= today:
        out.update(state=ST_DEFERRED_TODAY, detail="未闭市：当日/未来日一律不补")
        return out

    try:
        em = _em()
        ov = em.existing_overview(engine, str(day), min_codes=gate, codes=codes)
    except Exception as e:  # noqa: BLE001
        out.update(state=ST_DB_ERROR, detail=f"读库失败: {e}"[:200])
        return out

    out.update(
        rows=int(ov.get("rows") or 0),
        codes=int(ov.get("codes") or 0),
        sources=list(ov.get("sources") or []),
        null_source_rows=int(ov.get("null_source_rows") or 0),
        null_amount_rows=int(ov.get("null_amount_rows") or 0),
        minutes=int(ov.get("minutes") or 0),
        amount_minutes=int(ov.get("amount_minutes") or 0),
        missing_minutes=list(ov.get("missing_minutes") or []),
        short_codes=list(ov.get("short_codes") or []),
        missing_codes=list(ov.get("missing_codes") or []),
    )
    if out["rows"] == 0:
        out.update(state=ST_TQ_EMPTY, detail="该交易日 0 行（疑 TQ 空源；归因见 attribution）")
    elif ov.get("complete"):
        out.update(state=ST_COMPLETE, detail=None)
    elif out["null_source_rows"]:
        out.update(
            state=ST_PARTIAL,
            detail=f"已有行但 {out['null_source_rows']} 行来源为 NULL/空：无法归因，不补、不覆盖",
        )
    elif out["null_amount_rows"]:
        # 有行但金额为 NULL：COUNT(*) 看着满，实际算不出成交额。既不能判完整（否则
        # 从此不再补），也不能补（会覆盖别人的行），只能显式报残缺 + 上报可观测面。
        out.update(
            state=ST_PARTIAL,
            detail=(
                f"已有行但 {out['null_amount_rows']} 行 amount_wan 为 NULL"
                f"（{out['amount_minutes']}/{em.EXPECTED_MINUTES} 分钟有金额）："
                "不能判完整，不补、不覆盖"
            ),
        )
    elif out["missing_codes"]:
        out.update(
            state=ST_PARTIAL,
            detail=f"篮子缺 {len(out['missing_codes'])} 只（{out['missing_codes'][:5]}）：不补、不覆盖",
        )
    else:
        out.update(state=ST_PARTIAL, detail="已有行但残缺：不补、不覆盖，报失败")
    return out


def plan_gapfill(
    dates: Sequence[str] | None = None,
    *,
    days: int = DEFAULT_DAYS,
    engine=None,
    now: datetime | None = None,
    codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """只做分类（不联网、不写库），返回 {"days": [...], "pending": [待补日期]}。"""
    eng = _require_engine(engine)
    now = _app_now(now)
    today = now.date()
    codes = list(codes) if codes is not None else _basket_codes()
    gate = _coverage_gate(len(codes))
    wanted = _candidate_dates(dates, days, today)
    states = [classify_day(d, today=today, engine=eng, gate=gate, codes=codes) for d in wanted]
    return {
        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "gate_etfs": gate,
        "days": states,
        "pending": [s["date"] for s in states if s["state"] == ST_TQ_EMPTY],
    }


def _candidate_dates(dates: Sequence[str] | None, days: int, today: date) -> list[str]:
    """候选日期：显式 dates 原样保留（含假日，交由分类显式失败）；否则取最近 N 个已闭市交易日。"""
    if dates:
        out: list[str] = []
        for raw in dates:
            s = str(raw).strip().replace("-", "")
            if s and s not in out:
                out.append(s)
        return out
    from src.collectors.tq_etf_watch import recent_trade_dates

    n = max(1, int(days))
    got = recent_trade_dates(n, end=today)
    cutoff = today.strftime("%Y%m%d")
    return [d for d in got if d < cutoff]


# ── 补缺主流程 ───────────────────────────────────────────────────────────

def _retryable(state: dict[str, Any], dry_run: bool) -> bool:
    entry = state.get("backfill") or {}
    if entry.get("action") in ("written", "planned"):
        return False
    if dry_run:
        return False
    return str(entry.get("reason") or "") in _RETRYABLE_REASONS


def _cache_ref(ref):
    """同一轮里对同一天只问一次 TQ 日线：backfill 对账与 0 行归因共用一份参照。

    只缓存**成功**结果。失败不缓存 —— 一次瞬时失败之后，归因/重试还应该能重新问一次
    （预算由 `deadline_at` 兜住，不会无限重问）。
    """
    if ref is None:
        return None
    cache: dict[str, dict[str, float]] = {}

    def _cached(day: str):
        key = str(day)
        if key not in cache:
            cache[key] = ref(day) or {}
        return cache[key]

    return _cached


def attribute_tq_empty(
    day: str,
    *,
    daily_ref: Callable[[str], dict[str, float]] | None,
    codes: Sequence[str],
) -> dict[str, Any]:
    """给「该日 0 行」做归因，别把库里没行直接说成「TQ 空源」。

    只用 TQ 自己的日线证据（和逐只对账同一份参照，不额外联网）：

    - `tq_source_empty`：TQ 日线也没有该日数据 → 源本身空（客户端 `minline` 未下载/
      该日未收录），库里的 0 行是源的真实状态；
    - `collector_missing`：TQ 日线**有**该日数据、分钟表却 0 行 → 采集侧没落库，
      这是采集故障，处置方式和「源空」完全不同，告警里必须分清；
    - `tq_reference_unavailable`：拿不到日线参照（网关不可用/预算耗尽）→ 归因未知；
    - `unknown`：本轮根本没有 TQ 参照（调用方显式没给）。
    """
    wanted = [str(c) for c in codes]
    if daily_ref is None:
        return {
            "attribution": "unknown",
            "attribution_detail": "本轮无 TQ 日线参照，0 行原因不可归因",
            "tq_daily_codes": None,
        }
    try:
        ref = daily_ref(day) or {}
    except Exception as e:  # noqa: BLE001 - 归因失败不能盖掉主失败
        return {
            "attribution": "tq_reference_unavailable",
            "attribution_detail": f"TQ 日线参照取不到，归因未知: {type(e).__name__}: {e}"[:220],
            "tq_daily_codes": None,
        }
    have = [c for c in wanted if (ref.get(c) or 0) > 0]
    if have:
        return {
            "attribution": "collector_missing",
            "attribution_detail": (
                f"TQ 日线有该日成交额（{len(have)}/{len(wanted)} 只）但分钟表 0 行："
                "采集侧未落库，不是源空（TQ 客户端/调度要查）"
            ),
            "tq_daily_codes": len(have),
        }
    return {
        "attribution": "tq_source_empty",
        "attribution_detail": (
            "TQ 日线同样无该日数据：分钟源本身为空（minline 未下载/该日未收录）"
        ),
        "tq_daily_codes": 0,
    }


def run_gapfill(
    dates: Sequence[str] | None = None,
    *,
    days: int = DEFAULT_DAYS,
    engine=None,
    now: datetime | None = None,
    codes: Sequence[str] | None = None,
    daily_ref: Callable[[str], dict[str, float]] | None = None,
    dry_run: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_sleep_s: float = DEFAULT_RETRY_SLEEP_S,
    sleep: Callable[[float], None] = time.sleep,
    deadline_s: float = DEFAULT_DEADLINE_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """把「已闭市且 TQ 0 行」的交易日用东财补齐。**默认真写库**（调度用）。

    落库全部走 `em_etf_minute.backfill(..., require_verified=True, daily_ref=...)`，
    这里不复制它的任何校验/写入逻辑，也不存在跳过对账的入口。

    审计口径：**20 只篮子必须 20 只齐全、每只 240 根且与 TQ 日线逐只对账**才允许写入；
    旧实现把 16/20 当 complete（80% 闸门），残缺日从此被当"已补过"跳过，必须按
    partial 报失败且不覆盖。`deferred_days` 只放「未闭市」，complete 单独进
    `complete_days`（旧实现把 complete 混进 deferred_days，等于把"已完成"说成"排队中"）。

    `deadline_s` 是**软预算不是硬上界**：预算用尽后不再开新的取数/重试/退避，但在途的
    那次 HTTP/RPC 只能靠自己的 timeout 收手，所以整轮可能越界。报告如实给出
    `elapsed_s` / `deadline_breached`；因预算耗尽而没处理的日期是 `deadline_exceeded`
    失败（`ok=False`），绝不当成功。TQ 日线参照与逐日对账共用同一个绝对截止时刻，
    不会"跨日各续一份预算"把 deadline 撑破。

    0 行日期失败时附 `attribution`（`tq_source_empty` / `collector_missing` /
    `tq_reference_unavailable` / `unknown`）与 `attribution_detail`：库里的 0 行
    不等于「TQ 源空」，告警必须能区分源空与采集漏。

    返回报告：`ok`、每天的 `state`/`result`/`attempts`/`reason`/`attribution`、
    `written_days`、`complete_days`、`failed_days`、`deferred_days`、`empty_source_days`、
    `attempts`、`deadline_s`/`elapsed_s`/`deadline_exhausted`/`deadline_breached`。
    """
    em = _em()
    eng = _require_engine(engine)
    now = _app_now(now)
    today = now.date()
    codes = list(codes) if codes is not None else _basket_codes()
    gate = _coverage_gate(len(codes))
    planned = _candidate_dates(dates, days, today)

    states: list[dict[str, Any]] = [
        classify_day(d, today=today, engine=eng, gate=gate, codes=codes) for d in planned
    ]
    pending = [s for s in states if s["state"] == ST_TQ_EMPTY]

    started = monotonic()
    deadline_at = started + max(1.0, float(deadline_s))

    if pending and daily_ref is None:
        # 唯一路径：就地构造 TQ 日线参照。绝不退化成"不核对就写"。
        # 参照取数**共用本轮的剩余预算**（deadline_at）：跨日各续 90s 会直接撑破
        # deadline，让"整轮有上界"变成一句空话。
        left = max(0.0, deadline_at - monotonic())
        daily_ref = em.tq_daily_ref(
            [str(c) for c in codes],
            budget_s=min(DEFAULT_TQ_REF_BUDGET_S, left),
            deadline_at=deadline_at,
        )
    # 归因用的是同一份参照的缓存副本：backfill 拿到的仍是调用方给的那个对象
    # （不改变既有契约），失败日的 0 行归因只额外问一次（成功结果会被缓存）。
    attr_ref = _cache_ref(daily_ref)

    attempts = 0
    deadline_stop = False
    max_attempts = max(1, int(max_attempts))
    while pending and attempts < max_attempts:
        remaining = deadline_at - monotonic()
        if remaining <= 0:
            # 预算在本次尝试之前就没了（分类/构造参照吃掉了时间）：一次都不再开。
            deadline_stop = True
            break
        attempts += 1
        try:
            rep = em.backfill(
                [s["date"] for s in pending],
                codes=[str(c) for c in codes],
                engine=eng,
                now=now,
                dry_run=dry_run,
                daily_ref=daily_ref,
                require_verified=True,  # 写死：自动补缺不接受未经对账的数据
                fetch_budget_s=remaining,  # 取数阶段只能花剩下的预算，不许把整轮吃掉
            )
        except Exception as e:  # noqa: BLE001 - 单轮异常不该炸掉整个调度
            logger.warning("[etf-gapfill] backfill 抛异常（按可重试失败处理）: %s", e)
            rep = {
                "days": [
                    {"date": s["date"], "action": "skipped", "reason": "fetch_failed",
                     "error": f"{type(e).__name__}: {e}"[:200]}
                    for s in pending
                ]
            }
        by_day = {str(d.get("date")): d for d in (rep.get("days") or [])}
        for s in pending:
            s["backfill"] = by_day.get(s["date"]) or {
                "action": "skipped",
                "reason": "missing_in_report",
            }
            s["attempts"] = attempts
        pending = [s for s in pending if _retryable(s, dry_run)]
        if pending and attempts < max_attempts:
            left = deadline_at - monotonic()
            if left <= 0:
                deadline_stop = True
                break
            # 退避也不能越过 deadline：睡到预算尽头就停，不再多开一轮。
            sleep(min(float(retry_sleep_s), left))
            if deadline_at - monotonic() <= 0:
                deadline_stop = True
                break

    elapsed_s = monotonic() - started
    # `deadline_s` 是**软预算**：它只保证「不再开新活」，不保证在途的那次 HTTP/RPC
    # 立刻返回。所以这里把真实耗时和是否越界都写进报告，不把软预算说成硬上界。
    deadline_breached = elapsed_s > float(deadline_s)
    deadline_exceeded = bool(pending and deadline_stop)
    if deadline_exceeded:
        # 预算用尽后仍可重试的失败：明确标 deadline_exceeded（而不是原样的瞬时原因），
        # 并且它仍然是失败（`ok=False`），不许因为「时间到了」就说成功。
        for s in pending:
            s["backfill"] = {"action": "skipped", "reason": "deadline_exceeded"}

    days_report: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    written: list[str] = []
    completed: list[str] = []
    deferred: list[str] = []
    empty_source: list[str] = []

    for s in states:
        item = {
            "date": s["date"],
            "state": s["state"],
            "rows": s.get("rows", 0),
            "codes": s.get("codes", 0),
            "sources": s.get("sources", []),
            "null_source_rows": s.get("null_source_rows", 0),
            "null_amount_rows": s.get("null_amount_rows", 0),
            "missing_codes": s.get("missing_codes", []),
            "minutes": s.get("minutes", 0),
            "amount_minutes": s.get("amount_minutes", 0),
            "detail": s.get("detail"),
            "attempts": int(s.get("attempts") or 0),
            "result": None,
            "reason": None,
            "verified": None,
            "attribution": None,
            "attribution_detail": None,
        }
        entry = s.get("backfill") or {}
        if s["state"] == ST_TQ_EMPTY:
            empty_source.append(s["date"])
            action = entry.get("action")
            reason = entry.get("reason")
            item["verified"] = entry.get("verified")
            if action in ("written", "planned"):
                item["result"] = "backfilled" if action == "written" else "planned"
                item["rows"] = int(entry.get("rows") or 0)
                item["etfs"] = int(entry.get("etfs") or 0)
                item["verified"] = bool(entry.get("verified"))
                if action == "written":
                    written.append(s["date"])
                _emit(
                    "backfilled",
                    date=s["date"],
                    rows=item["rows"],
                    etfs=item.get("etfs", 0),
                    verified=item["verified"],
                    attempts=item["attempts"],
                )
            elif reason in ("date_already_ingested", "date_partially_ingested"):
                # 分类与写入之间的并发先到者。以库里的真实状态为准重新判定。
                after = classify_day(s["date"], today=today, engine=eng, gate=gate, codes=codes)
                if after["state"] == ST_COMPLETE:
                    item.update(state=ST_COMPLETE, result="complete", rows=after["rows"],
                                codes=after["codes"], sources=after["sources"],
                                minutes=after["minutes"], detail=None)
                    completed.append(s["date"])
                else:
                    item.update(result="partial_conflict", reason=reason,
                                rows=after["rows"], sources=after["sources"],
                                null_source_rows=after.get("null_source_rows", 0),
                                missing_codes=after.get("missing_codes", []),
                                detail=after.get("detail") or "该日已被其它写入方占住但数据残缺：不覆盖，报失败")
                    failures.append(item)
                    _publish_gap_event(
                        s["date"], "partial_conflict", item["detail"],
                        sources=after["sources"], null_source_rows=after.get("null_source_rows", 0),
                    )
            else:
                item.update(result="failed", reason=reason or "unknown")
                item["detail"] = (
                    entry.get("error") or entry.get("reconcile_error")
                    or entry.get("detail") or item["detail"] or ""
                )[:300]
                item["missing_codes"] = entry.get("missing_codes") or item["missing_codes"]
                # 0 行不许直接说成「TQ 空源」：先归因（源空 / 采集漏 / 无从判断），
                # 告警里带上归因字段，处置方向才分得清。
                attribution = attribute_tq_empty(s["date"], daily_ref=attr_ref, codes=codes)
                item.update(attribution)
                failures.append(item)
                _emit(
                    "tq_empty_attribution",
                    date=s["date"],
                    attribution=item["attribution"],
                    tq_daily_codes=item["tq_daily_codes"],
                    detail=item["attribution_detail"],
                )
                _publish_gap_event(
                    s["date"],
                    "empty_source",
                    f"{item['reason']}: {item['detail']}",
                    attempts=item["attempts"],
                    missing_codes=item["missing_codes"],
                    attribution=item["attribution"],
                    attribution_detail=item["attribution_detail"],
                )
        elif s["state"] in _BENIGN_STATES:
            item["result"] = s["state"]
            if s["state"] == ST_COMPLETE:
                completed.append(s["date"])
                _emit("complete", date=s["date"], rows=item["rows"], sources=item["sources"])
            else:
                deferred.append(s["date"])
                _emit("deferred_today", date=s["date"], detail=item["detail"])
        else:
            # 假日 / 残缺日 / 日期非法 / 读库失败 / 日历不可用：一律显式失败。
            item["result"] = s["state"]
            item["reason"] = s["state"]
            failures.append(item)
            kind = "partial_day" if s["state"] == ST_PARTIAL else s["state"]
            _emit(kind, date=s["date"], rows=item["rows"], sources=item["sources"],
                  detail=item["detail"])
            if s["state"] in (ST_PARTIAL, ST_HOLIDAY):
                _publish_gap_event(s["date"], kind, str(item["detail"] or s["state"]),
                                   rows=item["rows"], sources=item["sources"],
                                   null_source_rows=item["null_source_rows"],
                                   null_amount_rows=item["null_amount_rows"],
                                   missing_codes=item["missing_codes"])
        days_report.append(item)

    report: dict[str, Any] = {
        "ok": not failures,
        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "days_requested": planned,
        "gate_etfs": gate,
        "expected_etfs": len(codes),
        "attempts": attempts,
        "max_attempts": max_attempts,
        # deadline 是软预算，不是硬上界：三个字段一起看才不会误读。
        "deadline_s": float(deadline_s),
        "elapsed_s": round(float(elapsed_s), 3),
        "deadline_exhausted": deadline_exceeded,
        "deadline_breached": deadline_breached,
        "deadline_exceeded": deadline_exceeded,  # 兼容旧字段名，语义同 deadline_exhausted
        "written_days": written,
        "complete_days": completed,
        "deferred_days": deferred,
        "empty_source_days": empty_source,
        "failed_days": [f["date"] for f in failures],
        "failures": failures,
        "days": days_report,
        "dry_run": bool(dry_run),
    }
    if empty_source and not written:
        _emit("empty_source", days=",".join(empty_source), written=0,
              failed=len(failures),
              attribution=",".join(
                sorted({str(f.get("attribution")) for f in failures if f.get("attribution")})
              ) or "unknown")
    _remember(report)
    return report


# ── 超出兜底窗口的空洞：巡检上报（补不了也要看得见） ────────────────────

def _em_window_days(today: date) -> list[str]:
    """东财滚动窗口内、且已闭市的交易日（`MAX_NDAYS` 含当日，故最多 4 个）。"""
    from src.collectors.tq_etf_watch import recent_trade_dates

    cutoff = today.strftime("%Y%m%d")
    return [d for d in recent_trade_dates(_em().MAX_NDAYS, end=today) if d < cutoff]


def survey_unfillable_gaps(
    *,
    now: datetime | None = None,
    engine=None,
    days: int = SURVEY_DAYS,
    codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """巡检比兜底窗口更早的空洞：东财补不到，必须显式上报而不是当作"最近没缺口"。

    两类历史问题都要看得见（**只读**，一行都不写、也不触发补缺）：

    - `unfillable_days`：0 行日 —— 东财也补不到，需人工补数；
    - `incomplete_days`：**残缺日**（16/20、金额 NULL、分钟不全…）—— 自动补缺按纪律
      绝不覆盖别人的行，所以它永远不会自己变好；不能因为「不是 0 行」就从视野里消失，
      否则库里那半天数据会静默地一直当下游基线。逐日带 `detail` 上报。

    不计入 `ok`（这不是本模块能修的范围），但会打 ERROR 日志 + 可观测事件。
    """
    em = _em()
    eng = _require_engine(engine)
    now = _app_now(now)
    today = now.date()
    codes = list(codes) if codes is not None else _basket_codes()
    gate = _coverage_gate(len(codes))
    inside = set(_em_window_days(today))
    older = [d for d in _candidate_dates(None, days, today) if d not in inside]

    unfillable: list[str] = []
    incomplete: list[dict[str, Any]] = []
    for day in older:
        st = classify_day(day, today=today, engine=eng, gate=gate, codes=codes)
        if st["state"] == ST_TQ_EMPTY:
            unfillable.append(day)
        elif st["state"] == ST_PARTIAL:
            incomplete.append({
                "date": day,
                "rows": st.get("rows", 0),
                "codes": st.get("codes", 0),
                "null_amount_rows": st.get("null_amount_rows", 0),
                "detail": st.get("detail"),
            })
    if unfillable:
        logger.error(
            "[etf-gapfill] unfillable_gaps days=%s 超出东财滚动窗口(%s 个交易日)，需人工补数",
            ",".join(unfillable), em.MAX_NDAYS,
        )
        _publish_gap_event(
            f"{unfillable[0]}~{unfillable[-1]}",
            "unfillable_gaps",
            f"超出兜底窗口的 0 行交易日 {len(unfillable)} 个，东财补不到",
            days=unfillable,
        )
    if incomplete:
        # 不覆盖是纪律，但「不覆盖」不等于「不用管」：残缺历史必须进可观测面。
        _emit(
            "incomplete_history",
            days=",".join(i["date"] for i in incomplete),
            count=len(incomplete),
            null_amount_rows=sum(int(i["null_amount_rows"] or 0) for i in incomplete),
        )
        _publish_gap_event(
            f"{incomplete[0]['date']}~{incomplete[-1]['date']}",
            "incomplete_history",
            f"超出兜底窗口的历史残缺日 {len(incomplete)} 个：按纪律不覆盖，需人工评估是否重采",
            days=[i["date"] for i in incomplete],
            incomplete=incomplete,
        )
    return {
        "surveyed": len(older),
        "window_days": sorted(inside),
        "unfillable_days": unfillable,
        "incomplete_days": incomplete,
    }


# ── 错过 15:35 收盘定型：有限窗口补跑（TQ 自身） ─────────────────────────

def _default_close_runner() -> dict[str, Any]:
    from src.collectors import tq_etf_watch

    return tq_etf_watch.run_close()


def catch_up_close(
    *,
    now: datetime | None = None,
    engine=None,
    runner: Callable[[], dict[str, Any]] | None = None,
    dry_run: bool = False,
    codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """收盘定型（15:35）错过时，在 [15:35, 22:00] 窗口内补跑一次 TQ 采集。

    只处理**当日**：`run_close` 是幂等的同源 upsert（`write_minute` 只更新
    `source = tdx_tq` 的行），所以重跑不会覆盖任何异源/用户数据；一旦发现异源行，
    `write_minute` 返回 -2，这里也提前停手并记录 `foreign_source`。
    """
    em = _em()
    eng = _require_engine(engine)
    now = _app_now(now)
    today = now.date()
    day = today.strftime("%Y%m%d")
    hm = now.strftime("%H%M")
    out: dict[str, Any] = {"date": day, "at": now.strftime("%Y-%m-%d %H:%M:%S"),
                           "ok": False, "action": "skipped", "reason": None}

    try:
        trading = _is_trading_day(today)
    except Exception as e:  # noqa: BLE001
        out.update(reason=ST_CALENDAR_ERROR, detail=str(e)[:200])
        return out
    if not trading:
        out.update(reason=ST_HOLIDAY, detail="非交易日，不补收盘定型", ok=True)
        return out
    if hm < CLOSE_HM:
        out.update(reason="too_early", detail=f"未到收盘定型时刻 {CLOSE_HM}", ok=True)
        return out
    if hm > RETRY_DEADLINE_HM:
        out.update(reason="out_of_window", detail=f"超过补跑窗口 {RETRY_DEADLINE_HM}", ok=True)
        return out

    codes = list(codes) if codes is not None else _basket_codes()
    gate = _coverage_gate(len(codes))
    try:
        ov = em.existing_overview(eng, day, min_codes=gate, codes=codes)
    except Exception as e:  # noqa: BLE001
        out.update(reason=ST_DB_ERROR, detail=f"读库失败: {e}"[:200])
        return out

    foreign = [s for s in (ov.get("sources") or []) if s != _SOURCE_TQ]
    if foreign:
        out.update(reason="foreign_source", sources=ov.get("sources"),
                   null_source_rows=ov.get("null_source_rows", 0),
                   detail="当日已有非 TQ/来源不明（NULL）的行，拒绝重跑收盘定型（同日不混源）",
                   ok=True)
        _emit("foreign_source", date=day, sources=",".join(foreign),
              null_source_rows=ov.get("null_source_rows", 0))
        return out
    if ov.get("complete"):
        out.update(reason="already_complete", rows=ov.get("rows"), ok=True)
        _emit("close_complete", date=day, rows=ov.get("rows"))
        return out
    if dry_run:
        out.update(action="planned", reason="would_rerun_close",
                   rows=ov.get("rows"), ok=True)
        return out

    run = runner or _default_close_runner
    try:
        result = run()
    except Exception as e:  # noqa: BLE001
        out.update(action="failed", reason="close_runner_failed", detail=str(e)[:200])
        _publish_gap_event(day, "close_catchup_failed", out["detail"])
        return out
    rows = int((result or {}).get("rows") or 0) if isinstance(result, dict) else 0
    ok_run = bool((result or {}).get("ok")) if isinstance(result, dict) else bool(result)
    out.update(action="rerun", rows=rows, result=result, ok=bool(ok_run and rows >= 0))
    _emit("close_catchup", date=day, rows=rows, ok=out["ok"],
          had_rows=ov.get("rows"))
    if not out["ok"]:
        _publish_gap_event(day, "close_catchup_empty",
                           f"重跑 run_close 后仍为 0 行：{str(result)[:200]}")
    return out


def run_after_close(
    *,
    now: datetime | None = None,
    engine=None,
    close_runner: Callable[[], dict[str, Any]] | None = None,
    days: int = DEFAULT_DAYS,
    dates: Sequence[str] | None = None,
    codes: Sequence[str] | None = None,
    daily_ref: Callable[[str], dict[str, float]] | None = None,
    dry_run: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_sleep_s: float = DEFAULT_RETRY_SLEEP_S,
    sleep: Callable[[float], None] = time.sleep,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> dict[str, Any]:
    """收盘后一轮：先补跑错过的 15:35 定型，再补已闭市日的 TQ 空源。"""
    now = _app_now(now)
    eng = _require_engine(engine)
    close = catch_up_close(now=now, engine=eng, runner=close_runner, dry_run=dry_run, codes=codes)
    gap = run_gapfill(
        dates,
        days=days,
        engine=eng,
        now=now,
        codes=codes,
        daily_ref=daily_ref,
        dry_run=dry_run,
        max_attempts=max_attempts,
        retry_sleep_s=retry_sleep_s,
        sleep=sleep,
        deadline_s=deadline_s,
    )
    survey = survey_unfillable_gaps(now=now, engine=eng, codes=codes)
    return {
        "ok": bool(close.get("ok")) and bool(gap.get("ok")),
        "close": close,
        "gapfill": gap,
        "unfillable_days": survey["unfillable_days"],
        "incomplete_days": survey["incomplete_days"],
    }


# ── 启动恢复（后台线程，不阻塞 lifespan） ────────────────────────────────

def startup_recovery(
    *,
    now: datetime | None = None,
    engine=None,
    close_runner: Callable[[], dict[str, Any]] | None = None,
    days: int = DEFAULT_DAYS,
    dry_run: bool = False,
    force: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """启动时的后台恢复：补跑收盘定型 + 补已闭市空源。

    - 交易时段内直接跳过（TQ 背后只有一个客户端进程，启动预热别和 60s 采集抢）；
    - 15:35–16:30 的 TQ 收盘定型重负载窗口也跳过：此刻 `run_close` 与 TQ 启动自愈
      正在跑同一个客户端，再并行一轮 `catch_up_close` 既抢资源又可能重复写；
    - 其它时间只做一次，没有循环、没有无限重试。
    """
    now = _app_now(now)
    if not force and _in_session(now):
        logger.info("[etf-gapfill] 启动恢复跳过：交易时段 %s，不抢 TQ 客户端", now.strftime("%H:%M"))
        return {"ok": True, "action": "skipped", "reason": "in_session",
                "at": now.strftime("%Y-%m-%d %H:%M:%S")}
    if not force and _in_tq_close_window(now):
        logger.info("[etf-gapfill] 启动恢复跳过：TQ 收盘定型窗口 %s，交给定时任务", now.strftime("%H:%M"))
        return {"ok": True, "action": "skipped", "reason": "tq_close_window",
                "at": now.strftime("%Y-%m-%d %H:%M:%S")}
    return run_after_close(
        now=now,
        engine=engine,
        close_runner=close_runner,
        days=days,
        dry_run=dry_run,
        **kwargs,
    )


_STARTED_LOCK = threading.Lock()
_STARTED = False


def start_startup_recovery(
    delay_s: float = STARTUP_DELAY_S,
    *,
    wait_for: threading.Thread | None = None,
    wait_timeout_s: float = 300.0,
    name: str = "etf-minute-gapfill-recovery",
    engine_provider: Callable[[], Any] | None = None,
    **kwargs: Any,
) -> threading.Thread | None:
    """起一个 daemon 线程做启动恢复。**立即返回**，绝不阻塞 lifespan。

    只允许起一次（幂等）；参数透传给 `startup_recovery`（测试注入 `now`/`engine`）。
    `engine_provider` 是 startup 的注入点：把「本进程用的只读 engine」交给 core，
    避免 core 在运行时反向 import web 层。

    `wait_for` 用来等先前的 TQ 启动自愈线程跑完，避免"东财先写、TQ 后到被判异源"
    的无谓冲突 —— 等待在 worker 线程里做，调用方不受影响。
    """
    if engine_provider is not None:
        configure_engine(engine_provider)

    global _STARTED
    with _STARTED_LOCK:
        if _STARTED:
            return None
        _STARTED = True

    def _worker() -> None:
        try:
            if wait_for is not None and wait_for.is_alive():
                wait_for.join(wait_timeout_s)
            if delay_s > 0:
                time.sleep(delay_s)
            startup_recovery(**kwargs)
        except Exception:  # noqa: BLE001 - 后台自愈失败不能影响进程
            logger.warning("[etf-gapfill] 启动恢复失败（不影响启动）", exc_info=True)

    thread = threading.Thread(target=_worker, name=name, daemon=True)
    thread.start()
    return thread


# ── 调度注册 ─────────────────────────────────────────────────────────────

def job_morning() -> dict[str, Any]:
    """盘前 08:40：只补已闭市日（含昨日）。今天还没收盘，不碰。"""
    try:
        out = run_gapfill(days=DEFAULT_DAYS)
        survey = survey_unfillable_gaps()
        out["unfillable_days"] = survey["unfillable_days"]
        out["incomplete_days"] = survey["incomplete_days"]
        return out
    except Exception as e:  # noqa: BLE001
        logger.error("[etf-gapfill] 盘前补缺失败: %s", e)
        return {"ok": False, "error": str(e)[:300]}


def job_after_close() -> dict[str, Any]:
    """收盘后 16:50：错过的 15:35 定型 + 已闭市空源。"""
    try:
        return run_after_close()
    except Exception as e:  # noqa: BLE001
        logger.error("[etf-gapfill] 收盘后补缺失败: %s", e)
        return {"ok": False, "error": str(e)[:300]}


def job_late() -> dict[str, Any]:
    """补漏 18:50：同收盘后一轮，给一次性失败留第二次机会（窗口到 22:00）。"""
    try:
        return run_after_close()
    except Exception as e:  # noqa: BLE001
        logger.error("[etf-gapfill] 补漏轮失败: %s", e)
        return {"ok": False, "error": str(e)[:300]}


#: (job_id, 函数, 时, 分, 名称)。三个时间点都在 TQ 重负载窗口（15:35–16:30）之外。
JOB_SPECS: tuple[tuple[str, Callable[[], dict[str, Any]], int, int, str], ...] = (
    ("etf-minute-gapfill-am", job_morning, 8, 40, "ETF分钟空源补缺(盘前)"),
    ("etf-minute-gapfill-close", job_after_close, 16, 50, "ETF分钟空源补缺(收盘后)"),
    ("etf-minute-gapfill-late", job_late, 18, 50, "ETF分钟空源补缺(补漏)"),
)


def register(
    scheduler,
    *,
    engine_provider: Callable[[], Any] | None = None,
) -> bool:
    """把三个 cron 挂到调度器。**不做任何取数/写库动作**（装配期零 I/O）。

    `engine_provider` 是 startup 的注入入口（例如
    `lambda: src.db.session.engine`），core 由此获得本进程的只读库；不传时各 job
    在运行时按 `_require_engine` 解析（显式参数 / sys.modules 中已构造的中立层 engine）。
    """
    if engine_provider is not None:
        configure_engine(engine_provider)
    if scheduler is None:
        logger.warning("[etf-gapfill] 调度器不可用，跳过注册")
        return False
    for job_id, fn, hour, minute, name in JOB_SPECS:
        scheduler.add_job(
            fn,
            "cron",
            day_of_week="mon-fri",
            hour=hour,
            minute=minute,
            id=job_id,
            name=name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=900,
        )
    return True
