"""全市场 / 多标的确定性管线（2026-10-23）。

**为什么另起一个模块**：`pipeline.py` 的管线是「一个问题 → 一两只票 → 全部工具都跑一遍」，
入口第一句就是 `symbols_of(question)`；解析不到代码就直接返回 None 回落多轮 loop。
盘前策略官 / 盘后复盘官 / 竞价猎手 / 盘中盯盘官这类问题天生**没有标的**（"今天怎么看"、
"帮我复盘今天"、"今天竞价谁爆量"），于是这 6 份配方被留在多轮 tool loop 里：
2~4 个 LLM round-trip、p50 45~90s、工具集合 Jaccard 0.57。
它们被落下的原因是「解析不到代码」，**不是**「工具要代码」。

**做法**：按 schema 的 `required` 把配方工具分三段，而不是一律 `{"symbol": sym}`：

1. `market`：`required` 为空 → 用 `{}` 直接跑。全市场结论的主体都在这一段
   （hot_stocks / sentiment_cycle / market_news / strategy_signals / opportunities /
   scan_auction_anomaly / get_auction_data / get_portfolio / get_market_anomalies …）。
2. `symbol`：`required` 含 `symbol` → 有自选/持仓时按最多 8 只展开，每只最多 3 个这类工具
   （`MAX_WATCHLIST_SYMBOLS` / `MAX_SYMBOL_TOOLS`）；**没有自选就不跑**，但在轨迹里留
   `ok=False, error="no_symbol_scope"`，结论于是可以诚实地写「未取到」而不是让模型编。
3. `unsupported`：`required` 里有 keyword / query 之类市场级无从填的参数（`search_symbols`、
   `get_wencai_enhanced`）→ 不跑，记 `error="unsatisfiable_args:<names>"`。
   注册表里查不到的工具（`unknown_tool`）与 `schema=None` 的 handler-only 工具
   （`no_schema`，当前只有 `get_opportunities`，故意不给模型看的荐股口径工具）同样不跑 ——
   fail-closed，绝不盲调，也绝不把合规上隐藏的工具悄悄复活。

三条纪律与 `pipeline.py` 逐字对齐：有界并发（复用 `_concurrency()`，重型工具走串行锁）、
单工具超时（`PER_TOOL_TIMEOUT`）、证据裁剪（单工具 `PER_TOOL_CHARS` / 合计
`TOTAL_EVIDENCE_CHARS`）；另外多一道**墙钟预算**（`PIPELINE_BUDGET`，默认 90s）：
展开后最多 24 次调用，60s 单工具超时 × 8 波排队是"用户早走了"的量级，到点没发的
工具直接记 `budget_exhausted`。**所有工具都没取到 → 返回 None**，调用方回落原来的多轮 loop
（管线挂了不许变成没答案）。记录类工具（`fallback=False`）照旧只写库、不进取数 lane。

**上下文透传（2026-11-07 补齐）**：调用方（`chat.py` → `pipeline_router`）对两条管线
是同一次调用，会递 `context` / `fact_pack` / `evidence_sink` / `view_state` / `db` / `user`。
这里**只接收真的会消费的那部分**，逐项说清楚，避免「加了形参不作数」：

- `context`：会话上下文（本人/页面/持仓/偏好）→ 拼进 system。消费。
- `fact_pack`：委派场景父 run 冻结的事实包。**只消费 `as_of` / `source` / `text` 三个键**
  的冻结锚点（与 `pipeline.compose_prompt` 同口径），绝不把包里的标的快照塞进全市场
  结论 —— 全市场问题没有单标的，塞进去就是假证据。
- `evidence_sink`：出站证据落点（SSE `done` 里 `_attach_evidence` 读的就是它）。
  结论真的产出后写 `evidence_pack` 结构；**回落路径一条都不写**。
  未给 sink 时退回 `evidence_pack.CURRENT_SINK`（生产路径就是这么走的）：
  只 `extend` 那个 list、不 `set` 变量，所以不会跨请求/跨上下文串味。
- `view_state` / `db` / `user`：**本模块不接收**。`view_state` 是单标的页面锚点
  （`propose_levels` 用），全市场结论没有那一个标的；`db`/`user` 已在路由层由
  `resolve_watchlist` 消费完，再传下来就是死形参。

**边界**：纯增量模块，不改 `pipeline.py` 的既有语义，也不碰 `chat.py`。
接线由集成方做：`recipe.pipeline and recipe.scope == "market"` → 走这里，否则走原路。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time as _time
from dataclasses import dataclass, field
from datetime import date as _date

from src.harness.pipeline import (
    PER_TOOL_CHARS,
    PER_TOOL_TIMEOUT,
    TOTAL_EVIDENCE_CHARS,
    _concurrency,
    _emit_evidence,
    _is_heavy_tool,
    _ms,
    _record,
    _split_pipeline_tools,
)

logger = logging.getLogger(__name__)

# 展开上限：8 只 × 3 个工具 = 24 次调用。再多就不是「取数」而是全市场扫了，
# 而且结论段落也塞不下（证据 12000 字上限先爆）。
MAX_WATCHLIST_SYMBOLS = 8
MAX_SYMBOL_TOOLS = 3

# 一轮全市场取数的墙钟上限。单工具 60s × 最多 24 次调用 ÷ 并发 3 = 最坏 8 分钟，
# 那是"用户早就走了"的量级；到点还在排队的工具直接记 budget_exhausted，不排队干等。
# env 可覆盖（`PIPELINE_MARKET_BUDGET=0` 即所有工具都不发，只在排障/单测用）。
PIPELINE_BUDGET = 90.0

# 未取数的原因（写进轨迹与结论提示词，措辞固定，便于治理页聚合）
SKIP_NO_SYMBOL = "no_symbol_scope"
SKIP_UNSATISFIABLE = "unsatisfiable_args"
SKIP_UNKNOWN = "unknown_tool"
SKIP_HANDLER_ONLY = "no_schema"
SKIP_SYMBOL_CAP = "symbol_tool_cap"

# 工具"回了字"不等于"给到了证据"（2026-11-10 复审 P0）。
# 但**判据只能是声明，不能是正文里出现过什么字**：上一版用子串匹配（`未开通`/`无数据`
# 出现在前 300 字就算拒权/空库）会把 `「融资融券未开通的标的不计入」` 这种口径说明
# 当拒权，静默删掉一条可用证据 —— 比假证据更坏。
#
# 现在只认两种**声明**，都有稳定契约：
#   a. 执行层/我方 renderer 的**首行**固定文案（拒权/失败/无数据/数据日）；
#   b. 工具返回的 JSON 对象里的显式字段（`available` / `data_date_status` / `symbols`）。
# 认不出的就是"未知"：未知绝不冒充今日，也绝不改写成"没有"。
#
# 首行的**域名标签**是渲染约定，不是正文（2026-11-10 复审第 2 轮 P0）：
#   生产真实拒权行是 `[市场扫描榜] 无权限：该榜为 Pro 专属功能…`，
#   复盘失败行是 `[盘后复盘] 读取异常：KeyError…`。
# 旧实现只比对整行首字符前缀，于是这些**真实声明一条都没被识别**，
# 全被记成「已获可用证据」写进结论。标签由我方 renderer 自己写在首行，
# 所以「标签 + 声明词」同样是稳定契约 —— 认它，不等于扫正文。
_LABEL_RE = re.compile(r"^(?:\[[^\]\n]{1,40}\]|【[^】\n]{1,40}】)\s*")

DENIED_PREFIXES = (
    "无权限", "需要登录", "权限不足", "未授权", "该工具需要已登录的用户身份",
)
#: 显式声明「这次没取到」（不是拒权，也不是"没有数据"）
FAILED_PREFIXES = (
    "工具执行出错", "取数失败", "读取异常", "未知工具", "本轮不使用工具",
    "请提供股票代码",
)
#: 显式声明「本条没有数据」——**只在带标签的首行**认（`[市场扫描榜] 暂无三榜快照…`、
#: `[盘后复盘] 无数据。`）。无标签的同名词太容易是正文（"暂无涨停"），见下方正则。
NO_DATA_PREFIXES = ("暂无", "无数据", "未取到")
#: 首行是复合文案的失败声明（如 registry 的预算拒绝，前缀是本轮配方名）
_FAILED_PATTERNS = (
    re.compile(r"^.+?的工具预算（\d+\s*次）已用尽"),
    # 执行层的白名单拒绝：`{配方名} 本轮不使用工具 \`x\`…`
    re.compile(r"^.{0,30}? 本轮不使用工具 "),
    # 各 handler 自己的失败开场（`同花顺热榜查询失败: …` / `…拉取出错: …`）。
    # 只认**首行**且必须带冒号：正文里出现"失败"二字是行情描述，不是这条证据的状态。
    re.compile(r"^.{0,24}?(?:失败|出错|报错)[:：（]"),
)
#: 无标签首行里的「没有数据」自述（工具自己写的开场，不是数据里的字）
_NO_DATA_PATTERNS = (
    # `同花顺热榜暂无数据(可能非交易时段…)` / `东财异动池暂无数据(…)`
    re.compile(r"^[^。\n]{0,24}?暂无数据"),
    # `通达信客户端(TQ)当前取不到该数据(...)`
    re.compile(r"^[^。\n]{0,16}?取不到该数据"),
)

#: 数据日的**声明行**。只有工具自己说"数据日是 X"才算数：
#: 正文里最晚的日期、页脚日期、未来事项日期都不是数据日（2026-11-10 复审阻断）。
_DECL_DAY = r"(\d{4}-\d{2}-\d{2})"
_DECL_STALE_RE = re.compile(r"^快照已过期[:：]\s*数据日\s*" + _DECL_DAY)
_DECL_DAY_RE = re.compile(r"^数据日\s*" + _DECL_DAY)
_DECL_UNKNOWN_MARKERS = (
    "数据日期未核实", "数据日期未知", "基准日未知", "时效不可判断",
)
#: 首行可能带的装饰字符（⚠️ 等）
_HEAD_NOISE = " \t\u26a0\ufe0f\u3000"


def _first_line(text: str) -> str:
    """正数第一行（去掉装饰字符）—— 声明只可能出现在这里。"""
    for line in str(text or "").splitlines():
        body = line.strip()
        if body:
            return body.lstrip(_HEAD_NOISE)
    return ""


def _declaration(text: str) -> tuple[str, str]:
    """首行 → (域名标签, 标签后的正文)。无标签时标签为空串。

    标签是 renderer 自己写在首行的域名（`[市场扫描榜] `、`【盘后复盘证据】 `），
    声明词跟在它后面。只剥**一个**前缀标签，不做任何其它清洗。
    """
    head = _first_line(text)
    match = _LABEL_RE.match(head)
    if not match:
        return "", head
    return match.group(0).strip(), head[match.end():].strip()


def _json_payload(text: str) -> dict | None:
    """工具返回的 JSON 对象；不是对象就返回 None（不做任何修补/猜测）。"""
    body = str(text or "").strip()
    if not body or body[0] not in "{[":
        return None
    try:
        payload = json.loads(body)
    except Exception:  # noqa: BLE001 - 非 JSON 是常态（多数工具回人话）
        return None
    return payload if isinstance(payload, dict) else None


def tool_status(text: str) -> str:
    """把工具返回分成 ok / denied / unavailable（只读**声明**，不扫正文）。

    - `denied`：首行就是我们/执行层的拒权文案（"无权限…"、"需要登录…"），
      允许带域名标签（`[市场扫描榜] 无权限：…`）；
    - `unavailable`：首行是取数失败/工具缺失/预算用尽/显式"无数据"，
      或 JSON 显式 `available=False`。
    正文里出现同样的词**不算** —— 那是口径说明，不是这条证据的状态。
    """
    payload = _json_payload(text)
    if payload is not None:
        if payload.get("denied") is True:
            return "denied"
        if payload.get("available") is False or payload.get("ok") is False:
            return "unavailable"
    label, body = _declaration(text)
    # 固定首行来自对应真实 handler；不扫描正文中的普通叙述。
    if (body.startswith(("暂无实时资讯数据", "暂无策略信号（未找到", "取不到数据：", "取不到数据:"))
            or body.startswith(("情绪周期: 涨停池数据获取失败", "情绪周期：涨停池数据获取失败"))):
        return "unavailable"
    if label:
        # 标签行的声明词位置是稳定的：标签 → 声明词 → 解释。
        if body.startswith(DENIED_PREFIXES):
            return "denied"
        if body.startswith(FAILED_PREFIXES) or body.startswith(NO_DATA_PREFIXES):
            return "unavailable"
        if any(p.match(body) for p in _FAILED_PATTERNS):
            return "unavailable"
        return "ok"
    head = body
    if head.startswith(DENIED_PREFIXES):
        return "denied"
    if head.startswith(FAILED_PREFIXES) or any(p.match(head) for p in _FAILED_PATTERNS):
        return "unavailable"
    if any(p.match(head) for p in _NO_DATA_PATTERNS):
        return "unavailable"
    return "ok"


def _cn_today() -> _date:
    """北京时间的"今天"（与证据 renderer 同一时钟，不用宿主墙钟）。"""
    try:
        from src.core.trade_phase import cn_now

        return cn_now().date()
    except Exception:  # noqa: BLE001 - 时钟模块不可用也不能把日期判成今天
        return _date.today()


def _classify_day(day: str) -> tuple[str, str | None]:
    """(状态, 数据日)。状态 ∈ {"today","not_today","unknown"}。"""
    try:
        parsed = _date.fromisoformat(str(day or "").strip())
    except ValueError:
        return "unknown", None
    return ("today" if parsed == _cn_today() else "not_today"), parsed.isoformat()


def declared_data_day(text: str) -> tuple[str, str | None]:
    """证据**声明的**数据日 → (状态, ISO 日期)。

    只认两处声明，认不出返回 `("unknown", None)`：
    1. JSON 里 `data_date` + `data_date_status == "verified"`
       （只带 data_date 不算 —— 与 `tools_scan_read._resolve_data_date` 同口径）；
    2. **无标签**首行声明行：`数据日 2026-11-10（…）` /
       `⚠️ 快照已过期：数据日 2026-11-09` / `数据日期未核实…`。
       这两句只由 `tools_scan_read._freshness_line` 在**结构化 data_date_status
       = verified** 时渲染，所以认它们等于认结构化字段。

    带**域名标签**的首行一律不算数据日（2026-11-10 复审 P0）：
    `【盘后复盘证据】20261110 · 口径 …` 里的日期是 `build_replay` 的
    **取数/时钟日**（无参调用直接 `datetime.now()`），池子取空时它照样写今天。
    把它当数据日 = 把"日期未知"标成"今日"。认不出就维持 unknown。
    绝不再从正文里取"最晚日期"：`2027-01-01 限售解禁` 会把未来日期冒充今日。
    """
    payload = _json_payload(text)
    if payload is not None:
        day = str(payload.get("data_date") or "").strip()
        if day and str(payload.get("data_date_status") or "") == "verified":
            return _classify_day(day)
    label, body = _declaration(text)
    if label:
        # 标签行只声明"这是什么域"，不声明数据日（见 docstring）。
        return "unknown", None
    if any(marker in body for marker in _DECL_UNKNOWN_MARKERS):
        return "unknown", None
    for pattern in (_DECL_STALE_RE, _DECL_DAY_RE):
        match = pattern.match(body)
        if match:
            return _classify_day(match.group(1))
    return "unknown", None


def declared_candidates(text: str) -> tuple[str, list[str]]:
    """证据**声明的**候选名单 → (状态, 代码)。

    状态 ∈ {"declared","unknown"}。只认 JSON 里 `symbols` / `candidates` 这种
    **结构化名单**；正文里的 6 位数字一律不算（`成交额 3001234.56 万` 会造出假代码）。
    `unknown` ≠ "没有候选"，两者的措辞在 `missing_note()` 里是分开的。

    现实约束（2026-11-10 复审）：市场级工具目前**全部返回人话字符串**（三榜/DDE/
    复盘/热榜/异动），没有一个是 symbols JSON。所以生产里这条几乎总是 `unknown`
    —— 结论必须写"名单未结构化声明"，不许写成"已有候选"或"没有候选"。
    """
    payload = _json_payload(text)
    if payload is None:
        return "unknown", []
    for key in ("candidates", "symbols"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        codes: list[str] = []
        for row in rows:
            if isinstance(row, dict):
                code = str(row.get("symbol") or row.get("code") or "").strip()
            else:
                code = str(row or "").strip()
            if code and code not in codes:
                codes.append(code)
        return "declared", codes
    return "unknown", []


@dataclass
class MarketRun:
    """一次全市场取数的账本。`outputs` 的标签带 `@代码`（同一工具可能跑多只）。"""

    market: str = "CN"
    outputs: list[tuple[str, str]] = field(default_factory=list)
    ok: int = 0
    failed: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 拒权 / 取不到分开记：它们不是"工具挂了"，也不是"没有数据"，
    # 结论里必须写清是哪种缺口（治理页也要能按原因聚合）。
    denied: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    # (标签, 数据日)：工具**声明**的数据日不是今天，只能算"非今日"，不许当今日。
    stale: list[tuple[str, str]] = field(default_factory=list)
    # 标签 → 工具声明的数据日（ISO）。没有声明的一律进 `undated`（时效未知）。
    dates: dict[str, str] = field(default_factory=dict)
    undated: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.ok > 0

    @property
    def data_date(self) -> str | None:
        """**工具声明过的**数据日：只有一个日期时给出；未声明或混杂 → None（不猜）。"""
        days = set(self.dates.values())
        return days.pop() if len(days) == 1 else None

    @property
    def data_date_status(self) -> str:
        """verified = 全部成功证据都声明了同一个数据日；partial = 只有一部分声明了；
        unverified = 没有声明或声明互相矛盾（这两种都不得称今日/实时）。"""
        days = set(self.dates.values())
        if not days:
            return "unverified"
        if len(days) > 1:
            return "unverified"
        return "partial" if self.undated else "verified"

    @property
    def candidates(self) -> list[str]:
        """**结构化声明过**的候选代码（去重保序）—— 不从正文数字里扫。"""
        out: list[str] = []
        for _, text in self.outputs:
            state, codes = declared_candidates(text)
            if state != "declared":
                continue
            for code in codes:
                if code not in out:
                    out.append(code)
        return out

    @property
    def candidates_status(self) -> str:
        """declared = 有工具给了结构化名单；unknown = 没人声明（≠「没有候选」）。"""
        for _, text in self.outputs:
            if declared_candidates(text)[0] == "declared":
                return "declared"
        return "unknown"

    @property
    def no_candidates(self) -> bool:
        """只有"声明了名单且名单为空"才算没有候选。"""
        return self.candidates_status == "declared" and not self.candidates

    def missing_note(self) -> str:
        """一句结构化的缺口说明（进提示词，也进确定性前缀）。"""
        parts = []
        if self.denied:
            parts.append(f"拒权 {len(self.denied)} 项（{', '.join(self.denied[:3])}）")
        if self.unavailable:
            parts.append(
                f"未取到 {len(self.unavailable)} 项（{', '.join(self.unavailable[:3])}）")
        other_errors = [e for e in self.errors if e.split(':', 1)[0] not in
                        set(self.denied + self.unavailable + self.skipped)
                        and ':stale_data:' not in e]
        if other_errors:
            parts.append(f"调用失败 {len(other_errors)} 项（{', '.join(e.split(':', 1)[0] for e in other_errors[:3])}）")
        if self.stale:
            days = sorted({d for _, d in self.stale})
            parts.append(f"非今日数据 {len(self.stale)} 项（数据日 {', '.join(days)}）")
        if self.skipped:
            parts.append(f"未跑 {len(self.skipped)} 项")
        if self.undated:
            parts.append(
                f"数据日未声明 {len(self.undated)} 项（时效未知，不得称今日/实时）")
        if self.candidates_status == "unknown":
            parts.append("候选名单未声明（时效与覆盖未知，不是「没有候选」）")
        elif self.no_candidates:
            parts.append("工具声明无候选（不是「没有机会」）")
        if not parts:
            return ""
        return "本轮缺数据：" + "；".join(parts)


def evidence_payload(run: MarketRun) -> dict:
    """出站证据的缺口摘要（与 `evidence_pack` 的 ok/missing 互补，不替代它）。

    `candidates` **只在结构化声明过名单时**才出现：生产工具回的都是人话字符串，
    出站带一个空数组等于对外宣称"没有候选"，而事实是"没人声明过"。
    """
    payload = {
        "market": run.market,
        "ok": run.ok,
        "failed": run.failed,
        "skipped": list(run.skipped),
        "denied": list(run.denied),
        "unavailable": list(run.unavailable),
        "stale": [[label, day] for label, day in run.stale],
        "data_date": run.data_date,
        "data_date_status": run.data_date_status,
        "undated": list(run.undated),
        "candidates_status": run.candidates_status,
        "missing_note": run.missing_note(),
    }
    if run.candidates_status == "declared":
        payload["candidates"] = run.candidates
    return payload


@dataclass(frozen=True)
class _Step:
    """一条待执行的取数动作；`skip` 非空表示不执行、直接记一条失败轨迹。"""

    tool: str
    symbol: str = ""
    args: dict = field(default_factory=dict)
    skip: str = ""

    @property
    def label(self) -> str:
        return f"{self.tool}@{self.symbol}" if self.symbol else self.tool


# ---------------------------------------------------------------- 工具能力探测


def _schema_params(tool: str) -> dict | None:
    """取工具的 parameters；None = 调用方须按 fail-closed 处理（见 `tool_requirements`）。"""
    try:
        from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    except Exception:  # noqa: BLE001 - 注册表不可用：宁可判定为"不认识"，不盲调
        logger.warning("注册表不可用，全市场管线按 unknown_tool 处理: %s", tool)
        return None
    entry = CHAT_TOOL_REGISTRY.get(str(tool))
    if entry is None:
        return None
    schema = getattr(entry, "schema", None)
    if schema is None and isinstance(entry, dict):
        schema = entry.get("schema") or entry
    if not isinstance(schema, dict):
        return None
    params = (schema.get("function") or {}).get("parameters")
    return params if isinstance(params, dict) else {}


def tool_requirements(tool: str) -> tuple[str, list[str]]:
    """(状态, 必填参数名)。状态 ∈ {"ok", "unknown", "handler_only"}。

    `handler_only` 很重要：注册表里 `schema=None` 的工具（当前只有 `get_opportunities`）
    是**故意不给模型看**的（机会候选属于荐股口径）。它在配方 `tools` 里从来就调不到
    （`filter_schemas` 也过滤掉），管线不许把它悄悄复活 —— 那是越过合规判断的改动。
    """
    try:
        from src.agents.chat.registry import CHAT_TOOL_REGISTRY

        entry = CHAT_TOOL_REGISTRY.get(str(tool))
    except Exception:  # noqa: BLE001
        return "unknown", []
    if entry is None:
        return "unknown", []
    if getattr(entry, "schema", None) is None:
        return "handler_only", []
    params = _schema_params(tool)
    if params is None:
        return "unknown", []
    req = params.get("required")
    if not isinstance(req, (list, tuple)):
        return "ok", []
    return "ok", [str(x) for x in req]


def required_args(tool: str) -> list[str] | None:
    """工具的必填参数名；None = 查不到 / handler-only（两种都不跑）。"""
    status, req = tool_requirements(tool)
    return req if status == "ok" else None


def _declares(tool: str, prop: str) -> bool:
    params = _schema_params(tool)
    if params is None:
        return False
    props = params.get("properties")
    return isinstance(props, dict) and prop in props


def _symbol_args(tool: str, symbol: str, market: str) -> dict:
    """标的级工具的参数：`symbol` 必给；`market` 只在 schema 真的声明了才给。"""
    args = {"symbol": str(symbol)}
    if market and _declares(tool, "market"):
        args["market"] = str(market)
    return args


# ---------------------------------------------------------------- 计划构建


def build_steps(recipe, watchlist=None, market: str = "CN") -> list[_Step]:
    """把配方的 `tools` 展开成**按配方顺序**执行的步骤表。

    顺序即语义：证据块按配方书写顺序拼接，和模型自己按序调用得到的东西一致，
    审计时能一眼对上「配方第 3 个工具 → 证据第 3 段」。
    """
    data_tools, _post_tools = _split_pipeline_tools(getattr(recipe, "tools", ()))
    syms: list[str] = []
    for s in watchlist or ():
        code = str(s or "").strip()
        if code and code not in syms:
            syms.append(code)
    syms = syms[:MAX_WATCHLIST_SYMBOLS]

    # 先给每个工具定性，再按原顺序展开 —— 两遍扫描，避免展开顺序随分类顺序漂移
    disposition: dict[str, tuple[str, str]] = {}
    symbol_tools: list[str] = []
    for tool in data_tools:
        status, req = tool_requirements(tool)
        if status == "unknown":
            disposition[tool] = ("skip", SKIP_UNKNOWN)
        elif status == "handler_only":
            disposition[tool] = ("skip", SKIP_HANDLER_ONLY)
        elif not req:
            disposition[tool] = ("market", "")
        elif "symbol" in req:
            symbol_tools.append(tool)
        else:
            disposition[tool] = ("skip", f"{SKIP_UNSATISFIABLE}:{','.join(req)}")
    for i, tool in enumerate(symbol_tools):
        over = i >= MAX_SYMBOL_TOOLS
        if over:
            disposition[tool] = ("skip", SKIP_SYMBOL_CAP)
        elif not syms:
            disposition[tool] = ("skip", SKIP_NO_SYMBOL)
        else:
            disposition[tool] = ("symbol", "")

    steps: list[_Step] = []
    for tool in data_tools:
        kind, reason = disposition[tool]
        if kind == "market":
            steps.append(_Step(tool=tool))          # 市场级：required 为空 → 必须传 {}
        elif kind == "symbol":
            for sym in syms:
                steps.append(_Step(tool=tool, symbol=sym,
                                   args=_symbol_args(tool, sym, market)))
        else:
            steps.append(_Step(tool=tool, skip=reason))
    return steps


# ---------------------------------------------------------------- 执行


async def _call(tool: str, args: dict, executor, *,
                timeout: float | None = None) -> tuple[bool, str | None, str | None, int]:
    """跑一个工具，**绝不抛**：返回 (ok, text, error, 耗时ms)。三态与 pipeline.py 一致。"""
    t0 = _time.perf_counter()
    limit = PER_TOOL_TIMEOUT if timeout is None else max(0.0, float(timeout))
    try:
        out = await asyncio.wait_for(executor(tool, dict(args or {})), timeout=limit)
    except asyncio.TimeoutError:
        return False, None, "timeout", _ms(t0)
    except Exception as exc:  # noqa: BLE001 - 单个工具炸了不该毁整条管线
        return False, None, type(exc).__name__, _ms(t0)
    text = str(out or "").strip()
    if not text:
        # 回了个空串：这是"没内容"，不是"没有数据"（口径不同，轨迹里分开记）。
        return False, None, "empty", _ms(t0)
    # 有响应 ≠ 有证据：拒权/取不到按失败记 —— 但只认**声明**（首行前缀/JSON 字段），
    # 不认正文里出现过什么字（2026-11-10 复审阻断）。
    status = tool_status(text)
    if status != "ok":
        return False, None, status, _ms(t0)
    return True, text, None, _ms(t0)


def _budget() -> float:
    """本轮墙钟预算（秒）：env 优先，非法值退回常量。"""
    raw = os.getenv("PIPELINE_MARKET_BUDGET")
    if raw is None or str(raw).strip() == "":
        return PIPELINE_BUDGET
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("PIPELINE_MARKET_BUDGET=%r 非法，退回 %s", raw, PIPELINE_BUDGET)
        return PIPELINE_BUDGET


async def _exec_steps(steps: list[_Step], executor, market: str, trace=None,
                      on_start=None) -> MarketRun:
    """有界并发执行步骤表，返回账本（永不抛）。"""
    slots: list[tuple[bool, str | None, str | None, int] | None] = [None] * len(steps)
    sem = asyncio.Semaphore(_concurrency())
    heavy_lock = asyncio.Lock()
    deadline = _time.perf_counter() + _budget()

    async def _go(idx: int, step: _Step) -> None:
        if on_start is not None:
            try:
                on_start(step)      # 进度旁路：报不出去也不影响取数
            except Exception:  # noqa: BLE001
                pass
        remain = deadline - _time.perf_counter()
        if remain <= 0:             # 墙钟预算用完：不再发请求，别让用户等到 8 分钟
            slots[idx] = (False, None, "budget_exhausted", 0)
            return
        slots[idx] = await _call(step.tool, step.args, executor,
                                 timeout=min(PER_TOOL_TIMEOUT, remain))

    async def _one(idx: int, step: _Step) -> None:
        try:
            if _is_heavy_tool(step.tool):
                async with heavy_lock:   # 全市场扫描/刷新类：一次只放一个进去
                    async with sem:
                        await _go(idx, step)
            else:
                async with sem:
                    await _go(idx, step)
        except Exception as exc:  # noqa: BLE001 - 调度层出问题也只算这一条失败
            slots[idx] = (False, None, type(exc).__name__, 0)

    pending = [(i, s) for i, s in enumerate(steps) if not s.skip]
    if pending:
        await asyncio.gather(*(_one(i, s) for i, s in pending), return_exceptions=True)

    run = MarketRun(market=market)
    # 统一按步骤顺序结算 + 记轨迹：账目与执行先后无关，便于回归比对
    for idx, step in enumerate(steps):
        if step.skip:
            run.skipped.append(step.label)
            run.errors.append(f"{step.label}:{step.skip}")
            _record(trace, step.tool, step.symbol, ok=False, error=step.skip, ms=0,
                    args=step.args)
            continue
        ok, text, err, ms = slots[idx] or (False, None, "unknown", 0)
        if ok and text is not None:
            # 昨日榜不许认今日（2026-11-10 复审 P0）：但判据是工具的**声明数据日**，
            # 不是正文里最晚的日期。声明为更早日期 → 只能算"非今日"，不进今日证据。
            state, day = declared_data_day(text)
            if state == "not_today" and day:
                run.stale.append((step.label, day))
                run.failed += 1
                run.errors.append(f"{step.label}:stale_data:{day}")
                _record(trace, step.tool, step.symbol, ok=False,
                        error=f"stale_data:{day}", ms=ms, args=step.args)
                continue
            run.outputs.append((step.label, text))
            run.ok += 1
            if day:
                run.dates[step.label] = day
            else:
                # 没有声明就不要假装知道：时效"未知"必须写进账本，结论里不许称今日。
                run.undated.append(step.label)
            _record(trace, step.tool, step.symbol, ok=True, ms=ms, result=text, args=step.args)
            continue
        run.failed += 1
        if err == "denied":
            run.denied.append(step.label)
        elif err in ("unavailable", "empty"):
            run.unavailable.append(step.label)
        if err != "unavailable":
            run.errors.append(f"{step.label}:{err}")
        _record(trace, step.tool, step.symbol, ok=False, error=err, ms=ms, args=step.args)
    return run


async def run_market_pipeline(recipe, executor, *, market: str = "CN", trace=None,
                              watchlist=None) -> MarketRun | None:
    """全市场取数。返回 None = 一个维度都没取到，调用方回落到多轮 tool loop。

    `executor(name, args) -> str` 由调用方注入（与 `pipeline.run` 同一个），
    这样本模块不必反向依赖 web 层，也不碰 `chat.py`。
    """
    steps = build_steps(recipe, watchlist, market)
    run = await _exec_steps(steps, executor, market, trace=trace)
    if not run.usable:
        logger.warning("全市场管线一个维度都没取到，回落 tool loop：%s", run.errors[:3])
        return None
    return run


async def run_market_pipeline_stream(recipe, executor, *, market: str = "CN", trace=None,
                                     watchlist=None):
    """流式取数：边跑边报进度，最后产出 ("run", MarketRun | None)。

    模型首字要 20s+，取数阶段一片空白最伤体感；进度用队列旁路，工具真开跑时投一条，
    不会因为并发而漏报或乱报数量（与 `pipeline.run_stream` 同一手法）。
    """
    steps = build_steps(recipe, watchlist, market)
    total = sum(1 for s in steps if not s.skip)
    done = 0
    q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_exec_steps(steps, executor, market, trace=trace,
                                           on_start=q.put_nowait))
    task.add_done_callback(lambda _t, _q=q: _q.put_nowait(None))   # 结束哨兵
    run: MarketRun | None = None
    try:
        while True:
            step = await q.get()
            if step is None:
                break
            done += 1
            yield "stage", f"正在取数 {done}/{total}：{step.label}"
    finally:
        if not task.done():      # 调用方提前关闭生成器：别把后台取数漏在运行里
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    if task.cancelled():
        return
    run = task.result()
    if not run.usable:
        logger.warning("全市场管线一个维度都没取到，回落 tool loop：%s", run.errors[:3])
        run = None
    yield "run", run


# ---------------------------------------------------------------- 结论提示词


def _fact_pack_header(fact_pack: dict | None) -> str:
    """冻结事实包的前缀（与 `pipeline.compose_prompt` 逐字同口径）。

    只读 `as_of` / `source` / `text`：事实包冻结点不是各工具数据的基准日，除这句
    约束外不掺别的东西；包里 `symbols` 的标的价格属于父 run 的单标的快照，
    全市场结论引用它就是把「某只票的行情」冒充「盘面数据」。
    """
    if not fact_pack or not isinstance(fact_pack, dict):
        return ""
    return (
        f"--- 本轮事实包（冻结于 {fact_pack.get('as_of', '')}，"
        f"来源 {fact_pack.get('source', '')}）---\n"
        f"{fact_pack.get('text', '')}\n任何结论不得引用晚于该时点的数据；缺项写「暂无」\n\n"
    )


def compose_market_prompt(recipe, run: MarketRun, question: str,
                          context: str = "", fact_pack: dict | None = None) -> tuple[str, str]:
    """把全市场证据拼成 (system, user)。

    规则文本与 `pipeline.compose_prompt` 同口径（不许补、不许算、缺维度写「暂无」），
    额外多一条：本轮**明确没跑**的维度（没自选 / 参数填不上）必须在「口径」里写「未取到」，
    并写出原因 —— 否则模型会把「没查」写成「没有」。

    `fact_pack` 非空时把冻结锚点放到 system 最前面（与符号管线同一位置、同一措辞），
    委派场景下向模型提示父 run 的冻结时点；这不是硬性时点校验，工具可能返回更晚的数据。
    """
    sections = " / ".join(recipe.output_sections) if recipe.output_sections else ""
    rule = (
        "\n\n以下是本轮**已经取到**的全部数据（由系统按固定顺序调用全市场工具，你没有调用工具的能力）。"
        "请只依据这些数据写结论：每个数字必须能在下面的证据里找到原文，缺失的维度写「暂无」，"
        "不要补充任何未列出的数据，也不要推测。"
    )
    note = run.missing_note()
    if note:
        rule += (
            f"\n\n【本轮缺口（系统判定，必须写进口径段）】{note}。"
            "拒权/取不到/非今日/数据日未声明都算**没取到**：不许写成「没有」「为零」，"
            "也不许拿别的日期或常识补齐。"
        )
    if run.undated:
        rule += (
            f"\n以下维度**没有声明数据日**（时效未知）：{', '.join(run.undated[:6])}。"
            "引用它们时只能说「取数时间未声明」，**禁止**写成「今天」「今日」「实时」，"
            "也禁止据此判断新鲜度。"
        )
    if run.stale:
        days = ", ".join(sorted({d for _, d in run.stale}))
        rule += (
            f"\n以下维度的数据日不是今天（{days}），**禁止当作今日行情**："
            f"{', '.join(label for label, _ in run.stale)}。"
            "引用时必须写明数据日期，结论里不得出现「今天」「今日」指代它们。"
        )
    if run.denied:
        rule += (
            f"\n以下维度是**无权限/未开通**（不是没有数据）：{', '.join(run.denied)}；"
            "口径段要写「未取到（数据源未授权）」。"
        )
    if run.candidates_status == "unknown":
        rule += (
            "\n本轮**没有任何工具给出结构化候选名单**：候选段落只许照抄证据里出现过的"
            "代码与出处，并写明「名单未结构化声明，覆盖与时效未知」；"
            "不许断言「今天没有机会」，也不许把热度/情绪描述当候选标的。"
        )
    elif run.no_candidates:
        rule += (
            "\n本轮工具**结构化声明了空名单**（不是没有机会）：候选段落写「数据源未返回候选」，"
            "不许用热度/情绪补齐。"
        )
    if run.skipped:
        rule += (
            f"\n以下维度本轮**没有取到**：{', '.join(run.skipped)}。"
            "它们必须在结论里写成「未取到（原因：无标的可查 / 参数不适用）」，"
            "不许用常识或印象补齐，也不许据此断言「没有」。"
        )
    system = (recipe.prompt_fragment or "") + rule + (f"\n输出段落：{sections}" if sections else "") + '\n\n【通用排版铁律 —— 禁止教科书式长段落】\n1) 第一段必须先给一句话结论（≤45 字，带方向/状态 + 两个关键数字）。\n2) 每个段落下的内容一律写「- 」要点列表或 GFM 表格；每条要点独立成行、≤50 字、以数字或标的开头。\n3) 任何地方禁止出现超过 3 行的连续散文段落；禁止使用「从盘面看/整体而言/值得注意的是/综上所述」等套话。\n4) 段落标题单独成行（用【段落名】包起来），不许用 markdown `#` 标题。'
    system = _fact_pack_header(fact_pack) + system
    if context:
        system += "\n\n--- 会话上下文（用户本人 / 当前页面 / 持仓与偏好）---\n" + context

    head = f"【用户问题】{question}"
    blocks = [head,
              f"\n===== 全市场（{run.market}）：成功 {run.ok} / 失败 {run.failed} / "
              f"未跑 {len(run.skipped)} / 拒权 {len(run.denied)} / "
              f"未取到 {len(run.unavailable)} / 非今日 {len(run.stale)} / "
              f"未声明数据日 {len(run.undated)} ====="]
    if run.data_date and run.data_date_status == "verified":
        blocks.append(f"（工具声明的数据日：{run.data_date}）")
    elif run.data_date:
        # 只有一部分维度声明了这个数据日：不许用它代表整轮（复审：缺结构化证据
        # 维持未知，不许把"部分已验证"写成"整轮今日"）。
        blocks.append(
            f"（部分工具声明的数据日：{run.data_date}；另有 {len(run.undated)} 项未声明，"
            "不得据此把整轮称今日/实时）")
    else:
        blocks.append("（数据日：未声明 —— 不得称今日/实时）")
    if run.skipped:
        blocks.append(f"（本轮未跑：{', '.join(run.skipped)}）")
    if run.stale:
        blocks.append("（非今日数据，不许当今日："
                      + ", ".join(f"{label}@{day}" for label, day in run.stale) + "）")
    used = 0
    for label, text in run.outputs:
        body = text if len(text) <= PER_TOOL_CHARS else text[:PER_TOOL_CHARS] + "\n…（后略）"
        if used + len(body) > TOTAL_EVIDENCE_CHARS:
            blocks.append(f"\n--- {label} ---\n（证据总量已达上限，此处省略；该维度在结论里写「暂无」）")
            continue
        used += len(body)
        blocks.append(f"\n--- {label} ---\n{body}")
    if not run.outputs:
        blocks.append("（全部工具取数失败：请在结论里明确写「数据未取到」，不要推断）")
    return system, "\n".join(blocks)


def _missing_prefix(run: MarketRun) -> str:
    """确定性缺口前缀：缺口不靠模型自觉写，由代码写在正文最前面（2026-11-10 复审 P0）。"""
    note = run.missing_note()
    if not note:
        return ""
    return f"> 数据口径：{note}\n\n"


def _audit(text: str, run: MarketRun, system: str, question: str) -> str:
    """数字对账：证据是确定的，对账最有意义（模型只该抄，不该算）。失败就原样返回。"""
    try:
        from src.core.number_audit import annotate as _ann, audit as _aud

        evidence = "\n".join(t for _, t in run.outputs) + "\n" + system
        return _ann(text, _aud(text, evidence, question))
    except Exception:  # noqa: BLE001 - 对账是增强, 不许因此把回答搞挂
        return text


# ---------------------------------------------------------------- 作答


async def answer_market(ai_client, recipe, executor, question: str, *, market: str = "CN",
                        trace=None, watchlist=None, context: str = "",
                        fact_pack: dict | None = None,
                        evidence_sink: list | None = None) -> str | None:
    """非流式全市场作答。返回 None = 没跑成，调用方回落多轮 loop。

    `fact_pack` / `evidence_sink` 与符号管线同名同义（见模块 docstring 的透传口径）：
    冻结锚点进提示词，证据旁路只在**真的产出了结论**时写。
    """
    run = await run_market_pipeline(recipe, executor, market=market, trace=trace,
                                    watchlist=watchlist)
    if run is None:
        return None
    system, user_block = compose_market_prompt(recipe, run, question, context, fact_pack)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_block}]
    try:
        if hasattr(ai_client, "chat_with_tools"):
            final = await ai_client.chat_with_tools(msgs, tools=[], temperature=0.2)
            text = (getattr(final, "content", "") or "").strip()
        else:
            text = (await ai_client.chat(system, user_block, temperature=0.2) or "").strip()
    except Exception as exc:  # noqa: BLE001 - 写结论失败就当没加成，回落 loop
        logger.warning("全市场管线写结论失败，回落 tool loop：%s", type(exc).__name__)
        return None
    if not text:
        return None
    audited = _missing_prefix(run) + _audit(text, run, system, question)
    # 上面两条 `return None`（取数全失败 / 写结论失败）都在这之前，回落路径不留证据。
    _emit_evidence([run], trace, fact_pack, evidence_sink)
    return audited


async def answer_market_stream(ai_client, recipe, executor, question: str, *, market: str = "CN",
                               trace=None, watchlist=None, context: str = "",
                               fact_pack: dict | None = None,
                               evidence_sink: list | None = None):
    """流式全市场作答：("stage", 进度) → ("delta", 增量) → ("done", 全文)。

    一个维度都没取到 → 直接 return（不产出 done），调用方据此回落多轮 loop。

    证据旁路在 `("done", ...)` **之前**落盘：调用方读 sink 拼 done 载荷，晚一步就是
    「正文到了、证据没到」。只 `extend` 传入/ContextVar 里的 list，不 `set` ContextVar
    —— 生成器在调用方 context 里跑，`set` 会漏给外层请求。
    """
    run: MarketRun | None = None
    async for kind, payload in run_market_pipeline_stream(
        recipe, executor, market=market, trace=trace, watchlist=watchlist
    ):
        if kind == "stage":
            yield "stage", payload
        else:
            run = payload if isinstance(payload, MarketRun) else None
    if run is None:
        return
    prefix = _missing_prefix(run)
    if prefix:                 # 缺口先于正文出站：读第一段就知道哪些没取到
        yield "delta", prefix
    system, user_block = compose_market_prompt(recipe, run, question, context, fact_pack)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_block}]
    acc: list[str] = [prefix] if prefix else []
    final = None
    try:
        async for kind, payload in ai_client.chat_with_tools_stream(msgs, tools=[], temperature=0.2):
            if kind == "delta":
                acc.append(payload)
                yield "delta", payload
            else:
                final = payload
    except Exception as exc:  # noqa: BLE001 - 流断了就用已收到的部分
        logger.warning("全市场管线流式写结论中断：%s", type(exc).__name__)
    text = "".join(acc).strip() or (getattr(final, "content", "") or "").strip()
    if not text:
        return
    audited = _audit(text, run, system, question)
    if len(audited) > len(text):    # 对账附言作为增量补发，不重复正文
        yield "delta", audited[len(text):]
        text = audited
    _emit_evidence([run], trace, fact_pack, evidence_sink)
    yield "done", text
