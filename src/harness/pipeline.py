"""确定性管线（2026-10-21）。

问题：配方的 `tools` 原来只是"允许用哪些"，**用哪几个、按什么顺序**仍由模型决定。
实测同一问题三次：全景诊断 5/7/5 个工具、撤单 2/3/0 个 —— Jaccard 0.57。

做法：`pipeline: true` 的配方命中后，harness 自己按 `tools` 顺序调用（限时、失败可跳），
把结果拼成证据块，再让模型**一次**写成固定段落。模型不碰工具选择，Jaccard 必为 1.0。

并发（2026-10-21 第二批）：原来 `for tool in recipe.tools` 完全串行，deep_diagnosis 10 个工具、
left_diagnosis 12 个工具，单工具 1~2.5s，一轮 15~30s，占 41s p50 的大头。现在改成**单标的内有界并发**
（`PIPELINE_CONCURRENCY`，默认 3）：输出顺序、失败语义、trace 记录与串行时完全一致，只是墙钟时间落到
"ceil(M/P) × 单工具耗时"。

两条例外：
1. **标的一间仍串行**——多标的 × 多工具全打出去会成倍放大下游压力，收益也远不如单标的内部并发。
2. **TQ / 客户端类工具单独走串行 lane**（见 `_TQ_PREFIXES`）：TQ 是单线程进程，行情要走 17709 端口，
   连打会把它打崩（`TdxW.exe` 挂掉后整轮全是超时），所以这类工具先进先出、一次一个。

失败语义：所有工具都失败 → 返回 ok=0，调用方回落到原来的多轮 tool loop（不至于因为管线挂了就没答案）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time as _time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PER_TOOL_TIMEOUT = 60.0

# 单标的内部工具并发度。3 是实测折中：再高对腾讯/本地缓存的边际收益很小，反而拉高尾延迟。
# env 可覆盖（`PIPELINE_CONCURRENCY=1` 即退回串行，出事时当总开关用）。
PIPELINE_CONCURRENCY = 3

# 必须串行的工具前缀。TQ 客户端是单线程进程、行情走 17709 端口，突发并发会打崩 TdxW.exe。
# 竞价类（get_auction_data / scan_auction_anomaly）同样读 TQ 快照，一并串行。
_TQ_PREFIXES = ("get_thsdk_", "tdx_", "get_auction_data", "scan_auction_anomaly")
# 证据裁剪：6 个工具原文能到 2 万字，喂给模型只会让它更慢（实测某次 141s 里工具只占 8.6s，
# 其余全在生成）。保留数字与结论行，砍掉长列表尾部。
PER_TOOL_CHARS = 2500
TOTAL_EVIDENCE_CHARS = 12000
MAX_SYMBOLS = 2


def _ms(t0: float) -> int:
    return int((_time.perf_counter() - t0) * 1000)


def _record(trace, tool: str, symbol: str, *, ok: bool, ms: int,
            error: str | None = None, result: str | None = None) -> None:
    """管线里的工具调用也要进轨迹：否则治理页看不到，Jaccard 也没法从库里量。"""
    if trace is None:
        return
    try:
        trace.step(tool, {"symbol": symbol}, ok=ok, error=error, result=result, elapsed_ms=ms)
    except Exception:  # noqa: BLE001 - 轨迹是旁路
        pass
_CODE_RE = re.compile(r"(?<!\d)((?:00|30|60|68|83|87|43)\d{4})(?!\d)")


@dataclass
class PipelineRun:
    symbol: str = ""
    outputs: list[tuple[str, str]] = field(default_factory=list)
    ok: int = 0
    failed: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.ok > 0


def _names_to_codes(text: str) -> list[str]:
    """没有 6 位代码时，用 TQ 名称检索兜底（"全景诊断 三花智控" 这种只写名字的提问）。"""
    try:
        from src.core.symbol_guard import resolve_mentions

        info = resolve_mentions(text or "")
        return [code.split(".")[0] for _, code, _ in (info.get("resolved") or [])]
    except Exception:  # noqa: BLE001 - 解析失败就当解析不出来
        return []


def symbols_of(text: str, bound: str | None = None, *, allow_names: bool = False) -> list[str]:
    """从问句里取标的：显式代码优先，其次会话锁定；最多 2 只（多只就各跑一遍）。"""
    codes = []
    for m in _CODE_RE.finditer(text or ""):
        c = m.group(1)
        if c not in codes:
            codes.append(c)
    if codes:
        return codes[:MAX_SYMBOLS]
    if bound:
        return [bound]
    if allow_names:
        return _names_to_codes(text)[:MAX_SYMBOLS]
    return []


def _concurrency() -> int:
    """当前并发度：env 优先（运维可热调），非法值退回常量，最小 1（=串行）。"""
    raw = os.getenv("PIPELINE_CONCURRENCY")
    if raw is None or str(raw).strip() == "":
        return max(1, int(PIPELINE_CONCURRENCY))
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("PIPELINE_CONCURRENCY=%r 非法，退回 %s", raw, PIPELINE_CONCURRENCY)
        return max(1, int(PIPELINE_CONCURRENCY))


def _is_tq_tool(name) -> bool:
    return str(name or "").startswith(_TQ_PREFIXES)


async def _call_tool(tool: str, sym: str, executor, market: str) -> tuple[bool, str | None, str | None, int]:
    """跑一个工具，**绝不抛**：返回 (ok, text, error, 耗时ms)。

    失败三态与串行版逐字一致：超时 / 异常 / 空文本。任何单个工具炸了都不能影响别的工具，
    所以异常在这里就地吃掉，由调用方按顺序记成 failed。
    """
    t0 = _time.perf_counter()
    try:
        out = await asyncio.wait_for(
            executor(tool, {"symbol": sym, "market": market}),
            timeout=PER_TOOL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return False, None, "timeout", _ms(t0)
    except Exception as exc:  # noqa: BLE001 - 单个工具炸了不该毁整条管线
        return False, None, type(exc).__name__, _ms(t0)
    text = str(out or "").strip()
    if not text:
        return False, None, "empty", _ms(t0)
    return True, text, None, _ms(t0)


async def _run_symbol(recipe, sym: str, executor, market: str, trace=None, on_start=None):
    """单个标的：内部有界并发取数，返回 (PipelineRun, errors)。

    - 普通工具占 `PIPELINE_CONCURRENCY` 个信号量槽；
    - TQ 类工具先拿串行锁再拿槽（锁在外层：只有一个 TQ 在跑，不会占满槽位干等）；
    - 结果按 `recipe.tools` 下标回填，**输出顺序 == 配方顺序**，与谁先跑完无关。
    """
    tools = list(recipe.tools)
    slots: list[tuple[bool, str | None, str | None, int] | None] = [None] * len(tools)
    sem = asyncio.Semaphore(_concurrency())
    tq_lock = asyncio.Lock()

    async def _go(idx: int, tool: str) -> None:
        if on_start is not None:
            try:
                on_start(tool)  # 进度旁路，报不出去也不影响取数
            except Exception:  # noqa: BLE001
                pass
        slots[idx] = await _call_tool(tool, sym, executor, market)

    async def _one(idx: int, tool: str) -> None:
        try:
            if _is_tq_tool(tool):
                async with tq_lock:      # TQ 单线程：一次只放一个进去
                    async with sem:      # 再占并发槽，保证总峰值不超 PIPELINE_CONCURRENCY
                        await _go(idx, tool)
            else:
                async with sem:
                    await _go(idx, tool)
        except Exception as exc:  # noqa: BLE001 - 兜底：调度层出问题也只算这一个失败
            slots[idx] = (False, None, type(exc).__name__, 0)

    # return_exceptions=True：一个任务炸了不影响 gather 里的其它任务
    await asyncio.gather(*(_one(i, t) for i, t in enumerate(tools)), return_exceptions=True)

    pr = PipelineRun(symbol=sym)
    errors: list[str] = []
    # 统一在这里按配方顺序结算 + 记轨迹：账目（ok/failed/errors/trace）与串行版逐项一致
    for idx, tool in enumerate(tools):
        ok, text, err, ms = slots[idx] or (False, None, "unknown", 0)
        if ok and text is not None:
            pr.outputs.append((tool, text))
            pr.ok += 1
            _record(trace, tool, sym, ok=True, ms=ms, result=text)
            continue
        pr.failed += 1
        if err != "empty":  # 空文本照串行版：算失败但不进 errors 明细
            errors.append(f"{tool}:{err}")
        _record(trace, tool, sym, ok=False, error=err, ms=ms)
    return pr, errors


async def run(recipe, symbols: list[str], executor, db=None, user=None,
              market: str = "CN", trace=None) -> tuple[list[PipelineRun], list[str]]:
    """按配方跑工具。executor(name, args) -> str（由 chat 层注入，避免反向依赖）。

    单标的内并发（PIPELINE_CONCURRENCY），标的之间仍串行。
    """
    results: list[PipelineRun] = []
    errors: list[str] = []
    for sym in symbols:
        pr, errs = await _run_symbol(recipe, sym, executor, market, trace=trace)
        results.append(pr)
        errors.extend(errs)
    return results, errors


async def run_stream(recipe, symbols: list[str], executor, market: str = "CN",
                     trace=None):
    """流式版取数：边跑边报进度（模型首字要 20s+，中间不能一片空白）。

    产出 ("stage", 文案) 若干，最后 ("runs", (results, errors))。
    取数本身复用 `_run_symbol`（同一套并发/串行语义），进度用队列旁路：工具真开跑时投一条，
    主协程边等边 yield，不会因为并发而漏报或乱报数量。
    """
    results: list[PipelineRun] = []
    errors: list[str] = []
    total = len(symbols) * len(recipe.tools)
    done = 0
    for sym in symbols:
        q: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(
            _run_symbol(recipe, sym, executor, market, trace=trace, on_start=q.put_nowait)
        )
        task.add_done_callback(lambda _t, _q=q: _q.put_nowait(None))  # 结束哨兵
        try:
            while True:
                tool = await q.get()
                if tool is None:
                    break
                done += 1
                yield "stage", f"正在取数 {done}/{total}：{tool}"
        finally:
            if not task.done():  # 调用方提前关闭生成器：别把后台取数漏在运行里
                task.cancel()
        pr, errs = task.result()
        results.append(pr)
        errors.extend(errs)
    yield "runs", (results, errors)


def compose_prompt(recipe, runs: list[PipelineRun], question: str) -> tuple[str, str]:
    """把管线结果拼成 (system, user)。system 说明格式，user 放证据。"""
    sections = " / ".join(recipe.output_sections) if recipe.output_sections else ""
    system = (
        (recipe.prompt_fragment or "")
        + "\n\n以下是本轮**已经取到**的全部数据（由系统按固定顺序调用，你没有调用工具的能力）。"
        "请只依据这些数据写结论：每个数字必须能在下面的证据里找到原文，缺失的维度写「暂无」，"
        "不要补充任何未列出的数据，也不要推测。"
        + (f"\n输出段落：{sections}" if sections else "")
    )
    blocks = [f"【用户问题】{question}"]
    used = 0
    for pr in runs:
        blocks.append(f"\n===== 标的 {pr.symbol}（成功 {pr.ok} / 失败 {pr.failed}）=====")
        for tool, text in pr.outputs:
            body = text if len(text) <= PER_TOOL_CHARS else text[:PER_TOOL_CHARS] + "\n…（后略）"
            if used + len(body) > TOTAL_EVIDENCE_CHARS:
                blocks.append(f"\n--- {tool} ---\n（证据总量已达上限，此处省略；该维度在结论里写「暂无」）")
                continue
            used += len(body)
            blocks.append(f"\n--- {tool} ---\n{body}")
        if not pr.outputs:
            blocks.append("（该标的全部工具取数失败：请在结论里明确写「数据未取到」，不要推断）")
    return system, "\n".join(blocks)


async def answer(ai_client, recipe, question: str, bound: str | None, executor,
                 market: str = "CN", trace=None) -> str | None:
    """完整跑一条管线并写结论。返回 None = 管线没跑成，调用方回落到多轮 loop。"""
    syms = symbols_of(question, bound, allow_names=True)
    if not syms:
        logger.info("管线跳过：问题里没有可解析的标的（%s）", (question or "")[:30])
        return None
    runs, errors = await run(recipe, syms, executor, market=market, trace=trace)
    if not any(r.usable for r in runs):
        logger.warning("管线全部失败，回落 tool loop：%s", errors[:3])
        return None
    system, user_block = compose_prompt(recipe, runs, question)
    try:
        text = await ai_client.chat(system, user_block, temperature=0.2)
    except Exception as exc:  # noqa: BLE001 - 写结论失败就当没加成，回落 loop
        logger.warning("管线写结论失败，回落 tool loop：%s", type(exc).__name__)
        return None
    text = (text or "").strip()
    if not text:
        return None
    # 数字对账：管线模式下证据是确定的，对账最有意义（模型只该抄，不该算）
    try:
        from src.core.number_audit import annotate as _ann, audit as _audit

        evidence = "\n".join(t for r in runs for _, t in r.outputs)
        text = _ann(text, _audit(text, evidence, question))
    except Exception:  # noqa: BLE001
        pass
    return text


async def answer_stream(ai_client, recipe, question: str, bound: str | None, executor,
                        market: str = "CN", trace=None):
    """流式版：边生成边下发。取数 ~5s，生成 25~135s —— 不流式的话用户只能干等。

    产出 ("delta", 文本) 若干 → 最后 ("done", 全文)。管线没跑成时直接 return（调用方回落 loop）。
    """
    syms = symbols_of(question, bound, allow_names=True)
    if not syms:
        return
    runs: list[PipelineRun] = []
    errors: list[str] = []
    async for kind, payload in run_stream(recipe, syms, executor, market=market, trace=trace):
        if kind == "stage":
            yield "stage", payload
        else:
            runs, errors = payload
    if not any(r.usable for r in runs):
        logger.warning("管线全部失败，回落 tool loop：%s", errors[:3])
        return
    system, user_block = compose_prompt(recipe, runs, question)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_block}]
    acc: list[str] = []
    final = None
    try:
        async for kind, payload in ai_client.chat_with_tools_stream(msgs, tools=[], temperature=0.2):
            if kind == "delta":
                acc.append(payload)
                yield "delta", payload
            else:
                final = payload
    except Exception as exc:  # noqa: BLE001 - 流断了就用已收到的部分
        logger.warning("管线流式写结论中断：%s", type(exc).__name__)
    text = "".join(acc).strip() or (getattr(final, "content", "") or "").strip()
    if not text:
        return
    try:
        from src.core.number_audit import annotate as _ann, audit as _audit

        evidence = "\n".join(t for r in runs for _, t in r.outputs)
        audited = _ann(text, _audit(text, evidence, question))
        if len(audited) > len(text):  # 对账附言作为增量补发，不重复正文
            yield "delta", audited[len(text):]
            text = audited
    except Exception:  # noqa: BLE001
        pass
    yield "done", text
