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
# 生产上最常见的假证据是拒权/未开通/空库这类**有响应的失败**：旧实现只看
# `text.strip()` 非空就记 ok，于是「无权限访问」被当成一条证据写进结论，
# 模型据此答「今天没机会」——比没有数据更坏。
DENY_MARKERS = (
    "无权限", "权限不足", "未授权", "拒绝访问", "未开通", "请先开通", "套餐不包含",
    "请先登录", "登录后查看", "unauthorized", "forbidden", "access denied",
    "not authorized", "permission denied",
)
EMPTY_MARKERS = (
    "暂无数据", "无数据", "没有数据", "查询结果为空", "数据不存在",
    "empty result", "no data",
)
# 只有**开头**这么长的正文才按"整条都是拒权/空"处理：长证据正文里偶尔出现
# 「暂无数据」是某个字段的口径说明，不能整条丢掉。
_VERDICT_HEAD = 300

# 榜单/行情类证据里的日期。昨日榜认今日是 2026-11-10 复审点名的第二个 P0：
# 「昨日龙虎榜」写进今天的建仓结论，用户看到的是已经走完的行情。
_DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")


def _verdict(text: str) -> str:
    """把工具返回分成 ok / denied / empty 三态（只读开头，避免误伤长证据）。"""
    head = (text or "").strip()[:_VERDICT_HEAD].lower()
    if any(m in head for m in DENY_MARKERS):
        return "denied"
    if any(m in head for m in EMPTY_MARKERS):
        return "empty"
    return "ok"


def payload_date(text: str) -> _date | None:
    """证据正文里出现的**最晚**日期；没有日期返回 None（不猜）。"""
    latest = None
    for y, m, d in _DATE_RE.findall(text or ""):
        try:
            cur = _date(int(y), int(m), int(d))
        except ValueError:
            continue
        if latest is None or cur > latest:
            latest = cur
    return latest


@dataclass
class MarketRun:
    """一次全市场取数的账本。`outputs` 的标签带 `@代码`（同一工具可能跑多只）。"""

    market: str = "CN"
    outputs: list[tuple[str, str]] = field(default_factory=list)
    ok: int = 0
    failed: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 拒权 / 空库分开记：它们不是"工具挂了"，而是"我们没有这个数据权限"，
    # 结论里必须写清是哪种缺口（治理页也要能按原因聚合）。
    denied: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    # (标签, 数据日)：取到的是更早日期的数据，只能当"昨日"，不许当今日。
    stale: list[tuple[str, str]] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.ok > 0

    @property
    def data_date(self) -> str | None:
        """本轮所有证据里最晚的数据日期（ISO）；一条都没日期就不给。"""
        dates = [payload_date(t) for _, t in self.outputs]
        dates = [d for d in dates if d is not None]
        return max(dates).isoformat() if dates else None

    @property
    def candidate_codes(self) -> list[str]:
        """证据里出现的 6 位代码（去重保序）——「有候选」是数据事实，不是措辞。"""
        out: list[str] = []
        for _, text in self.outputs:
            for code in re.findall(r"(?<!\d)((?:00|30|60|68|83|87|43)\d{4})(?!\d)", str(text)):
                if code not in out:
                    out.append(code)
        return out

    @property
    def no_candidates(self) -> bool:
        return bool(self.outputs) and not self.candidate_codes

    def missing_note(self) -> str:
        """一句结构化的缺口说明（进提示词，也进确定性前缀）。"""
        parts = []
        if self.denied:
            parts.append(f"拒权 {len(self.denied)} 项（{', '.join(self.denied[:3])}）")
        if self.empty:
            parts.append(f"空数据 {len(self.empty)} 项（{', '.join(self.empty[:3])}）")
        if self.stale:
            days = sorted({d for _, d in self.stale})
            parts.append(f"非今日数据 {len(self.stale)} 项（数据日 {', '.join(days)}）")
        if self.skipped:
            parts.append(f"未跑 {len(self.skipped)} 项")
        if self.no_candidates:
            parts.append("未取到任何候选标的（不是「没有机会」）")
        if not parts:
            return ""
        return "本轮缺数据：" + "；".join(parts)


def evidence_payload(run: MarketRun) -> dict:
    """出站证据的缺口摘要（与 `evidence_pack` 的 ok/missing 互补，不替代它）。"""
    return {
        "market": run.market,
        "ok": run.ok,
        "failed": run.failed,
        "skipped": list(run.skipped),
        "denied": list(run.denied),
        "empty": list(run.empty),
        "stale": [[label, day] for label, day in run.stale],
        "data_date": run.data_date,
        "no_candidates": run.no_candidates,
        "missing_note": run.missing_note(),
    }


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
        return False, None, "empty", _ms(t0)
    # 有响应 ≠ 有证据：拒权/未开通/空库整条按失败记（2026-11-10 复审 P0）。
    verdict = _verdict(text)
    if verdict != "ok":
        return False, None, verdict, _ms(t0)
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
    today = _date.today()
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
            # 昨日榜不许认今日（2026-11-10 复审 P0）：正文自带的最晚日期早于今天，
            # 这条证据只能算「数据日 X」，不能进今天的证据块。
            d = payload_date(text)
            if d is not None and d < today:
                day = d.isoformat()
                run.stale.append((step.label, day))
                run.failed += 1
                run.errors.append(f"{step.label}:stale_data:{day}")
                _record(trace, step.tool, step.symbol, ok=False,
                        error=f"stale_data:{day}", ms=ms, args=step.args)
                continue
            run.outputs.append((step.label, text))
            run.ok += 1
            _record(trace, step.tool, step.symbol, ok=True, ms=ms, result=text, args=step.args)
            continue
        run.failed += 1
        if err == "denied":
            run.denied.append(step.label)
        elif err == "empty":
            run.empty.append(step.label)
        if err != "empty":
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
            "拒权/空数据/非今日数据都算**没取到**：不许写成「没有」「为零」，也不许拿别的日期或常识补齐。"
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
    if run.no_candidates:
        rule += (
            "\n本轮证据里**没有任何候选代码**：候选段落必须写「未取到候选（数据未覆盖）」，"
            "不许把热度/情绪描述当成候选标的，更不许据此断言「今天没有机会」。"
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
              f"未跑 {len(run.skipped)} / 拒权 {len(run.denied)} / 空 {len(run.empty)} / "
              f"非今日 {len(run.stale)} ====="]
    if run.data_date:
        blocks.append(f"（证据中最晚数据日：{run.data_date}）")
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
