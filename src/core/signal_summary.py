# -*- coding: utf-8 -*-
"""今日系统信号摘要(设计稿 §7.3「被动注入」, 2026-09-01)。

把 5 块市场级信号(情绪周期/市场主线/全市场三榜/涨停复盘/指数资金)聚合渲染成
≤800 字纯文本, 盘后 cron 预生成落库 signal_summary_daily, 对话热路径只读 text
注入 system message(毫秒级)。

## 设计要点
- 5 块全部复用现有生产接口, **零新采集**;
- 每块独立 try/except, 失败 → data_status="missing", 不阻塞其它块;
- data_status 三态: ok(完整) / partial(部分) / missing(缺失), AI 看字段自识别;
- text 是渲染后的纯文本(无 emoji, 精简结论), blocks 是结构化数据(供审码/扩展)。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
#: A 股连续竞价收盘时刻（用于判定「最近已收盘交易日」）。
MARKET_CLOSE_HOUR = 15
_CLOSE_MINUTES = MARKET_CLOSE_HOUR * 60
_ISO_DAY_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
#: **显式标注**的数据日期才算可信基准日（2026-11-10 时效审计）：
#: 只认「数据日期 / 数据基准日 / 统计日期 / 行情日期 / 数据截至」这几种标签，
#: 不认散落在正文里的任意参考日期（历史对比日、涨跌停日、提示词里的例子日期），
#: 也不认本模块自己生成的叙述（「基准日 X」「最近交易日 Y」）—— 否则会被自己污染。
_LABELED_DAY_RE = re.compile(
    r"(?:数据日期|数据基准日|统计日期|行情日期|数据截至)\s*[:：=]?\s*(\d{4})-(\d{2})-(\d{2})(?!\d)"
)
#: 渲染时贴在块正文尾部的时效标签（如「〔基准日 2026-11-05·落后 2 个交易日·仅背景〕」）。
#: 读回旧快照时必须先剥掉**上一次渲染**的标签再按当前时间重算 —— 否则 11-10 收盘写下的
#: 「今日已收盘·本块与今日一致」会被 11-11 的对话原样带出，与新的时效结论自相矛盾。
_BLOCK_TAG_RE = re.compile(r"\s*〔[^〕]*(?:基准日|无数据日|不计入时效核验)[^〕]*〕")


# ──────────────────────── 证据时效硬校验（2026-11-10）────────────────────────
# 用户现场：11-05 的旧证据被当成 11-10 的「今日盘面」。提示词叮嘱不算校验，
# 所以时效在这里用**代码**判定：每一条注入路径各自用自己的真实基准日比对
# 交易日历（单一事实源 src.core.ladder_freshness），得出：
#   - 基准日是不是今天 / 是不是最近交易日；
#   - 落后几个交易日；
#   - 无交易日历（年份未覆盖）时**不猜**、明写无法判定。
# 历史数据允许在场（历史对比有价值），但必须写真实基准日 + 过期说明，
# 不得冒充今日，也不许被静默删除。


def opportunity_header(latest, *, now=None) -> str:
    """按快照真实基准日标注机会候选（盘中 / 休市 / 日历缺失各自如实），不猜。

    2026-11-10 时效审计：盘中 14:40 的快照曾被写成「今日机会候选」并暗示可直接用于
    今日结论；上一已收盘交易日（昨收）又被写成「历史·落后 1 个交易日」。现在四态分开：
    今日（已收盘）/ 今日盘中（未收盘）/ 前日（最近已收盘）/ 历史（落后 N 个交易日）。
    """
    fresh = resolve_freshness(latest, now=now)
    if fresh.get("base_date") is None:
        return "【机会候选·基准日未知】快照未标注日期，时效不可判定，不得当作今日候选"
    if fresh.get("calendar") != "ok":
        return (
            f"【机会候选·基准日 {latest}】时效未判定（交易日历不可用；"
            "不写今日，也不得当作今日结论依据）"
        )
    if fresh.get("anomaly"):
        return (
            f"【机会候选·基准日异常】{latest} 晚于今日/最近交易日"
            "（未来日期或时区错位，已从基准日候选里排除）"
        )
    if fresh.get("usable_for_today"):
        return f"【今日机会候选】{latest}"
    if fresh.get("is_today"):
        return (
            f"【今日盘中机会候选】{latest}（今日盘中未收盘，会变，"
            "非收盘定稿，不得作为今日结论）"
        )
    if fresh.get("is_latest_closed"):
        if fresh.get("session") == "intraday":
            return (
                f"【前日机会候选】基准日 {latest}"
                f"（最近已收盘交易日；今日 {fresh['today']} 盘中未收盘，非今日数据）"
            )
        return (
            f"【最近交易日机会候选】基准日 {latest}"
            f"（今日 {fresh['today']} 休市，非今日新增数据）"
        )
    lag = fresh.get("lag_trading_days")
    return (
        f"【历史机会候选】基准日 {latest}"
        f"（落后 {lag if lag is not None else '?'} 个交易日，非今日，仅作背景/对比）"
    )


def now_cn(now: datetime | None = None) -> datetime:
    """北京时间当前时刻。传入 naive datetime 视为北京时间（测试注入用）。"""
    if now is None:
        return datetime.now(_CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=_CST)
    return now.astimezone(_CST)


def _parse_day(value) -> date | None:
    """YYYY-MM-DD / YYYYMMDD / date / datetime → date；其它 → None（不猜）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    m = _ISO_DAY_RE.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def extract_dates(text: str) -> list[str]:
    """抽出文本里所有 ISO 日期（YYYY-MM-DD），去重保序。"""
    out: list[str] = []
    for m in _ISO_DAY_RE.finditer(str(text or "")):
        day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if day not in out:
            out.append(day)
    return out


def extract_labelled_dates(text: str) -> list[str]:
    """只抽**显式标注**的数据日期（数据日期/统计日期/数据截至…），去重保序。

    2026-11-10 时效审计：正文里任何一处日期（历史对比日、提示词举例日）都不许当基准日，
    否则 11-10 的情绪信封会拿「对比 2026-10-20 那次高潮」里的 10-20 当数据日。
    """
    out: list[str] = []
    for m in _LABELED_DAY_RE.finditer(str(text or "")):
        day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if day not in out:
            try:
                date.fromisoformat(day)
            except ValueError:
                continue
            out.append(day)
    return out


