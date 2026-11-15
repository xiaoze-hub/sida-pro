"""题材演化观察层(A 通道): 每日板块成分快照 + 事后描述报告。

## 为什么存在

现有板块成分接口**只有"此刻"**(`sector_filter.board_constituents` 走实时 MCP +
TTL 缓存、`tdx_boards.constituents` 同为当前态), 没有任何 as-of 版本。所以:

1. 每天收盘后存一份板块→成分快照(`theme_board_daily`), 只追加当天, **绝不回填历史**;
2. 报告只用当日快照 + 当日涨停池代码, 算"某题材有多少成员今天在涨停池里"这类
   **描述量**, 不预测、不给方向。

历史日拿不到就是拿不到 —— 拿今天的成分去解释昨天 = 未来信息, 本模块禁止。

## 硬约束(违反即视为 bug)

- `None`(取不到) 与 `[]`(确为空) 必须落成不同 `state`: `fetch_failed` / `empty_ok`。
  报告里两者分开计数, 空列表不得被读成"该题材没有成员"。
- 覆盖不足(`MIN_COVERAGE_FOR_RANKING`)时**不出排名**, 只出覆盖率与缺失清单 ——
  半份数据算出来的"第一名"是噪声。
- 涨停池为空 → 参与度标 unknown, 不写 0(0 只涨停 vs 没取到是两件事)。
- 抓取总预算 `FETCH_BUDGET_S`: 超预算的板块记 `fetch_failed`(reason=budget_exhausted),
  不阻塞调度线程。
- 报告正文禁止出现方向性/交易措辞(`FORBIDDEN_WORDS`), 由 `assert_no_trading_wording` 兜底。
- 不写任何用户数据: 两张表都是公开市场数据, 不带 `user_id`。

## 时点

`register_cron` 工作日 15:16 —— 15:10 情绪周期 sync 之后、15:30 盘后链路之前。
调度器不可用时返回 False 不崩(与 `market_phase.register_cron` 同构)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Iterable

from sqlalchemy import select

from src.core.timezone import beijing_now
from src.db.models import ThemeBoardDaily, ThemeLimitUpDaily

logger = logging.getLogger(__name__)

JOB_ID = "theme-evolution-daily"
REPORT_TITLE = "题材演化观察"
RULE_VERSION = "theme_evo_v1"
SOURCE_TQ = "tq"

STATE_OK = "ok"
STATE_EMPTY = "empty_ok"
STATE_FAILED = "fetch_failed"
_STATES = (STATE_OK, STATE_EMPTY, STATE_FAILED)

#: 覆盖率达到这个比例才出参与度排名; 低于它只报覆盖与缺失。
MIN_COVERAGE_FOR_RANKING = 0.5

#: 单次采集的总时间预算(秒)。超预算的板块记 fetch_failed, 不拖死调度线程。
FETCH_BUDGET_S = 300.0

#: 报告正文禁止出现的交易措辞(不含"投资建议"这类免责表述)。
FORBIDDEN_WORDS = ("建议买入", "可买入", "建议卖出", "建议加仓", "建议减仓", "目标价", "必买")

#: 通达信板块目录里混着一批**按规则生成的"条件/技术"板块**(昨日涨停/最近多板/连续亏损/
#: 高市净率/已高送转…)。它们的成分本身由财务或行情条件筛出来, 拿"成员今天涨停几只"去排名
#: 会形成循环(例如「昨日涨停」的成分天然容易在今天继续涨停)。这里只做**排除标记**,
#: 不删数据: 被排除的板块在报告里逐个列出, 名字由人在阅读时复核。
#:
#: 这是"源端分类"而不是"题材判断" —— 命中即标 `condition_screen`, 未命中一律按普通板块处理,
#: 宁可漏标也不把真实题材静默丢掉。
CONDITION_BOARD_PATTERN = re.compile(
    r"(昨日|昨曾|昨ST|最近|连板|首板|涨停|跌停|异动|多板|"
    r"情绪指数|安全分|换手|振幅|涨幅|跌幅|新高|新低|创新|"
    r"亏损|盈利|负债|市盈|市净|ROE|股息|派息|送转|解禁|增发|配股|"
    r"破净|超跌|强势|弱势|活跃|低价|高价|小盘|大盘|中盘|摘帽|戴帽|风险提示|退市)"
)

ENV_ENABLED = "SIDA_THEME_EVOLUTION_ENABLED"


# ---------------------------------------------------------------- 默认取数
def _default_boards() -> list[dict]:
    from src.core import tdx_boards

    return list(tdx_boards.sector_items() or [])


def _default_members(block_code: str):
    from src.core import tdx_boards

    return tdx_boards.constituents(block_code)


def _default_fresh() -> bool:
    from src.core import tdx_boards

    return bool(tdx_boards.data_fresh())


def _default_pool(day: str) -> list[dict]:
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    return list(MarketSentimentCollector().get_limit_up_pool(day) or [])


def _default_quotes(codes: list[str]) -> dict | None:
    """板块行情(一次批量)。失败/空 → None = 板块类型未知, 不猜成"没有行情"。"""
    from src.core import tdx_boards

    try:
        out = tdx_boards.board_quotes(codes, with_fund=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[theme-evo] 板块行情批量失败: %r", exc)
        return None
    return out or None


def _default_trade_date() -> str | None:
    """通达信日线最新交易日(YYYYMMDD)。取不到 → None(未知, 不猜)。"""
    from src.core import tdx_boards

    try:
        raw = tdx_boards.latest_trade_date()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[theme-evo] 交易日探测失败: %r", exc)
        return None
    return raw.replace("-", "") if raw else None


# ---------------------------------------------------------------- 工具
def _norm_code(value: Any) -> str:
    """成分股代码归一: 去空白/大写/去交易所后缀。非 A 股形态原样保留(不猜市场)。"""
    s = str(value or "").strip().upper()
    if not s:
        return ""
    for suffix in (".SH", ".SZ", ".BJ"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _dump_members(codes: Iterable[str]) -> str:
    return json.dumps(sorted(set(codes)), ensure_ascii=False)


def _load_members(row: ThemeBoardDaily) -> list[str]:
    if not row.members_json:
        return []
    try:
        data = json.loads(row.members_json)
    except (TypeError, ValueError):
        logger.warning("[theme-evo] members_json 解析失败 day=%s board=%s", row.day, row.board_code)
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def _upsert(db, model, keys: dict, values: dict) -> None:
    obj = db.execute(select(model).filter_by(**keys)).scalar_one_or_none()
    if obj is None:
        db.add(model(**keys, **values))
        return
    for k, v in values.items():
        setattr(obj, k, v)


# ---------------------------------------------------------------- 采集
def collect_snapshot(
    db,
    *,
    day: str | None = None,
    boards: list[dict] | None = None,
    fetch_members: Callable[[str], list[str] | None] | None = None,
    fetch_pool: Callable[[str], list[dict]] | None = None,
    fetch_quotes: Callable[[list[str]], dict | None] | None = None,
    source_fresh: bool | None = None,
    source_trade_date: str | None = None,
    fetch_trade_date: Callable[[], str | None] | None = None,
    now: datetime | None = None,
    max_boards: int | None = None,
    budget_s: float = FETCH_BUDGET_S,
) -> dict:
    """抓取并落当日板块成分快照 + 涨停池代码。返回摘要(不抛业务异常)。

    返回 `status`:
      - `ok`          全部板块都拿到了结果(ok/empty_ok 均算拿到)
      - `partial`     有 fetch_failed
      - `refused`     板块目录为空 / 源不新鲜 / 无可用板块 → **不写任何行**
    """
    if db is None:
        raise ValueError("collect_snapshot 需要显式传入 db session(core 层不自开会话)")

    now = now or beijing_now()
    day = day or now.strftime("%Y%m%d")
    boards = _default_boards() if boards is None else boards
    fetch_members = fetch_members or _default_members
    fetch_pool = fetch_pool or _default_pool
    fetch_quotes = fetch_quotes or _default_quotes
    fresh = _default_fresh() if source_fresh is None else bool(source_fresh)
    if source_trade_date is None:
        source_trade_date = (fetch_trade_date or _default_trade_date)()
    source_trade_date = (source_trade_date or "").replace("-", "") or None

    summary: dict[str, Any] = {
        "day": day, "rule_version": RULE_VERSION,
        "fetched_at": now.isoformat(timespec="seconds"),
        "source": SOURCE_TQ, "source_fresh": fresh, "source_trade_date": source_trade_date,
        "boards_total": len(boards or []), "ok": 0, "empty_ok": 0, "fetch_failed": 0,
        "stored_boards": 0, "limit_up_count": 0, "limit_up_stored": False,
        "status": "ok", "reason": None, "errors": [],
    }

    if not boards:
        summary["status"] = "refused"
        summary["reason"] = "board_catalog_empty"
        summary["errors"].append("板块目录为空 → 不落空快照(避免把取数失败写成'没有板块')")
        return summary
    if not fresh:
        summary["status"] = "refused"
        summary["reason"] = "source_stale"
        summary["errors"].append("通达信源不新鲜(客户端未更新) → 不落当日成分快照")
        return summary
    # 交易日对齐: 客户端最新日线还停在更早的日期 → 今天的成分/涨停池其实是那天的,
    # 用它冒充 day 就是把旧数据写进新一天(数据日错配)。宁可拒绝并留日志。
    if source_trade_date and source_trade_date < day:
        summary["status"] = "refused"
        summary["reason"] = "source_not_updated_for_day"
        summary["errors"].append(
            f"客户端最新交易日 {source_trade_date} 早于目标日 {day} → 拒绝落库(数据日错配)"
        )
        return summary

    started = time.monotonic()
    selected = boards if max_boards is None else boards[: int(max_boards)]
    pool_targets = [(b.get("code") or "").strip() for b in selected]

    # 板块行情: 用来区分"真题材/行业板块"(有行情)与通达信"选股条件板块"(有成分但无行情)。
    # 批量失败 → 全部 None(类型未知), 报告里显式说明, 不猜。
    quotes = fetch_quotes(pool_targets) if fetch_quotes else None

    for idx, board in enumerate(selected):
        code = (board.get("code") or "").strip()
        if not code:
            continue
        q = (quotes or {}).get(code) if quotes is not None else None
        has_quote = None if q is None else bool(q.get("price") is not None)
        if time.monotonic() - started > budget_s:
            _upsert(
                db, ThemeBoardDaily, {"day": day, "board_code": code},
                {"board_name": board.get("name"), "board_type": board.get("board_type"),
                 "source": SOURCE_TQ, "state": STATE_FAILED, "member_count": None,
                 "members_json": None, "has_quote": has_quote,
                 "fetched_at": now, "error": "budget_exhausted"},
            )
            summary[STATE_FAILED] += 1
            summary["errors"].append(f"budget_exhausted@{idx}/{len(selected)}")
            continue

        error = None
        members: list[str] | None
        try:
            raw = fetch_members(code)
        except Exception as exc:  # noqa: BLE001 —— 单个板块失败不能打断整轮
            raw, error = None, f"{type(exc).__name__}: {exc}"[:240]
        members = None if raw is None else [_norm_code(c) for c in raw if _norm_code(c)]
        members = None if members is None else sorted(set(members))

        if members is None:
            state, count, payload, error = STATE_FAILED, None, None, error or "constituents_unavailable"
        elif not members:
            # 取到了但确为空: 单独一态, 报告里会标可疑, 不能当"没成员"直接吞掉
            state, count, payload = STATE_EMPTY, 0, _dump_members([])
        else:
            state, count, payload = STATE_OK, len(members), _dump_members(members)

        _upsert(
            db, ThemeBoardDaily, {"day": day, "board_code": code},
            {"board_name": board.get("name"), "board_type": board.get("board_type"),
             "source": SOURCE_TQ, "state": state, "member_count": count,
             "members_json": payload, "has_quote": has_quote,
             "fetched_at": now, "error": error},
        )
        summary[state] += 1
        summary["stored_boards"] += 1

    # 涨停池: 只存返回了什么; 空池不写行(报告据此标 unknown, 不是"0 只涨停")
    try:
        pool = fetch_pool(day) or []
    except Exception as exc:  # noqa: BLE001
        pool = []
        summary["errors"].append(f"limit_up_pool_failed: {type(exc).__name__}: {exc}"[:240])
    codes = {_norm_code(item.get("code")) for item in pool if isinstance(item, dict)}
    codes.discard("")
    if codes:
        db.query(ThemeLimitUpDaily).filter(ThemeLimitUpDaily.day == day).delete()
        for item in pool:
            code = _norm_code(item.get("code"))
            if not code:
                continue
            db.add(ThemeLimitUpDaily(day=day, stock_code=code,
                                     stock_name=str(item.get("name") or "")[:64] or None,
                                     source=str(item.get("source") or "limit_up_pool")[:32],
                                     fetched_at=now))
        summary["limit_up_count"] = len(codes)
        summary["limit_up_stored"] = True
    else:
        summary["errors"].append("limit_up_pool_empty_or_unavailable")

    db.commit()

    if summary[STATE_FAILED] and (summary[STATE_OK] or summary[STATE_EMPTY]):
        summary["status"] = "partial"
    elif summary[STATE_FAILED] and not (summary[STATE_OK] or summary[STATE_EMPTY]):
        summary["status"] = "partial"
        summary["reason"] = "all_boards_failed"
    logger.info("[theme-evo] 快照完成 day=%s %s", day, {
        k: summary[k] for k in ("ok", "empty_ok", "fetch_failed", "limit_up_count", "status")
    })
    return summary


# ---------------------------------------------------------------- 报告
def build_payload(db, *, day: str | None = None, top_n: int = 30,
                  collect: dict | None = None) -> dict:
    """从库里已存的当日快照构造报告载荷(不联网, 可复算)。"""
    if db is None:
        raise ValueError("build_payload 需要显式传入 db session")
    day = day or beijing_now().strftime("%Y%m%d")

    rows = list(db.execute(select(ThemeBoardDaily).filter_by(day=day)).scalars())
    pool_rows = list(db.execute(select(ThemeLimitUpDaily).filter_by(day=day)).scalars())
    pool_codes = {r.stock_code for r in pool_rows}

    prev_day = db.execute(
        select(ThemeBoardDaily.day)
        .where(ThemeBoardDaily.day < day)
        .order_by(ThemeBoardDaily.day.desc())
        .limit(1)
    ).scalar_one_or_none()
    prev_rows = (
        list(db.execute(select(ThemeBoardDaily).filter_by(day=prev_day)).scalars())
        if prev_day else []
    )
    prev_by_code = {r.board_code: r for r in prev_rows}

    counts = {STATE_OK: 0, STATE_EMPTY: 0, STATE_FAILED: 0}
    for r in rows:
        counts[r.state if r.state in _STATES else STATE_FAILED] += 1
    total = len(rows)
    coverage = (counts[STATE_OK] / total) if total else 0.0

    # 板块类型: 有行情 = 真题材/行业板块; 无行情 = 通达信"选股条件"板块(业绩预升/已高送转等),
    # 有成分但不是一个可观察的题材行情序列, 默认不参与排名。
    def _kind(r) -> str:
        name = r.board_name or ""
        if r.has_quote is False or CONDITION_BOARD_PATTERN.search(name):
            return "condition_screen"
        if r.has_quote is True:
            return "theme_like"
        return "unknown"

    boards: list[dict] = []
    for r in rows:
        members = _load_members(r) if r.state == STATE_OK else []
        hit = sorted(set(members) & pool_codes) if members and pool_codes else []
        prev = prev_by_code.get(r.board_code)
        added = removed = None
        if prev is not None and prev.state == STATE_OK and r.state == STATE_OK:
            prev_members = set(_load_members(prev))
            cur = set(members)
            added = sorted(cur - prev_members)
            removed = sorted(prev_members - cur)
        boards.append({
            "board_code": r.board_code, "board_name": r.board_name,
            "board_type": r.board_type, "board_kind": _kind(r),
            "state": r.state,
            "member_count": r.member_count, "error": r.error,
            "limit_up_hits": len(hit), "limit_up_codes": hit[:20],
            "hit_ratio": round(len(hit) / len(members), 4) if members else None,
            "added_vs_prev": added, "removed_vs_prev": removed,
        })

    ranking_ok = bool(pool_codes) and total > 0 and coverage >= MIN_COVERAGE_FOR_RANKING
    ranking_reason = None
    if not pool_codes:
        ranking_reason = "limit_up_pool_unavailable → 参与度 unknown, 不出排名"
    elif total == 0:
        ranking_reason = "no_snapshot_for_day → 不出排名"
    elif coverage < MIN_COVERAGE_FOR_RANKING:
        ranking_reason = f"coverage {coverage:.1%} < {MIN_COVERAGE_FOR_RANKING:.0%} → 不出排名"

    ranked = sorted(
        (b for b in boards
         if b["state"] == STATE_OK and b["board_kind"] != "condition_screen"),
        key=lambda b: (-b["limit_up_hits"], -(b["hit_ratio"] or 0.0), b["board_name"] or ""),
    ) if ranking_ok else []

    screen_rules = sorted(
        (b for b in boards if b["state"] == STATE_OK and b["board_kind"] == "condition_screen"),
        key=lambda b: (-b["limit_up_hits"], b["board_name"] or ""),
    )

    # 占比榜: 小成分板块 1-2 只涨停就能刷出高占比, 所以要求最小成分数与最小命中数,
    # 阈值是**描述口径**不是信号门槛; 达不到就只是不进这一张表, 原始榜里仍然有它。
    MIN_MEMBERS_FOR_RATIO = 15
    MIN_HITS_FOR_RATIO = 3
    by_ratio = sorted(
        (b for b in boards
         if b["state"] == STATE_OK and b["board_kind"] != "condition_screen"
         and (b["member_count"] or 0) >= MIN_MEMBERS_FOR_RATIO
         and b["limit_up_hits"] >= MIN_HITS_FOR_RATIO),
        key=lambda b: (-(b["hit_ratio"] or 0.0), -b["limit_up_hits"], b["board_name"] or ""),
    ) if ranking_ok else []

    changed = [b for b in boards if b["added_vs_prev"] or b["removed_vs_prev"]]
    changed.sort(key=lambda b: (-(len(b["added_vs_prev"] or []) + len(b["removed_vs_prev"] or [])),
                                b["board_name"] or ""))

    return {
        "day": day,
        "rule_version": RULE_VERSION,
        "generated_at": beijing_now().isoformat(timespec="seconds"),
        "prev_day": prev_day,
        "collect": (
            {k: collect.get(k) for k in ("status", "reason", "source_fresh",
                                         "source_trade_date", "fetched_at", "errors")}
            if collect else None
        ),
        "counts": counts, "boards_total": total, "coverage": round(coverage, 4),
        "limit_up": {
            "available": bool(pool_codes), "count": len(pool_codes),
            "sample": sorted(pool_codes)[:20],
        },
        "ranking_available": ranking_ok, "ranking_reason": ranking_reason,
        "ranking": ranked[:top_n],
        "ranking_by_ratio": by_ratio[:top_n],
        "ratio_filters": {"min_members": MIN_MEMBERS_FOR_RATIO, "min_hits": MIN_HITS_FOR_RATIO},
        "kind_counts": {
            "theme_like": sum(1 for b in boards if b["board_kind"] == "theme_like"),
            "condition_screen": len(screen_rules),
            "unknown": sum(1 for b in boards if b["board_kind"] == "unknown"),
        },
        "screen_rules": screen_rules[:top_n],
        "boards": boards,
        "changes": changed[:top_n],
        "changes_available": bool(prev_day),
        "missing": [
            {"board_code": b["board_code"], "board_name": b["board_name"],
             "state": b["state"], "error": b["error"]}
            for b in boards if b["state"] != STATE_OK
        ],
    }


def render_markdown(payload: dict) -> str:
    """把载荷渲染成报告正文。只描述事实与缺口, 不给方向。"""
    c = payload["counts"]
    lines = [
        f"# {REPORT_TITLE} · {payload['day']}",
        "",
        f"- 生成时刻: {payload['generated_at']} (收盘后采集, 非盘中快照)",
        f"- 规则版本: `{payload['rule_version']}`; 数据来源: 通达信板块目录/成分 + 当日涨停池",
        f"- 板块覆盖: 应解析 {payload['boards_total']} 个 → ok {c[STATE_OK]} / "
        f"empty_ok {c[STATE_EMPTY]} / fetch_failed {c[STATE_FAILED]}, "
        f"齐全率 **{payload['coverage']:.1%}**",
        f"- 当日涨停池: " + (f"{payload['limit_up']['count']} 只(已落库, 可复算)"
                            if payload["limit_up"]["available"]
                            else "**未取到 → 参与度 unknown, 不等于 0 只涨停**"),
        "",
    ]
    col = payload.get("collect") or {}
    if col.get("status") == "refused":
        lines += [f"> **本次采集被拒绝**: `{col.get('reason')}` —— "
                  + "；".join(str(e) for e in (col.get("errors") or [])),
                  "> 没有写入任何快照, 本报告不含当日新数据。", ""]
    elif col.get("source_trade_date"):
        lines += [f"- 源端最新交易日: `{col['source_trade_date']}`", ""]
    lines += [
        "## 口径与边界",
        "",
        "- 成分快照 = **采集时点**的板块成分, 只用于当日与前一快照的对比; 不回填历史, "
        "也不代表指数官方成分或基金持仓。",
        "- 「涨停池内成员数」是**描述量**: 该题材当日有多少成员出现在涨停池里。"
        "它不是题材强度、不是资金流、不是交易信号。",
        "- 概念板块允许一股多属, 各板块分别统计, **不可相加**。",
        "- 覆盖不足时不出排名; `empty_ok` 视为可疑(成功返回但为空), 单列在缺口里。",
        "",
        "## 题材参与度(涨停池内成员数)",
        "",
        "> 通达信板块目录混合了题材、行业、地区与**财务/技术条件类**板块(如「连续亏损」"
        "「高市净率」「昨日涨停」)。下表已排除按名称/无行情识别的条件类板块(清单见本节末尾), "
        "其余按原始目录统计、不做题材语义过滤 —— 条件类板块的数值不代表题材热度。",
        "",
    ]
    if not payload["ranking_available"]:
        lines += [f"**不出排名**: {payload['ranking_reason']}", ""]
    elif not payload["ranking"]:
        lines += ["当日无任何板块命中涨停池成员。", ""]
    else:
        lines += ["| # | 板块 | 类型 | 成分数 | 涨停池内成员 | 占成分比 |",
                  "|---|---|---|---|---|---|"]
        for i, b in enumerate(payload["ranking"], 1):
            ratio = "—" if b["hit_ratio"] is None else f"{b['hit_ratio']:.1%}"
            lines.append(
                f"| {i} | {b['board_name'] or b['board_code']} | {b['board_type'] or '—'} | "
                f"{b['member_count']} | {b['limit_up_hits']} | {ratio} |"
            )
        lines.append("")

    rf = payload.get("ratio_filters") or {}
    lines += ["### 高参与度(占成分比)", ""]
    if not payload["ranking_available"]:
        lines += ["**不出排名**(同上)。", ""]
    elif not payload["ranking_by_ratio"]:
        lines += [f"无板块同时满足成分数≥{rf.get('min_members')}且涨停池内成员≥"
                  f"{rf.get('min_hits')}。", ""]
    else:
        lines += [f"只列成分数≥{rf.get('min_members')}、涨停池内成员≥{rf.get('min_hits')} 的板块"
                  "(小成分板块 1-2 只涨停就能刷出高占比, 故加最小样本)。", "",
                  "| # | 板块 | 类型 | 成分数 | 涨停池内成员 | 占成分比 |",
                  "|---|---|---|---|---|---|"]
        for i, b in enumerate(payload["ranking_by_ratio"], 1):
            lines.append(
                f"| {i} | {b['board_name'] or b['board_code']} | {b['board_type'] or '—'} | "
                f"{b['member_count']} | {b['limit_up_hits']} | {b['hit_ratio']:.1%} |"
            )
        lines.append("")

    kc = payload.get("kind_counts") or {}
    if kc.get("condition_screen"):
        lines += ["### 未参与排名的「条件/技术」板块", "",
                  f"共 {kc['condition_screen']} 个板块按名称或「无行情」被判为条件/技术类"
                  "(如昨ST连板/已高送转/连续亏损/最近情绪指数), 其成分由财务或行情规则筛出, "
                  "用涨停成员数排名会形成循环, 故不参与排名。**它们的数据已落库, 只是不做排名**; "
                  "名称匹配是源端分类, 判断由人复核:",
                  ""]
        names = "、".join(b["board_name"] or b["board_code"] for b in payload["screen_rules"][:12])
        lines += [names + ("…" if kc["condition_screen"] > 12 else ""), ""]
    if kc.get("unknown"):
        lines += [f"另有 {kc['unknown']} 个板块的行情状态未知(批量取数失败), "
                  "保留在排名里但类型未经证实。", ""]

    lines += ["## 成分变动(与上一份快照比)", ""]
    if not payload["changes_available"]:
        lines += ["**unknown**: 库里没有更早的快照 —— 首次采集无法判断新增/剔除, 不猜。", ""]
    elif not payload["changes"]:
        lines += ["前后两份快照的成分集合无差异(仅限两日都成功的板块)。", ""]
    else:
        lines += ["| 板块 | 新增 | 剔除 |", "|---|---|---|"]
        for b in payload["changes"][:20]:
            add = "、".join(b["added_vs_prev"] or []) or "—"
            rem = "、".join(b["removed_vs_prev"] or []) or "—"
            lines.append(f"| {b['board_name'] or b['board_code']} | {add} | {rem} |")
        lines.append("")

    lines += ["## 缺口清单", ""]
    if not payload["missing"]:
        lines += ["无(全部板块均返回了结果)。", ""]
    else:
        lines += [f"共 {len(payload['missing'])} 个板块未取到/none 成分, 前 20 个:", ""]
        for m in payload["missing"][:20]:
            lines.append(f"- `{m['board_code']}` {m['board_name'] or ''} "
                         f"state={m['state']} error={m['error'] or '—'}")
        if len(payload["missing"]) > 20:
            lines.append(f"- …共 {len(payload['missing'])} 个, 详见落库记录")
        lines.append("")

    lines += [
        "## 声明",
        "",
        "- 本报告是公开市场数据的**描述性统计**, 不是预测, 不构成投资建议, 不含买卖方向。",
        "- 数据缺失一律标 unknown 并在上方列出; 未列出的一律视为取数失败, 不作零值处理。",
        "",
    ]
    return "\n".join(lines)


def assert_no_trading_wording(text: str) -> None:
    """正文禁止出现交易措辞(写完/渲染后各北京时间调用一次)。"""
    hits = [w for w in FORBIDDEN_WORDS if w in (text or "")]
    if hits:
        raise ValueError(f"报告正文出现禁止措辞: {hits}")


# ---------------------------------------------------------------- 日常入口
def run_daily(db=None, *, day: str | None = None, **collect_kwargs) -> dict:
    """采集 + 出报告。返回 {collect, payload, report}。

    报告走 `report_generator.write_report_document`(与盘前/盘后同一条落盘通道),
    `scope=shared` —— 只含公开市场数据, 不含任何用户持仓/自选。
    """
    own = db is None
    if own:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        collect = collect_snapshot(db, day=day, **collect_kwargs)
        payload = build_payload(db, day=collect["day"], collect=collect)
        text = render_markdown(payload)
        assert_no_trading_wording(text)
        from src.core.report_generator import write_report_document

        report = write_report_document(
            JOB_ID, f"{REPORT_TITLE} · {payload['day']}", text,
            scope="shared", contains_private_holdings=False,
        )
        return {"collect": collect, "payload": payload, "report": report}
    finally:
        if own:
            db.close()


def register_cron(scheduler) -> bool:
    """工作日 15:16 采集 + 出报告。开关 `SIDA_THEME_EVOLUTION_ENABLED`(默认开)。"""
    if scheduler is None or not hasattr(scheduler, "add_job"):
        return False
    if str(os.getenv(ENV_ENABLED, "1")).strip().lower() in ("0", "false", "no", "off"):
        logger.info("[theme-evo] 已通过 %s 关闭, 不注册定时任务", ENV_ENABLED)
        return False

    def _run_once():
        try:
            out = run_daily()
            logger.info("[theme-evo] 日报完成: %s", {
                "day": out["payload"]["day"],
                "coverage": out["payload"]["coverage"],
                "report": out.get("report", {}).get("path"),
            })
        except Exception as exc:  # noqa: BLE001 —— 调度线程不能被单次失败打死
            logger.error("[theme-evo] 日报异常: %r", exc)

    try:
        scheduler.add_job(
            _run_once, "cron", day_of_week="mon-fri", hour=15, minute=16,
            id="theme_evolution_daily", replace_existing=True,
            coalesce=True, max_instances=1,
        )
        logger.info("[theme-evo] 日报 cron 已注册: 工作日 15:16")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[theme-evo] cron 注册失败: %r", exc)
        return False
