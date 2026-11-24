"""机构披露持仓确定性直查（2026-11-21，参谋首批 C）。

## 为什么单独一条路

「汇金这季度新进了什么」「社保 2026-09-30 持仓有多少」这类问题**答案全在库里**，
但走模型工具循环要 30~70 秒，还可能把「榜单新进」说成「实际买入」、把
`len(items)` 当成机构全集（页面默认只取 200 条）。这条路把问句解析成确定参数，
直接调**页面同一套 core 查询**（`src/core/national_team.py`），一次性排版返回：

- 不新增 SQL / 表 / 采集链路，也不绕 HTTP 鉴权去调 `/api/national-team/*`；
- 权限在服务端重新核 `view_quotes`（与路由级 `require_perm` 同一口径），
  用户信息缺失或没权限一律拒绝作答，绝不「先答了再说」；
- 机构、报告期、榜单口径（float/total）、变动类型都在**取数前**确定，
  先按机构/变动过滤拿准确总量，再截断展示；
- 任何不确定（没写清单口径、只写了年份、同时要两种榜单）→ 明确追问，不猜。

## 边界（硬性）

1. **会话绑定不是机构**：`bound` 里只有个股代码，本模块从不拿它当机构；
2. **不接管个股分析**：出现「能买 / 走势 / 风险 / 为什么」等判断词，或正文点了
   6 位代码以外的具体个股且要求分析 → 返回 `None`，交回主流程（那边要证据链）；
3. **不接管混合问题**：提到北向/公募/私募/游资/龙虎榜等本模块没有的机构，
   或「汇金 + 某只票能不能买」→ `None`；
4. **不含宽基 ETF**：这是已知覆盖缺口，遇到 ETF 问句如实说明缺口，不编数字。
5. **跨期身份只认「同名」**：`holder_key` 由「组 + 股东全称哈希」得出，两期之间只有
   全称完全一致才算同一账户 —— 不做名称相似度合并（不同险资产品账户就是不同账户），
   也不做跨期/全库的账户清理。改名的主体在新报告期会以新 key 出现（宁多账、不误并）。
6. **SQL 只在 worker 线程跑、只拿 Engine**：`answer` 是 async，但 core 查询是同步
   阻塞的；直接调用会卡住事件循环。这里统一 `asyncio.to_thread` 并把**线程安全的
   Engine**（连接池）传进去 —— 绝不把请求的 `Session` 带进别的线程（串事务/串连接）。
   取数异常对外只给统一非敏感提示，底层异常（SQL/连接串/表名）只进服务端日志。

## 「没有数据」的四种情形必须分开说

`empty_table`（还没采集）/ `unknown_period`（没有这个报告期）/ `insufficient_periods`
（只有一期算不出环比）/ `zero_change`（有数据、本期确实零变动）/
`empty_result`（该机构本期不在榜）/ 取数异常（抛错误，不降级成零变动）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.core import national_team as nt

logger = logging.getLogger(__name__)

#: 与路由级 `require_perm(PERM_VIEW_QUOTE)` 同一权限点
_QUOTE_PERM = "view_quotes"

#: 取数失败的统一对外文案：不透露底层异常/SQL/连接信息（那些只进日志）。
_FETCH_ERROR_TEXT = (
    "取数失败：数据库查询未完成（这是取数错误，不代表该机构没有持仓）。请稍后重试，"
    "或打开同口径页面核对。"
)

#: 持仓意图词。没有这些词就只是在提机构名，不抢答（改追问）。
_HOLDING_WORDS: tuple[str, ...] = (
    "持仓", "持有", "持股", "十大股东", "十大流通股东", "重仓", "买了", "拿了",
    "新进", "退出", "加仓", "减仓", "增持", "减持", "增仓", "变动", "环比",
    "上榜", "在榜", "仓位", "持股市值", "拿了多少", "买了多少",
)

#: 判断/分析意图词 —— 出现即不接管（这类问题要证据链，直查给不了）。
#: 注意不含 `持有`/`加仓`/`减仓`（它们是持仓问句的正当词）。
_ANALYSIS_WORDS: tuple[str, ...] = (
    "怎么样", "怎么看", "如何看", "分析", "诊断", "研判", "点评", "看法", "评价",
    "能买", "能拿", "该不该", "要不要", "值得", "风险", "机会", "为什么", "原因",
    "走势", "趋势", "支撑", "压力", "目标价", "洗盘", "出货", "涨停", "跌停",
    "后面", "后续", "对比", "哪个好", "建议", "推荐", "能不能买", "还能持有",
    "可以买", "要不要卖", "买入吗", "卖出吗",
)

#: 本模块不覆盖的其他资金/榜单（出现即混合问题，交回主流程）。
_OTHER_ENTITIES: tuple[str, ...] = (
    "北向", "陆股通", "外资", "qfii", "公募", "私募", "游资", "龙虎榜",
    "主力资金", "基金公司", "资管计划", "大基金", "产业资本",
)

#: 比较问法：多家机构「谁多/哪个重」要用分机构口径回答，本模块只做单/多机构合计，
#: 硬答会把「谁多」变成「合计多少」—— 交回主流程，不猜。
_COMPARE_WORDS: tuple[str, ...] = ("谁", "哪个", "哪家", "相比", "比较", "对比")

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_DATE_RE = re.compile(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})\s*年?(?![\d\-/月])")

_HELP_TEXT = (
    "【机构持仓直查】这条问句还缺关键信息，先确认一下再查：\n"
    "· 看哪家：中央汇金（投资 / 汇金资管，或两者合计）、证金公司、社保基金、险资、外汇局三平台、\n"
    "  或者「国家队」= 以上合计；\n"
    "· 看什么：持仓明细（谁在十大股东里、持股多少），还是环比变动（新进 / 加仓 / 减仓 / 退出）；\n"
    "· 报告期：如 2026-09-30（缺省最新已采集报告期）；\n"
    "· 榜单口径：十大流通股东（float，缺省）或十大股东（total）。\n"
    "例如：「汇金 2026-09-30 十大流通股东持仓」「社保最新一期新进了哪些股」。"
)


@dataclass
class Question:
    """问句解析结果。`ambiguous` 非空时只追问，不查库。"""

    scopes: list[dict[str, str]] = field(default_factory=list)
    kind: str = "float"
    report_date: str | None = None
    #: 同比比较期（去年同季）；只在 changes + 「去年同期」时由 `_fetch` 填，
    #: 深链必须带上它，否则页面按相邻上一季重算成环比。
    prev_report_date: str | None = None
    period_hint: str = "latest"          # latest / prev / explicit / yoy
    change_kinds: tuple[str, ...] = ()
    stock_code: str | None = None
    mode: str = "holdings"               # holdings / changes
    ambiguous: str | None = None
    etf_gap: bool = False

    @property
    def holder_csv(self) -> str | None:
        vals = [s["value"] for s in self.scopes]
        return ",".join(vals) if vals else None

    @property
    def label(self) -> str:
        labels = [s["label"] for s in self.scopes]
        # 同组多账户（汇金投资 + 汇金资管）合并成「汇金系（投资+资管）」
        dedup: list[str] = []
        for lab in labels:
            if lab not in dedup:
                dedup.append(lab)
        return " + ".join(dedup) if dedup else "机构"


def _strip_institutions(text: str) -> str:
    """把已识别的机构名从正文里抹掉，避免「社保基金」里的「基金」被当成公募。"""
    work = text or ""
    for alias, _ in sorted(nt.HOLDER_ALIASES, key=lambda x: len(x[0]), reverse=True):
        work = work.replace(alias, "　")
    for alias, _ in sorted(nt.GROUP_ALIASES, key=lambda x: len(x[0]), reverse=True):
        work = work.replace(alias, "　")
    for w in nt.NATIONAL_TEAM_WORDS:
        work = work.replace(w, "　")
    return work


def parse_question(text: str) -> Question:
    """把问句解析成确定参数；不确定就写进 `ambiguous`。纯本地，不查库、不看绑定。"""
    raw = (text or "").strip()
    q = Question()
    if not raw:
        q.ambiguous = _HELP_TEXT
        return q

    q.scopes = nt.resolve_institution_scopes(raw)
    if not q.scopes:
        # 没点机构 → 不是这条路的问题（由调用方返回 None）
        q.ambiguous = None
        return q

    rest = _strip_institutions(raw)

    # 混合问题：本模块没有的其他资金/榜单
    if any(w in rest.lower() for w in _OTHER_ENTITIES):
        q.ambiguous = None
        q.mode = "not_ours"
        return q

    # 多机构比较（「汇金和社保谁持仓多」）：答案需要分机构口径，不在这里硬答
    if len(q.scopes) > 1 and any(w in raw for w in _COMPARE_WORDS):
        q.ambiguous = None
        q.mode = "not_ours"
        return q

    # 宽基 ETF 是已知缺口：如实说明，不查个股表（那里没有 ETF 持仓）
    if "etf" in rest.lower() or "基金持仓" in rest:
        q.etf_gap = True

    # 判断/分析意图 → 不接管
    if any(w in raw for w in _ANALYSIS_WORDS):
        q.mode = "not_ours"
        return q

    # 榜单口径
    has_float = ("流通股东" in raw) or ("流通股" in raw) or ("float" in raw.lower())
    has_total = ("十大股东" in raw) or ("股东榜" in raw) or ("total" in raw.lower())
    if has_float and has_total:
        q.ambiguous = (
            "【机构持仓直查】十大流通股东（float）和十大股东（total）是两个口径，"
            "混在一起算会重复/漏计。请指定其中一个（缺省为十大流通股东）。"
        )
        return q
    q.kind = "total" if (has_total and not has_float) else "float"

    # 变动词
    kinds = [k for k, aliases in nt.CHANGE_KIND_ALIASES.items() if any(a in raw for a in aliases)]
    q.change_kinds = tuple(dict.fromkeys(kinds))
    if q.change_kinds or any(w in raw for w in ("变动", "环比", "调仓", "变化")):
        q.mode = "changes"

    # 个股（只认 6 位代码；名字交给主流程解析，绝不在这条路猜代码）
    m = _CODE_RE.search(raw)
    if m:
        q.stock_code = m.group(1)

    # 报告期
    dm = _DATE_RE.search(raw)
    if dm:
        y, mo, d = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        try:
            from datetime import date

            q.report_date = date(y, mo, d).isoformat()
            q.period_hint = "explicit"
        except ValueError:
            q.ambiguous = f"【机构持仓直查】「{dm.group(0)}」不是有效日期，请给出报告期（如 2026-09-30）。"
            return q
    else:
        if any(w in raw for w in ("上期", "上个季度", "上一期", "上季度")):
            q.period_hint = "prev"
        elif "去年同期" in raw:
            q.period_hint = "yoy"
        elif any(w in raw for w in ("最新", "本期", "这期", "这个季度", "这季", "当前")):
            q.period_hint = "latest"
        else:
            ym = _YEAR_RE.search(raw)
            if ym and not re.search(r"(20\d{2})\s*[-/年]\s*\d{1,2}", raw):
                q.ambiguous = (
                    f"【机构持仓直查】只给了「{ym.group(1)}年」，请给出具体报告期"
                    "（季报报告期末，如 2026-09-30）。"
                )
                return q
            if re.search(r"(20\d{2})\s*[-/年]\s*\d{1,2}", raw) and not dm:
                q.ambiguous = (
                    "【机构持仓直查】请给出完整报告期（YYYY-MM-DD，季报报告期末），"
                    "只到月份的日期我不猜。"
                )
                return q
            q.period_hint = "latest"

    # 有机构名但没有任何持仓/变动意图 → 追问（不猜用户想问什么）
    if q.mode == "holdings" and not any(w in raw for w in _HOLDING_WORDS):
        q.ambiguous = _HELP_TEXT
        return q

    return q


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "--"
    a = abs(float(v))
    if a >= 1e12:
        return f"{float(v) / 1e12:.2f} 万亿"
    if a >= 1e8:
        return f"{float(v) / 1e8:.2f} 亿"
    if a >= 1e4:
        return f"{float(v) / 1e4:.2f} 万"
    return f"{float(v):.0f}"


def _fmt_shares(v: float | None) -> str:
    if v is None:
        return "--"
    a = abs(float(v))
    if a >= 1e8:
        return f"{float(v) / 1e8:.2f} 亿股"
    if a >= 1e4:
        return f"{float(v) / 1e4:.2f} 万股"
    return f"{float(v):.0f} 股"


def _engine_of(db: Any) -> Any:
    """从请求上下文里取出**线程安全的 Engine**（拿不到 → None）。

    Engine 自己线程安全（每次 `connect()` 从池里借连接），`Session` **不是** ——
    把请求线程的 Session 丢进 worker 线程会串事务/串连接，所以这里只取引擎：

    - 传进来的已经是 Engine → 原样用；
    - 是 Session → `get_bind()`；若绑在单个 `Connection` 上（测试/特殊场景），
      取它的 `.engine`，不把那条连接带进别线程；
    - 其他（None / 无法识别的对象）→ None，调用方按「取数不可用」拒绝作答，
      绝不降级成「零持仓」。
    """
    if db is None:
        return None
    from sqlalchemy.engine import Connection, Engine

    if isinstance(db, Engine):
        return db
    get_bind = getattr(db, "get_bind", None)
    if callable(get_bind):
        try:
            bind = get_bind()
        except Exception:  # noqa: BLE001 —— 取不到连接就按不可用处理
            return None
        if isinstance(bind, Engine):
            return bind
        inner = getattr(bind, "engine", None)
        return inner if isinstance(inner, Engine) else None
    if isinstance(db, Connection):
        inner = getattr(db, "engine", None)
        return inner if isinstance(inner, Engine) else None
    return None


class _PeriodUnavailable(Exception):
    """本模块内就确定不了报告期（只有一期 / 没有该期）—— 文案可直接展示，不查库。"""


def _fetch(engine: Any, q: Question) -> dict[str, Any]:
    """同步取数（**在 worker 线程里跑**）：报告期解析 → core 查询 → 汇总。

    只用 Engine、只读，不碰请求的 Session；任何底层异常原样上抛，由 `answer`
    统一转成非敏感文案。
    """
    prev_override: str | None = None
    periods = nt.persisted_periods(engine, q.kind) if q.period_hint in {"prev", "yoy"} else []
    if q.period_hint == "prev" and periods:
        latest = periods[0]
        prior_quarter = {
            "03-31": f"{int(latest[:4]) - 1}-12-31",
            "06-30": f"{latest[:4]}-03-31",
            "09-30": f"{latest[:4]}-06-30",
            "12-31": f"{latest[:4]}-09-30",
        }.get(latest[5:])
        if not prior_quarter or prior_quarter not in periods:
            coverage = "只有一期数据" if len(periods) == 1 else "已采集报告期不连续"
            missing = f"缺少 {prior_quarter}" if prior_quarter else "最新采集日期不是季度末"
            raise _PeriodUnavailable(
                f"{coverage}（最新为 {latest}），{missing}，无法回答「上期」：不拿最新一期顶替，也不跳期。"
            )
        q.report_date = prior_quarter
    elif q.period_hint == "yoy" and periods:
        # 「去年同期」有两种问法，不能混成一句：
        # · 看持仓 → 看去年同季那一期本身（把报告期挪过去即可）；
        # · 看变动 → 要的是「本期 vs 去年同季」的对比，报告期必须留在本期，
        #   否则会变成「去年同季 vs 它的上一季」，答非所问（2026-11-21 复审实证）。
        base = str(q.report_date or periods[0])
        last_year = f"{int(base[:4]) - 1}{base[4:]}"
        if q.mode == "changes":
            q.report_date = base
            prev_override = last_year
            # 同口径深链必须能还原「本期 vs 去年同季」；只带本期会让页面算成环比。
            q.prev_report_date = last_year
        else:
            q.report_date = last_year

    summary: dict[str, Any] = {}
    if q.mode == "changes":
        data = nt.changes(
            engine,
            report_date=q.report_date,
            kind=q.kind,
            holder=q.holder_csv,
            change=q.change_kinds or None,
            limit=10,
            prev_report_date=prev_override,
        )
    else:
        data = nt.holdings(
            engine,
            report_date=q.report_date,
            kind=q.kind,
            holder=q.holder_csv,
            q=q.stock_code,
            sort="market_cap",
            desc=True,
            limit=8,
        )
        summary = nt.scope_summary(
            engine,
            report_date=data.get("report_date"),
            kind=q.kind,
            holder=q.holder_csv,
            q=q.stock_code,
        )
    return {"data": data, "summary": summary}


def _page_link(q: Question, report_date: str | None, change_kinds: tuple[str, ...]) -> str:
    """站内同口径页（页面读这些 query 参数，见 `frontend/src/pages/NationalTeam.tsx`）。

    `prev_report_date` 只在同比变动时出现：页面拿它调 `/changes`，否则只凭本期
    `report_date` 会按相邻上一季重算成环比（2026-11-24 复审实证）。
    """
    params = [f"kind={q.kind}"]
    if report_date:
        params.append(f"report_date={report_date}")
    if q.mode == "changes" and q.prev_report_date:
        params.append(f"prev_report_date={q.prev_report_date}")
    if q.holder_csv:
        params.append(f"holder={q.holder_csv}")
    params.append(f"view={q.mode}")
    if change_kinds:
        params.append("change=" + ",".join(change_kinds))
    if q.stock_code:
        params.append(f"q={q.stock_code}")
    return "/national-team?" + "&".join(params)


_CALIBER_LINES = (
    "· 数据来源：东方财富十大股东 / 十大流通股东报表（季报披露）。",
    "· 榜单「新进/退出」只代表本期是否进入前十大股东/十大流通股东，**不等于实际买入/卖出**"
    "（退出也可能只是被挤出前十）。",
    "· 季报披露口径：报告期末持仓，不是实时持仓。",
    "· 不含宽基 ETF（汇金的沪深300 等 ETF 持仓在基金半年报/年报「前十大持有人」披露，无公开汇总）。",
    "· 十大流通股东与十大股东是两个口径，本次只用一种，不混算。",
)


def _footer(kind_label: str, link: str, snapshot_at: str | None) -> str:
    lines = ["", "口径提示：", *_CALIBER_LINES]
    lines.append(f"· 本次口径：{kind_label}。")
    if snapshot_at:
        lines.append(f"· 采集时点：{snapshot_at}。")
    lines.append(f"可复核（同口径页面，可换机构/报告期）：[打开国家队动向]({link})")
    return "\n".join(lines)


def _refuse(reason: str, link: str | None = None) -> str:
    text = f"【机构持仓直查】{reason}"
    if link:
        text += f"\n站内同口径页面：[打开国家队动向]({link})"
    return text


def _note_failure(runtime, q: "Question", error: str, detail: str | None) -> None:
    """取数/口径失败也落一步 failed。

    只落「为什么失败」的分类，不落 SQL、连接串或异常正文；落库后
    `finish_copilot_run` 会据此把 run 判成 degraded/error，而不是 ok。
    埋点是旁路：任何异常都吞掉，不影响对外作答。
    """
    trace = getattr(runtime, "trace", None)
    if trace is None:
        return
    try:
        trace.step(
            "institution_holdings",
            {"scopes": [s["value"] for s in q.scopes], "kind": q.kind, "mode": q.mode},
            ok=False,
            error=error[:120],
            result=(detail or "")[:120] or None,
        )
    except Exception:  # noqa: BLE001 —— 埋点失败不影响作答
        pass


_HOLDINGS_SORT_LABEL: dict[str, str] = {
    "market_cap": "持股市值",
    "hold_num": "持股数",
    "hold_ratio": "持股比例",
    "stock_code": "股票代码",
}


def _render_holdings(q: Question, data: dict[str, Any], summary: dict[str, Any]) -> str:
    rows = data.get("items") or []
    count = int(data.get("count") or 0)
    stocks = int(summary.get("stocks") or 0)
    holders = int(summary.get("holders") or 0)
    head = [
        f"【机构持仓直查 · {q.label}】",
        f"报告期 {data.get('report_date')}（{data.get('report_kind_label')} · 季报口径，非实时）",
    ]
    if q.stock_code:
        head.append(f"标的 {q.stock_code}")
    # 只数按股票去重，记录数按「账户 × 股票」—— 同一只票被汇金投资和汇金资管同时持有时是两个账户。
    head.append(
        f"共 {stocks} 只 · {count} 条持仓记录 · {holders} 个账户"
        f" · 持股市值 {_fmt_money(summary.get('market_cap'))}"
        f" · 持股 {_fmt_shares(summary.get('hold_num'))}"
        + (
            f"（{int(summary.get('market_cap_missing') or 0)} 条缺市值，合计为下限）"
            if summary.get("market_cap_missing")
            else ""
        )
    )
    body: list[str] = []
    if rows:
        # 文案按 core 回显的真实排序写，不自己猜「按市值」。
        sort_label = _HOLDINGS_SORT_LABEL.get(str(data.get("sort") or "market_cap"), "持股市值")
        body.append(f"持仓明细（按{sort_label}{'降序' if data.get('desc', True) else '升序'}）：")
        for i, r in enumerate(rows, 1):
            bits = [f"{i}. {r.get('stock_code')} {r.get('stock_name') or '—'}",
                    _fmt_money(r.get("market_cap"))]
            bits.append(f"持股 {_fmt_shares(r.get('hold_num'))}")
            if r.get("hold_ratio") is not None:
                bits.append(f"占总股本 {float(r['hold_ratio']):.2f}%")
            if r.get("holder_rank"):
                bits.append(f"第{r['holder_rank']}大")
            if r.get("shares_type"):
                bits.append(str(r["shares_type"]))
            if r.get("holder_name") and r.get("holder_name") != r.get("holder_group"):
                bits.append(str(r["holder_name"]))
            body.append(" · ".join(str(b) for b in bits))
        if count > len(rows):
            body.append(f"（共 {count} 条记录，这里只列前 {len(rows)} 条；完整名单见下方同口径页面。）")
        else:
            body.append(f"（共 {count} 条记录，已全部列出。）")
    else:
        body.append("（本报告期该机构没有命中持仓。）")
    return "\n".join(head + [""] + body)


def _comparison_label(data: dict[str, Any]) -> str:
    """比较口径的标题词：环比 / 同比 / 跨期对比。

    绝不把跨期对比写成「环比」—— 跨几个季度的「新进/退出」语义会失真，
    用户看到日期也未必去算跨度。
    """
    gap = data.get("compare_gap")
    if data.get("comparison") == "yoy":
        return "同比变动（与去年同季）"
    if isinstance(gap, int) and gap != 1:
        return f"与 {data.get('prev_report_date')} 相比（跨 {gap} 个季度）"
    return "环比变动"


def _render_changes(q: Question, data: dict[str, Any]) -> str:
    counts = data.get("counts") or {}
    wanted = q.change_kinds or tuple(nt.CHANGE_KINDS)
    head = [
        f"【机构持仓直查 · {q.label} · {_comparison_label(data)}】",
        f"{data.get('report_date')} vs {data.get('prev_report_date')}"
        f"（{nt.REPORT_KIND_LABEL.get(q.kind, q.kind)} · 季报口径）",
        f"新进 {counts.get('new', 0)} · 加仓 {counts.get('increase', 0)}"
        f" · 减仓 {counts.get('decrease', 0)} · 退出 {counts.get('exit', 0)}"
        f" · 未变 {counts.get('unchanged', 0)}",
    ]
    label = {"new": "新进", "increase": "加仓", "decrease": "减仓", "exit": "退出"}
    rows = data.get("items") or []
    body: list[str] = []
    if rows:
        # 文案对齐 core 的真实排序：先变动类型（新进→加仓→减仓→退出），
        # 同类型内按变动股数绝对值降序（不是全表按股数排序）。
        body.append(
            "变动明细（" + "/".join(label[k] for k in wanted)
            + "；先按变动类型，组内按变动股数绝对值从大到小）："
        )
        for i, it in enumerate(rows, 1):
            sign = "+" if float(it.get("delta_num") or 0) > 0 else ""
            body.append(
                f"{i}. [{label.get(it['kind'], it['kind'])}] {it.get('stock_code')} "
                f"{it.get('stock_name') or '—'} · {sign}{_fmt_shares(it.get('delta_num'))}"
                f"（{_fmt_shares(it.get('prev_num'))} → {_fmt_shares(it.get('cur_num'))}）"
            )
        total = int(data.get("count") or 0)
        if data.get("truncated"):
            body.append(f"（共 {total} 条，这里只列前 {len(rows)} 条；完整名单见下方同口径页面。）")
        else:
            body.append(f"（共 {total} 条，已全部列出。）")
    else:
        body.append("（该机构本期没有这一变动类型的记录。）")
    return "\n".join(head + [""] + body)


async def answer(db, text: str, bound: str | None, user, runtime=None) -> str | None:
    """机构披露持仓的确定性直查；不是这类问题、或证据不足时返回 `None` 交回主流程。

    与 `src/harness/quick_answer.answer` 同签名，方便上游共用一条旁路。
    `bound` 只可能是个股代码 —— **绝不当机构**，也不参与解析。
    """
    q = parse_question(text)
    if not q.scopes:
        return None                      # 没点机构：不是本模块的问题
    if q.mode == "not_ours":
        return None                      # 分析/混合问题：交回主流程
    if q.etf_gap:
        return (
            "【机构持仓直查】本模块是个股直投口径（十大股东/十大流通股东），"
            "**不含宽基 ETF 持仓**：汇金的沪深300 等 ETF 重仓只在基金半年报/年报的"
            "「前十大持有人」里披露，没有公开汇总接口，因此这里无法给出 ETF 数字。\n"
            "如需个股持仓，可问「汇金 2026-09-30 十大流通股东持仓」。"
        )
    if q.ambiguous:
        return q.ambiguous

    # 权限：服务端重新核 view_quotes（与路由级 require_perm 同一口径）。
    # 用户信息缺失 → 拒绝（不知道是谁，就不能给数据）。
    # 这一步刻意留在事件循环线程：它用的是请求自带的 Session（trial 计数还要写库），
    # 跨线程用 Session 会串事务；只有只读持仓查询才 offload 到 worker 线程。
    if user is None or getattr(user, "id", None) is None:
        return _refuse("未识别到登录用户，无法直查机构持仓。请先登录（该页需要行情浏览权限）。")
    try:
        from src.core.permissions import enforce_perm

        enforce_perm(user, _QUOTE_PERM, db)
    except Exception as exc:  # noqa: BLE001 —— 权限异常一律保守拒绝
        detail = getattr(exc, "detail", None)
        msg = detail.get("message") if isinstance(detail, dict) else detail
        return _refuse(f"当前账号没有行情浏览权限（{msg or _QUOTE_PERM}），无法直查机构持仓。")

    engine = _engine_of(db)
    if engine is None:
        _note_failure(runtime, q, "data_unavailable:engine_unavailable", None)
        return _refuse("数据连接不可用，本次未取到数据（不是「零持仓」）。")

    # 报告期解析 + core 查询全在 worker 线程里跑（core 是同步阻塞 SQL，直接在事件循环
    # 里调用会卡住整个对话）。`persisted_periods` 也在这段保护内 —— 它同样是查库，
    # 放在保护外会让一次库故障直接抛穿 async 调用方。
    try:
        payload = await asyncio.to_thread(_fetch, engine, q)
    except _PeriodUnavailable as exc:
        # 口径性失败（只有一期/期不存在）也要落 failed：run 若以 ok 收尾，
        # 「没拿到可对比数据」会被治理页与基线记成答完了。
        _note_failure(runtime, q, "period_unavailable", str(exc))
        return _refuse(str(exc), _page_link(q, None, q.change_kinds))
    except Exception as exc:  # noqa: BLE001 —— 取数失败必须说失败，不能说零变动/无持仓
        logger.warning("[institution_holdings] 取数失败: %s", type(exc).__name__)
        _note_failure(runtime, q, "fetch_failed", type(exc).__name__)
        return _refuse(_FETCH_ERROR_TEXT, _page_link(q, q.report_date, q.change_kinds))

    data = payload["data"]
    summary = payload["summary"]
    snapshot = summary.get("snapshot_at") if q.mode != "changes" else None

    status = str(data.get("status") or "")
    link = _page_link(q, data.get("report_date"), q.change_kinds)

    if status in {"empty_table", "unknown_period", "insufficient_periods", "empty_result",
                  "non_adjacent_period", "prev_unavailable"}:
        if status != "empty_result":     # empty_result = 该股本期确实没持仓, 是有效答案
            _note_failure(runtime, q, f"data_unavailable:{status}", None)
        return _refuse(str(data.get("reason") or "没有可展示的数据。"), link)
    if status == "zero_change":
        counts = data.get("counts") or {}
        text_out = (
            f"【机构持仓直查 · {q.label} · {_comparison_label(data)}】\n"
            f"{data.get('report_date')} vs {data.get('prev_report_date')}"
            f"（{nt.REPORT_KIND_LABEL.get(q.kind, q.kind)}）\n"
            f"本期共 {counts.get('unchanged', 0)} 只持股数与比较期完全相同"
            "（**确实零变动**，不是取数失败）。"
        )
        return text_out + _footer(nt.REPORT_KIND_LABEL.get(q.kind, q.kind), link, None)

    if q.mode == "changes":
        body = _render_changes(q, data)
    else:
        body = _render_holdings(q, data, summary)

    _trace = getattr(runtime, "trace", None)
    if _trace is not None:
        try:
            _trace.step(
                "institution_holdings",
                {"scopes": [s["value"] for s in q.scopes], "kind": q.kind,
                 "report_date": data.get("report_date"), "mode": q.mode},
                ok=True,
                result=body[:400],
                elapsed_ms=None,
            )
        except Exception:  # noqa: BLE001 —— 埋点失败不影响作答
            pass

    return body + _footer(nt.REPORT_KIND_LABEL.get(q.kind, q.kind), link, snapshot)


__all__ = ["answer", "parse_question", "Question"]