def pick_base_date(candidates, *, now: datetime | None = None) -> str | None:
    """候选日期 → 唯一可信基准日：未来日期剔除，多值冲突 → None（不猜）。

    未来日期一定不是「已有数据」的基准日（库内时区错位/脏数据/生成器把明日预写），
    所以先剔掉；剩下多于一个值时无法判断哪个才是真基准，一律按未知处理。
    """
    today = now_cn(now).date()
    valid: list[str] = []
    for c in candidates or []:
        d = _parse_day(c)
        if d is None or d > today:
            continue
        s = d.isoformat()
        if s not in valid:
            valid.append(s)
    if len(valid) > 1:
        logger.info("[时效] 基准日候选冲突 %s → 按未知处理", valid)
        return None
    return valid[0] if valid else None


def evidence_item_dates(items, *, now: datetime | None = None) -> dict:
    """逐条证据抽**显式标注**的数据日期，并给出「整体基准日」是否成立。

    2026-11-10 时效终审 P0：一个分条标了日期，不许给同一封信/同一份摘要里**没有日期**
    的其他分条盖同一个戳（「广度块 11-10」≠ 「情绪周期也是 11-10」）。因此公共基准日
    只在两种情况成立：
      · 每条证据都有自己的显式日期，且这些日期完全一致（单一值）；
    否则整体基准日 = None（未知），并把每条的真实情况（有日期/没日期）交回调用方，
    由调用方逐条标注 —— 未知就写未知，不拿别的条目的日期顶上。

    只认 `extract_labelled_dates` 的标签（数据日期/统计日期/数据截至…），正文里的
    参考日期（「对比 2026-10-20」）与裸日期一律不当基准日。

    返回：
      base_date  整体基准日（未知 → None）
      dated      {条目: 该条显式日期}
      undated    没有显式日期的条目
      bases      出现过的显式日期（去重排序）
    """
    dt = now_cn(now)
    dated: dict[str, str] = {}
    undated: list[str] = []
    for it in items or []:
        s = str(it or "").strip()
        if not s:
            continue
        labels = extract_labelled_dates(s)
        day = pick_base_date(labels, now=dt) if labels else None
        if day:
            dated[s] = day
        else:
            undated.append(s)
    bases = sorted(set(dated.values()))
    base = bases[0] if (bases and not undated and len(bases) == 1) else None
    return {"base_date": base, "dated": dated, "undated": undated, "bases": bases}


def _expected_latest(now: datetime) -> tuple[date | None, str]:
    """期望最新交易日（<= now 当日）。日历未覆盖年份 → (None, "unavailable")。"""
    try:
        from src.core.ladder_freshness import expected_latest_trading_date

        return _parse_day(expected_latest_trading_date(now)), "ok"
    except Exception as exc:  # noqa: BLE001 - 日历缺失/未覆盖年份
        logger.warning("[时效] 交易日历不可用，无法判定最新交易日: %s", exc)
        return None, "unavailable"


def _trading_lag(base: date, expected: date) -> int | None:
    try:
        from src.core.ladder_freshness import missing_trading_days

        return len(
            missing_trading_days(base.strftime("%Y%m%d"), expected.strftime("%Y%m%d"))
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[时效] 缺失交易日计数失败: %s", exc)
        return None


def market_session(now: datetime | None = None) -> dict:
    """当前盘口时段与「最近已收盘交易日」—— 全部由交易日历算出，不靠提示词。

    返回 session:
      - ``closed``      今日是交易日且已过 15:00 → closed_day = 今天
      - ``intraday``    今日是交易日但未收盘 → closed_day = **前一个**交易日（昨收）
      - ``non_trading`` 今日休市 → closed_day = 最近交易日
      - ``unknown``     交易日历不可用 → closed_day = None（不猜）
    """
    dt = now_cn(now)
    today = dt.date()
    out = {
        "today": today.isoformat(),
        "weekday": _WEEKDAY_CN[today.weekday()],
        "session": "unknown",
        "closed_day": None,
        "expected": None,
        "calendar": "unavailable",
    }
    expected, calendar = _expected_latest(dt)
    if calendar != "ok" or expected is None:
        return out
    out["calendar"] = "ok"
    out["expected"] = expected.isoformat()
    try:
        from src.core.trading_calendar import is_trading_day, prev_trading_day

        trading = bool(is_trading_day(today)) and today.weekday() < 5
        if trading:
            if dt.hour * 60 + dt.minute >= _CLOSE_MINUTES:
                out["session"] = "closed"
                closed = today
            else:
                out["session"] = "intraday"
                closed = prev_trading_day(today)
        else:
            out["session"] = "non_trading"
            closed = expected
    except Exception as exc:  # noqa: BLE001 - 日历查询失败 → 不判（不猜）
        logger.warning("[时效] 盘中/收盘状态判定失败: %s", exc)
        return out
    out["closed_day"] = closed.isoformat()
    return out


def resolve_freshness(base_date, *, now: datetime | None = None) -> dict:
    """判定某条证据的真实基准日相对「北京现在」的时效（盘中/收盘分开判）。

    返回 dict:
      base_date / today / weekday / expected / closed_day / session / calendar
      is_today(bool|None)         base == 北京今天（日历不可用 → None，不写今日）
      is_current(bool|None)       base == 最近交易日(<=今日)
      is_latest_closed(bool|None) base == 最近**已收盘**交易日（盘中即昨收）
      stale(bool|None)            base 早于最近已收盘交易日
      lag_trading_days(int|None)  落后最近已收盘交易日的交易日数
      usable_for_today(bool)      **代码核验**通过：base == 今天 且 今天已收盘
      anomaly(bool)               base 晚于今日/最近交易日（未来日期/时区错位）
      label / note
    任何一项判不出来就是 None / False，**不回落墙钟猜测**。
    usable_for_today 只在「今日 + 已收盘」成立；盘中快照、昨收、历史一律 False。
    """
    dt = now_cn(now)
    sess = market_session(dt)
    base = _parse_day(base_date)

    out: dict = {
        "base_date": base.isoformat() if base else None,
        "today": sess["today"],
        "weekday": sess["weekday"],
        "expected": sess["expected"],
        "closed_day": sess["closed_day"],
        "session": sess["session"],
        "calendar": sess["calendar"],
        "is_today": None,
        "is_current": None,
        "is_latest_closed": None,
        "stale": None,
        "lag_trading_days": None,
        "usable_for_today": False,
        "anomaly": False,
        "label": "",
        "note": "",
    }

    if base is None:
        out["label"] = "基准日未知"
        out["note"] = (
            "该域未标注数据基准日（未知），无法判定新旧 —— "
            "不得当作今日盘面，需人工核对后再用"
        )
        return out

    if sess["calendar"] != "ok" or sess["closed_day"] is None:
        # 日历不可用：连「今天开不开市」都不知道，绝不写今日。
        out["label"] = f"基准日 {base.isoformat()}"
        out["note"] = (
            f"基准日 {base.isoformat()}；北京日期 {sess['today']}；"
            "交易日历不可用，无法判定今天是否开市、最近交易日/最近已收盘交易日是哪天 —— "
            "该基准日不得写成今日，也不得当作今日结论依据，需人工核对"
        )
        return out

    today = dt.date()
    expected = date.fromisoformat(sess["expected"])
    closed = date.fromisoformat(sess["closed_day"])
    intraday = sess["session"] == "intraday"

    out["is_today"] = base == today
    out["is_current"] = base == expected
    out["is_latest_closed"] = base == closed

    # ① 基准日 == 今天（三态：已收盘 / 盘中未收盘 / 休市冲突）
    if base == today:
        if sess["session"] == "closed":
            out["usable_for_today"] = True
            out["lag_trading_days"] = 0
            out["stale"] = False
            out["label"] = "今日"
            out["note"] = (
                f"基准日 {base.isoformat()}（今日，已收盘 15:00 后，与最近已收盘交易日一致；"
                "代码核验通过，可用于今日结论）"
            )
            return out
        if intraday:
            out["lag_trading_days"] = 0
            out["stale"] = False
            out["label"] = "今日盘中"
            out["note"] = (
                f"基准日 {base.isoformat()} 是今日盘中快照（今日 {sess['weekday']} 尚未收盘 15:00，"
                f"最近已收盘交易日为 {closed.isoformat()}）；盘中数据会变，"
                "不得写成收盘结论，也不得当作今日结论依据"
            )
            return out
        # 休市日却出现「今天就绪」的数据 → 日期冲突
        out["anomaly"] = True
        out["label"] = "基准日异常"
        out["note"] = (
            f"今日 {today.isoformat()}（{sess['weekday']}）休市，不存在今日行情数据；"
            f"基准日 {base.isoformat()} 与休市日冲突（库内日期异常/生成日误写成数据日），"
            "不得当作盘面"
        )
        return out

    # ② 晚于最近交易日（今日之后的未来日期 / 时区错位）
    if base > expected:
        out["anomaly"] = True
        out["label"] = "基准日异常"
        out["note"] = (
            f"基准日 {base.isoformat()} 晚于今日 {today.isoformat()}"
            f"（最近交易日 {expected.isoformat()}）—— 未来日期或时区错位，"
            "已从基准日候选里排除，不得当作今日盘面"
        )
        return out

    # ③ 基准日 == 最近已收盘交易日
    if base == closed:
        out["lag_trading_days"] = 0
        out["stale"] = False
        if intraday:
            out["label"] = "前日"
            out["note"] = (
                f"基准日 {base.isoformat()} 是最近已收盘交易日（相对今日 {today.isoformat()} 即前日；"
                f"今日 {sess['weekday']} 盘中未收盘，比它更新的只有盘中数据）；"
                "可作为最近收盘基准，禁止写成今日盘中/今日结论"
            )
        else:
            out["label"] = "最近交易日"
            out["note"] = (
                f"基准日 {base.isoformat()}（最近交易日；今日 {today.isoformat()} "
                f"{sess['weekday']} 休市，非今日新增数据，禁止写成「今日」）"
            )
        return out

    # ④ 基准日早于最近已收盘交易日 → 历史
    lag = _trading_lag(base, closed)
    out["lag_trading_days"] = lag
    out["stale"] = True
    out["label"] = "历史"
    out["note"] = (
        f"基准日 {base.isoformat()} 早于最近已收盘交易日 {closed.isoformat()}"
        f"（落后 {lag if lag is not None else '?'} 个交易日）；"
        "属历史快照，可作背景/对比，不得冒充今日盘面或直接当作今日结论依据"
    )
    return out


def time_anchor_block(now: datetime | None = None) -> str:
    """system 用的时间锚（北京时间 + 交易日历 + 盘口时段），全部由代码算出。

    2026-11-10 时效审计：盘中 14:40 曾把「今日」当成可下结论的收盘日；日历不可用时
    又照写「今日：…」暗示开市。现在盘中明确写「尚未收盘」+「最近已收盘交易日 = 前一日」，
    日历不可用时只写北京日期、明说无法判定，也不出现「今日开市/可用于今日结论」的断言。
    """
    dt = now_cn(now)
    sess = market_session(dt)
    lines = [
        "--- 当前时间（北京时间，代码计算，非模型推测/非宿主墙钟）---",
        f"现在：{dt:%Y-%m-%d %H:%M} {sess['weekday']}",
    ]
    if sess["calendar"] != "ok":
        lines.append(f"北京日期：{sess['today']}（{sess['weekday']}）")
        lines.append(
            "交易日历不可用：无法判定今天是否开市、最近交易日/最近已收盘交易日是哪天 —— "
            "任何数据基准日都不得写成今日，也不得当作今日结论依据"
        )
        return "\n".join(lines)

    today = sess["today"]
    expected = sess["expected"]
    closed = sess["closed_day"]
    if sess["session"] == "closed":
        lines.append(f"今日：{today}（{sess['weekday']}，交易日，已收盘 15:00 后）")
        lines.append(f"最近已收盘交易日：{closed}（即今日）")
        lines.append("今日收盘数据可作为今日结论依据（代码核验：已过收盘时刻）")
    elif sess["session"] == "intraday":
        lines.append(f"今日：{today}（{sess['weekday']}，交易日，尚未收盘）")
        lines.append(f"最近已收盘交易日：{closed}（相对今日即「前日」）")
        lines.append(
            "今日盘中快照：随盘中变动，不代表收盘状态，不得作为今日结论依据；"
            "引用今日数据必须写明「盘中未收盘」"
        )
    else:
        lines.append(f"今日：{today}（{sess['weekday']}，休市）")
        lines.append(f"最近已收盘交易日：{closed}（今日休市，非今日新增数据）")
    lines.append(f"最近交易日（<=今日）：{expected}")
    return "\n".join(lines)


# 5 块顺序(决定 text 渲染顺序)
_BLOCK_ORDER = ("sentiment", "mainline", "market_scan", "limit_up", "indices_flow")


# ──────────────────────────── 各块采集 ────────────────────────────

async def _block_sentiment() -> dict:
    """块1 情绪周期: 涨停池指标 → classify_sentiment_cycle。"""
    from src.core.report_generator import _collect_limit_up_summary
    from src.core.sentiment_cycle import classify_sentiment_cycle

    try:
        summary = await _collect_limit_up_summary()
        if not isinstance(summary, dict) or summary.get("error"):
            return {"data_status": "missing", "content": "情绪周期数据缺失"}
        metrics = {
            "limit_up_count": summary.get("total"),
            "max_board_height": summary.get("max_days"),
            "break_rate": summary.get("break_rate"),
            "yesterday_board_perf": summary.get("yesterday_board_perf"),
            "losing_effect": summary.get("losing_effect"),
        }
        r = classify_sentiment_cycle(metrics)
        if r.get("cycle") == "数据不足":
            return {"data_status": "partial", "content": "情绪周期数据不足，无法判别"}
        content = (
            f"情绪周期：{r.get('cycle','?')}(置信度{r.get('confidence','?')})"
            f"。涨停{metrics.get('limit_up_count') or 0}家/"
            f"连板{metrics.get('max_board_height') or 0}板/"
            f"炸板率{metrics.get('break_rate') if metrics.get('break_rate') is not None else '?'}%"
        )
        return {
            "data_status": "ok",
            "content": content,
            "cycle": r.get("cycle"),
            "confidence": r.get("confidence"),
            "limit_up_count": metrics.get("limit_up_count"),
            "max_board_height": metrics.get("max_board_height"),
            "break_rate": metrics.get("break_rate"),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 情绪周期块失败: %s", e)
        return {"data_status": "missing", "content": "情绪周期数据缺失"}


async def _block_mainline() -> dict:
    """块2 市场主线: 涨停池 → aggregate_mainline, 取 Top3。"""
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector
        from src.core.market_mainline import aggregate_mainline

        pool = await asyncio.to_thread(MarketSentimentCollector().get_limit_up_pool)
        result = await asyncio.to_thread(aggregate_mainline, pool)
        ranked = result.get("ranked_groups") or []
        if not ranked:
            return {"data_status": "missing", "content": "市场主线数据缺失"}
        top = ranked[:3]
        parts = []
        for g in top:
            name = g.get("name", "?")
            cnt = g.get("limit_up_count", 0)
            leader = g.get("leader") or {}
            leader_name = leader.get("name") if isinstance(leader, dict) else None
            s = f"{name}({cnt}家)"
            if leader_name:
                s += f"龙头{leader_name}"
            parts.append(s)
        return {
            "data_status": "ok",
            "content": "市场主线：" + "/".join(parts),
            "top": [{"name": g.get("name"), "limit_up_count": g.get("limit_up_count")} for g in top],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 主线块失败: %s", e)
        return {"data_status": "missing", "content": "市场主线数据缺失"}


def _block_market_scan(db) -> dict:
    """块3 全市场三榜: 读 market_scan_ranks 最新快照(盘后 cron 已落库)。"""
    try:
        from src.db.models import MarketScanRank

        row = (
            db.query(MarketScanRank)
            # 2026-11-10 时效审计：未来日期的脏行（时区错位/预写）不许被选成「最新」，
            # 否则真实三榜全被一条 2026-12-01 挤掉，整份摘要还会被判基准日异常。
            .filter(MarketScanRank.snapshot_date <= now_cn().date())
            .order_by(MarketScanRank.snapshot_date.desc(), MarketScanRank.id.desc())
            .first()
        )
        if not row or not isinstance(row.payload, dict):
            return {"data_status": "missing", "content": "三榜数据缺失(盘后 15:30 生成)"}
        p = row.payload
        new_g = (p.get("new_g_points") or [])[:3]
        dark = (p.get("dark_top") or [])[:3]
        act = (p.get("activity_top") or [])[:3]

        def _syms(rows, key="symbol"):
            return "、".join(str(r.get(key, "")) for r in rows) or "无"

        dark_str = "、".join(
            f"{r.get('symbol')}({r.get('dark_net_wan')}万)" for r in dark
        ) or "无"
        act_str = "、".join(
            f"{r.get('symbol')}({r.get('level') or '-'})" for r in act
        ) or "无"
        content = (
            f"三榜：新G点(待确认)[{_syms(new_g)}]；"
            f"暗盘TOP[{dark_str}]；"
            f"机构活跃[{act_str}]"
        )
        base_day = _parse_day(getattr(row, "snapshot_date", None))
        return {
            "data_status": "ok",
            "content": content,
            # 2026-11-10 时效审计：块自带真实基准日，渲染时按块判定，不再让
            # 摘要标题拿「生成日」冒充数据基准日。
            "base_date": base_day.isoformat() if base_day else None,
            "new_g": [r.get("symbol") for r in new_g],
            "dark_top": [{"symbol": r.get("symbol"), "net_wan": r.get("dark_net_wan")} for r in dark],
            "activity_top": [{"symbol": r.get("symbol"), "level": r.get("level")} for r in act],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 三榜块失败: %s", e)
        return {"data_status": "missing", "content": "三榜数据缺失"}


async def _block_limit_up() -> dict:
    """块4 涨停复盘: 涨停家数/连板梯队/炸板率/跌停家数。

    2026-10-23 修复(严重): 原实现读 `summary.get("total")` 与 `summary.get("max_days")`，
    而 `MarketSentimentCollector.get_sentiment_summary()` 实际返回的键是
    `limit_up_count` 与 `max_streak` —— 键名不匹配 → 两个值都取到 None → 被 `or 0` 兜成 0，
    于是**每天摘要都写「0家涨停，最高0板」**（实测 2026-10-22 真实 44 家、10-23 真实 86 家）。
    现在改为按真实键名读，并顺带补齐梯队/炸板/跌停，任何一项缺失都写显式缺口而不是 0。
    """
    from src.core.report_generator import _collect_limit_up_summary

    try:
        summary = await _collect_limit_up_summary()
        if not isinstance(summary, dict) or summary.get("error"):
            return {
                "data_status": "missing",
                "content": "涨停复盘数据缺失（涨停池取数失败）",
            }

        # 真实键名：limit_up_count / max_streak / ladder / top_stocks / top_sectors
        total = summary.get("limit_up_count")
        max_days = summary.get("max_streak")
        if total is None or max_days is None:
            return {
                "data_status": "missing",
                "content": "涨停复盘数据缺失（涨停池字段不全）",
            }

        ladder = summary.get("ladder") or {}
        ladder_parts = [f"{k}板{v}家" for k, v in sorted(ladder.items(), key=lambda x: -int(x[0]))]
        content = f"涨停复盘：{total}家涨停，最高{max_days}板"
        if ladder_parts:
            content += "，梯队" + "/".join(ladder_parts)

        block: dict = {
            "data_status": "ok",
            "content": content,
            "total": total,
            "max_days": max_days,
            "ladder": {str(k): v for k, v in ladder.items()},
            "top_stocks": list(summary.get("top_stocks") or [])[:5],
            "caliber": summary.get("limit_up_source") or "unknown",
        }

        # 2026-11-10 时效终审 P0：涨停池本身不返回交易日，**不许**借 market_replay 的
        # as_of 给整块盖章 ——「今天的涨停家数」被标成「11-09 的数据」，比不标更糟。
        # 只有涨停池自己显式给出的数据日才认；没有就保持未知（不给无权威时间的块盖章）。
        pool_day = _parse_day(
            summary.get("trade_date") or summary.get("data_date") or summary.get("as_of")
        )
        if pool_day is not None:
            block["base_date"] = pool_day.isoformat()

        # 炸板/跌停：取自 `market_replay_daily`（东财三池同源同日），缺失就写缺口不写 0
        try:
            from src.core.market_replay import read_latest

            from src.db.session import SessionLocal

            _db = SessionLocal()
            try:
                replay = read_latest(_db)
            finally:
                _db.close()
            if replay:
                bk = replay.get("broken") or {}
                dt = replay.get("limit_down") or {}
                # 盘后复盘的 as_of 是显式数据日，但它只属于「炸板/跌停」这两个读数，
                # 不能升级成整块的基准日（涨停家数来自另一个源，日期未知）。
                replay_day = _parse_day(replay.get("as_of"))
                if replay_day is not None:
                    block["replay_base_date"] = replay_day.isoformat()
                supplement = ""
                if bk.get("break_rate_pct") is not None:
                    block["break_rate_pct"] = bk.get("break_rate_pct")
                    block["seal_rate_pct"] = bk.get("seal_rate_pct")
                    supplement += (
                        f"，炸板{bk.get('total')}家/{bk.get('break_rate_pct')}%"
                        f"(封板率{bk.get('seal_rate_pct')}%)"
                    )
                if dt.get("total") is not None:
                    block["limit_down"] = dt.get("total")
                    supplement += f"，跌停{dt.get('total')}家"
                if supplement:
                    # 显式写清这两个读数自己的数据日；不写就默认它们与涨停家数同日。
                    if replay_day is not None:
                        supplement += f"（炸板/跌停数据日期 {replay_day.isoformat()}）"
                    else:
                        supplement += "（炸板/跌停数据日期未知）"
                    content += supplement
        except Exception as e:  # noqa: BLE001
            logger.debug("[信号摘要] 炸板/跌停补充失败: %s", e)

        block["content"] = content
        return block
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 涨停复盘块失败: %s", e)
        return {"data_status": "missing", "content": "涨停复盘数据缺失"}


async def _block_indices_flow() -> dict:
    """块5 指数+资金流: 4 核心指数 + 两市主力今日净额。"""
    from src.core.report_generator import _collect_indices, _collect_market_flow

    idx_status, flow_status = "missing", "missing"
    idx_parts, flow_part = [], ""
    try:
        indices = await _collect_indices()
        if indices:
            idx_status = "ok"
            for it in indices[:4]:
                chg = it.get("change_pct")
                chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "?"
                idx_parts.append(f"{it.get('name','?')}{chg_s}")
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 指数块失败: %s", e)

    try:
        flow = await _collect_market_flow()
        if isinstance(flow, dict) and not flow.get("error"):
            flow_status = "ok"
            net = flow.get("total_main_flow")
            if net is None:
                net = flow.get("net_inflow")
            if net is None:
                sh = flow.get("sh_flow")
                sz = flow.get("sz_flow")
                if isinstance(sh, (int, float)) and isinstance(sz, (int, float)):
                    net = sh + sz
            if isinstance(net, (int, float)):
                flow_part = f"主力净流入{net:+.0f}亿"
            else:
                flow_part = f"主力净额未知"
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 资金流块失败: %s", e)

    if idx_status == "ok" and flow_status == "ok":
        status = "ok"
    elif idx_status == "ok" or flow_status == "ok":
        status = "partial"
    else:
        status = "missing"

    content = "指数资金：" + "、".join(idx_parts) if idx_parts else "指数资金数据缺失"
    if flow_part:
        content += f"；{flow_part}"

    return {
        "data_status": status,
        "content": content,
        "indices": idx_parts,
        "main_flow": flow_part,
    }


# ──────────────────────────── 聚合 + 渲染 ────────────────────────────

async def build_signal_summary(db) -> dict:
    """聚合 5 块 → 返回 {snapshot_date, blocks, text}。每块独立降级。"""
    blocks = {
        "sentiment": await _block_sentiment(),
        "mainline": await _block_mainline(),
        "market_scan": _block_market_scan(db),
        "limit_up": await _block_limit_up(),
        "indices_flow": await _block_indices_flow(),
    }
    dt = now_cn()
    snap = dt.strftime("%Y-%m-%d")
    text = render_signal_summary(blocks, snapshot_date=snap, now=dt)
    return {
        "snapshot_date": snap,
        "blocks": blocks,
        "text": text,
    }


def _block_tag(fresh: dict, *, has_data: bool = True) -> str:
    """块级时效标签（紧凑，跟着块正文走，模型抄不走样）。"""
    if not has_data:
        # 无数据 ≠ 基准日未知：没有读数就没有「数据日」可核验，不能混进「有数据但没标日期」，
        # 也不能被当成可用的今日证据（2026-11-10 时效审计）。
        return "〔本块数据缺失·无数据日·不计入时效核验〕"
    base = fresh.get("base_date")
    if base is None:
        return "〔基准日未知·时效不可判定·不得当作今日〕"
    if fresh.get("calendar") != "ok":
        return f"〔基准日 {base}·日历不可用无法判定·不得当作今日〕"
    if fresh.get("anomaly"):
        return f"〔基准日异常 {base}·已排除〕"
    if fresh.get("usable_for_today"):
        # 只说「本块与今日一致」：整份摘要能不能用于今日结论由 `_summary_verdict` 统一裁决，
        # 避免「另一块是历史/未来 → 整体不可用」时块级标签还单独喊「可作今日结论」。
        return f"〔基准日 {base}·今日已收盘·本块与今日一致〕"
    if fresh.get("is_today"):
        return f"〔基准日 {base}·今日盘中未收盘·不得作为今日结论〕"
    if fresh.get("is_latest_closed"):
        if fresh.get("session") == "intraday":
            return f"〔基准日 {base}·前日=最近已收盘·非今日〕"
        return f"〔基准日 {base}·最近交易日·今日休市非今日新增〕"
    lag = fresh.get("lag_trading_days")
    return f"〔基准日 {base}·落后 {lag if lag is not None else '?'} 个交易日·仅背景〕"


def _block_has_data(block, key: str) -> bool:
    """块里到底有没有读数（2026-11-10）。

    以块自己声明的 `data_status` 为准（采集侧降级时一定会写 missing/partial + 缺失文案）：
      · 缺块 / 空正文 / data_status=missing → 无数据；
      · partial 且正文就是「数据不足/无法判别」→ 也按无数据处理（没有读数可核验）。
    不去猜正文里的「数据缺失」字样（`_block_indices_flow` 可能写「指数资金数据缺失；主力净流入
    +x亿」—— 那是有一半读数的 partial，不能整块判死）。
    """
    if not isinstance(block, dict):
        return False
    status = str(block.get("data_status") or "").strip().lower()
    content = str(block.get("content") or "").strip()
    if status == "missing" or not content:
        return False
    if status == "partial" and ("数据不足" in content or "无法判别" in content):
        return False
    return True


def block_freshness(blocks: dict, *, now: datetime | None = None) -> dict:
    """**逐块**判时效（2026-11-10 时效审计）。

    为什么逐块：5 块来源不同（情绪周期/主线/三榜/涨停复盘/指数资金），任一块可能是旧快照
    或根本没标日期。旧实现拿「生成日」当整份摘要的基准日，于是 11-05 的三榜 + 11-10 的
    生成日 → 标题写「基准日 11-10，可用于今日结论」。现在每块各自判定，汇总只承认
    **代码核验过的**结论：全部块 base==今天且已收盘，才算 usable_for_today。

    三分类（2026-11-10 补完）：
      · ``missing``  无数据（缺块 / missing / 空正文）—— 没有数据日可核验；
      · ``unknown``  有读数但没标显式基准日；
      · ``stale`` / ``anomaly`` / ``intraday_today`` 同 `resolve_freshness`。
    任一非空都不算「可用于今日结论」。
    """
    dt = now_cn(now)
    rows: list[dict] = []
    for key in _BLOCK_ORDER:
        b = blocks.get(key) if isinstance(blocks, dict) else None
        has_data = _block_has_data(b, key)
        base = b.get("base_date") if isinstance(b, dict) else None
        if not has_data:
            base = None  # 缺块时即便残留 base_date 也不算读数
        fresh = resolve_freshness(base, now=dt)
        rows.append(
            {
                "key": key,
                "data_status": b.get("data_status") if isinstance(b, dict) else None,
                "has_data": has_data,
                "base_date": fresh["base_date"],
                "label": fresh["label"],
                "note": fresh["note"],
                "tag": _block_tag(fresh, has_data=has_data),
                "freshness": fresh,
            }
        )
    live = [r for r in rows if r["has_data"]]
    missing = [r["key"] for r in rows if not r["has_data"]]
    unknown = [r["key"] for r in live if not r["base_date"]]
    stale = [r["key"] for r in live if r["freshness"].get("stale") is True]
    anomaly = [r["key"] for r in live if r["freshness"].get("anomaly")]
    intraday_today = [
        r["key"]
        for r in live
        if r["freshness"].get("is_today") and not r["freshness"].get("usable_for_today")
    ]
    verified = (
        bool(rows)
        and not missing
        # 逐块都必须是「今天 + 已收盘」的代码核验通过；「没有过期」≠「可用于今日」
        # （2026-11-11 反例：11-10 的五块在次日盘中全是「前日=最近已收盘」，旧判法会
        #   把它们算成 verified，于是 11-11 的对话照样写「可用于今日结论」）。
        and all(r["freshness"].get("usable_for_today") is True for r in live)
        and not unknown
        and not anomaly
        and not intraday_today
    )
    bases = sorted({r["base_date"] for r in live if r["base_date"]})
    return {
        "base_date": bases[0] if bases else None,
        "block_bases": bases,
        "blocks": rows,
        "missing": missing,
        "unknown": unknown,
        "stale": stale,
        "anomaly": anomaly,
        "intraday_today": intraday_today,
        "verified_usable_for_today": verified,
    }


def _summary_verdict(bf: dict, generation_day: str | None, *, now: datetime | None = None) -> str:
    """摘要的时效结论 —— 只由 `block_freshness` 的代码核验结果生成。

    「不得仅凭提示词声称硬约束」：这里不读块内文字、不信任何块自述（比如块里写着
    「硬约束：可用于今日结论」），只看 base_date + 交易日历。声称永远是核验的产物。
    """
    dt = now_cn(now)
    gen = generation_day or "未知"
    n = len(bf["blocks"])
    if bf["verified_usable_for_today"]:
        return (
            f"生成日 {gen} 仅为写入日期（不是数据基准日）；代码核验：{n} 块基准日均为 "
            f"{dt.date().isoformat()} 且今日已收盘 → 可用于今日结论"
        )
    parts: list[str] = []
    stale_rows = [r for r in bf["blocks"] if r["freshness"].get("stale") is True]
    if stale_rows:
        items = "；".join(
            f"{r['key']} 基准日 {r['base_date']}"
            f"（落后 {r['freshness'].get('lag_trading_days') if r['freshness'].get('lag_trading_days') is not None else '?'} 个交易日）"
            for r in stale_rows
        )
        parts.append(items)
    if bf["intraday_today"]:
        parts.append(f"{len(bf['intraday_today'])} 块是今日盘中快照（未收盘，会变）")
    if bf["anomaly"]:
        parts.append(f"{len(bf['anomaly'])} 块基准日异常（未来日期/休市冲突，已排除）")
    if bf["unknown"]:
        parts.append(
            f"{len(bf['unknown'])} 块无显式基准日（{'/'.join(bf['unknown'])}）"
        )
    if bf.get("missing"):
        parts.append(
            f"{len(bf['missing'])} 块无数据（{'/'.join(bf['missing'])}），"
            "没有数据日可核验"
        )
    detail = "；".join(parts) or "无可用基准日"
    return (
        f"生成日 {gen} 仅为写入日期，不是数据基准日；代码核验：{detail} → "
        "不得作为今日结论依据，也不得把生成日当基准日；"
        "块内文字/提示词自称的时效不算数，只认本行核验结果"
    )


def render_signal_summary(blocks: dict, *, snapshot_date: str | None = None,
                          now: datetime | None = None) -> str:
    """渲染纯文本摘要(≤800 字), 注入 system message 用。

    2026-11-10 时效审计：旧实现拿 `snapshot_date`（= 生成日）当整份摘要的「基准日」，
    于是老快照 + 今日生成 = 「基准日今日，可用于今日结论」。现在：
      · 标题只写「生成日」，明说生成日不是数据基准日；
      · 每块按自己的 `base_date` 判时效并**贴标签**（未知/盘中/前日/历史 N 日）；
      · 只有全部块都通过代码核验（base==今天且已收盘）才写「可用于今日结论」。
    """
    dt = now_cn(now)
    gen = str(snapshot_date or "").strip() or None
    bf = block_freshness(blocks, now=dt)
    tags = {r["key"]: r["tag"] for r in bf["blocks"]}
    lines = [
        f"--- 系统信号摘要（生成日 {gen or '未知'}）---",
        f"时效：{_summary_verdict(bf, gen, now=dt)}",
    ]
    for key in _BLOCK_ORDER:
        b = blocks.get(key) if isinstance(blocks, dict) else None
        b = b if isinstance(b, dict) else {}
        # 空正文也写显式缺口：不渲染一行空白让模型以为「有块没内容=正常」
        content = str(b.get("content") or "").strip() or f"数据缺失（{key}）"
        tag = tags.get(key) or ""
        lines.append(f"{content}{tag}" if tag else content)
    return "\n".join(lines)


def run_signal_summary_job() -> dict:
    """盘后 cron 入口(report_scheduler 调用): 聚合 + 落库, 失败不抛。

    内部自开 DB session(与 run_market_scan_job 同款), 不与 FastAPI 依赖耦合。
    """
    from src.db.session import SessionLocal
    from src.db.models import SignalSummaryDaily

    db = SessionLocal()
    try:
        result = asyncio.run(build_signal_summary(db))
        snap = result["snapshot_date"]
        row = (
            db.query(SignalSummaryDaily)
            .filter(
                SignalSummaryDaily.snapshot_date == snap,
                SignalSummaryDaily.stock_market == "CN",
            )
            .first()
        )
        if row:
            row.blocks = result["blocks"]
            row.text = result["text"]
        else:
            db.add(
                SignalSummaryDaily(
                    snapshot_date=snap,
                    stock_market="CN",
                    blocks=result["blocks"],
                    text=result["text"],
                )
            )
        db.commit()
        statuses = {k: (v or {}).get("data_status") for k, v in result["blocks"].items()}
        return {"ok": True, "snapshot_date": snap, "text_len": len(result["text"]), "statuses": statuses}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("信号摘要聚合/落库失败: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def _coerce_blocks(value) -> dict:
    """落库的 blocks → dict；JSON 字符串（历史双编码）解析成功才用，解析不了按无块处理。

    2026-11-10 时效终审 P0：旧实现只认 dict，JSON 字符串被静默当成「没有 blocks」，
    于是退回「老文本」路径，从正文裸日期推断数据日并宣称已核验 —— 明明带着逐块
    base_date 的快照被整份当成今日数据。这里解析出来交给逐块核验；解析失败就是没有
    结构化基准日（未知），绝不拿正文日期回填。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            import json

            parsed = json.loads(value)
        except Exception as e:  # noqa: BLE001 - 坏 JSON 不猜
            logger.warning("[信号摘要] blocks JSON 解析失败，按无结构化块处理: %s", e)
            return {}
        if isinstance(parsed, dict):
            return parsed
        logger.warning(
            "[信号摘要] blocks JSON 不是对象（%s），按无结构化块处理", type(parsed).__name__
        )
    return {}


def read_latest_summary_text(db, *, now: datetime | None = None) -> str | None:
    """对话热路径: 读最新信号摘要 text(纯文本), 无快照 → None(不注入)。

    2026-11-10 时效审计：
      · 旧实现把 `snapshot_date`（生成日）当数据基准日 → 11-05 的快照在 11-10 自称
        「基准日今日，可用于今日结论」。现在 `snapshot_date` 只写「生成日」；
      · 有 blocks 的快照**逐块**判时效（块自带 base_date），汇总只认代码核验结果；
      · 落库 text 里**上一次渲染**的标题、时效行、块级标签先剥掉再按当前时间重算 ——
        否则 11-10 收盘写下的「今日已收盘·可用于今日结论」会被 11-11 的对话原样带出；
      · 只有 text 的老快照：只认正文里**显式标注**的数据日期（数据日期/统计日期/数据截至），
        标题里的日期是生成日不是数据日，裸日期/参考日期一律不当基准日；要求**每一条**读数
        都带同一个显式日期才算有基准日，否则未知；且纯文本无法逐块核验 —— 永远不写
        「已核验/可用于今日结论」；
      · blocks 以 JSON 字符串落库（历史双编码）时先解析再逐块核验，解析失败按无结构化块，
        不退回正文日期推断；
      · 未来日期的脏行不参与「最新快照」选择；
      · 历史快照仍注入（作背景），但不冒充今日，也不写「可用于今日结论」。
    """
    try:
        from src.db.models import SignalSummaryDaily

        dt = now_cn(now)
        today = dt.date().isoformat()
        row = (
            db.query(SignalSummaryDaily)
            # 未来日期脏行（时区错位/预写）不许被选成「最新」—— 否则一条 2026-12-01
            # 的真实旧行被挤掉，注入的还是「基准日异常」的一整份摘要。
            .filter(SignalSummaryDaily.snapshot_date <= today)
            .order_by(SignalSummaryDaily.snapshot_date.desc(), SignalSummaryDaily.id.desc())
            .first()
        )
        if not row:
            return None
        gen = str(getattr(row, "snapshot_date", "") or "").strip() or None
        blocks = _coerce_blocks(getattr(row, "blocks", None))
        raw_text = str(getattr(row, "text", "") or "")

        # 先剥旧渲染结论（标题行 / 时效行 / 块级〔基准日…〕标签），再重算。
        body: list[str] = []
        for ln in raw_text.splitlines():
            s = ln.strip()
            if s.startswith("---") and "系统信号摘要" in s:
                continue
            if s.startswith("时效："):
                continue
            clean = _BLOCK_TAG_RE.sub("", ln).rstrip()
            if clean != ln and not clean.strip():
                continue  # 纯标签行，剥完为空 → 不是证据
            body.append(clean)
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()

        if blocks:
            bf = block_freshness(blocks, now=dt)
            tags = {r["key"]: r["tag"] for r in bf["blocks"]}
            header = f"--- 系统信号摘要（生成日 {gen or '未知'}）---"
            if not body:
                # 库里有 blocks 但 text 空/被清过 → 现场按块重建，不把证据丢掉也不返回空壳
                body = [
                    str((blocks.get(k) or {}).get("content") or "").strip()
                    or f"数据缺失（{k}）"
                    for k in _BLOCK_ORDER
                ]
            body = _retag_body(body, blocks, tags)
            return "\n".join(
                [header, f"时效：{_summary_verdict(bf, gen, now=dt)}", *body]
            )

        # 老快照：只有 text。2026-11-10 时效终审 P0 —— **不许**从正文里的参考日期
        # 推断数据日再宣称已核验：旧格式的「（2026-11-05）」只是排版上的参考日期，
        # 不是「数据日期」标签。这里只认显式标注，且要求**每一条**读数都带同一个显式
        # 日期才算有基准日；任何一条没标日期 → 整体基准日未知（纯文本无法逐块核验）。
        items = [ln for ln in body if ln.strip()]
        if not items:
            return None
        info = evidence_item_dates(items, now=dt)
        base = info["base_date"]
        fresh = resolve_freshness(base, now=dt)
        header = (
            f"--- 系统信号摘要（生成日 {gen or '未知'}；"
            f"基准日 {base or '未知'}）---"
        )
        if base is None and info["bases"]:
            seen = "、".join(
                f"{d}（{resolve_freshness(d, now=dt)['label']}）" for d in info["bases"]
            )
            note = (
                f"老快照仅有纯文本：正文显式数据日期 {seen}，但 "
                f"{len(info['undated'])} 条读数没有标注数据日期/日期不一致 → 整体基准日未知，"
                "无法逐条核验；一个分条的日期不得当作其余分条的数据日，"
                "也不得声称已核验或当成今日结论依据"
            )
        elif base is None:
            note = f"{fresh['note']}；该快照仅有纯文本，无逐块基准日，无法核验"
        elif fresh.get("usable_for_today"):
            note = (
                f"正文各条均显式标注数据日期 {base}（今日已收盘），但该快照仅有纯文本、"
                "无逐块结构化基准日 —— 不得声称已核验，也不得当成今日结论依据"
            )
        else:
            note = f"{fresh['note']}；该快照仅有纯文本（各条显式日期一致），结论仅作背景"
        return "\n".join([header, f"时效：{note}", *body])
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 读快照失败: %s", e)
        return None


def _retag_body(body: list[str], blocks: dict, tags: dict) -> list[str]:
    """按块正文给正文行贴**本次**时效标签（不沿用旧标签）。

    正文行就是块 content（一行一块），用 content 做锚匹配；匹配不上（人工改写过）的行
    只保留剥掉旧标签的原文 —— 宁可不贴，也不贴错。
    """
    by_content: dict[str, str] = {}
    for key in _BLOCK_ORDER:
        b = blocks.get(key) if isinstance(blocks, dict) else None
        c = str((b or {}).get("content") or "").strip()
        tag = tags.get(key)
        if c and tag:
            by_content[c] = tag
    out: list[str] = []
    for ln in body:
        tag = by_content.get(ln.strip())
        out.append(f"{ln}{tag}" if tag and tag not in ln else ln)
    return out


# ──────────── 存量库兜底建表 ────────────
# 已收编进 B 层版本化迁移(W1.5/A5 2026-09-08): signal_summary_daily →
# src/web/migrations.py _m141; schema 变更唯一入口是迁移。
