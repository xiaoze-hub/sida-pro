"""确定性管线（2026-10-21）。

问题：配方的 `tools` 原来只是"允许用哪些"，**用哪几个、按什么顺序**仍由模型决定。
实测同一问题三次：全景诊断 5/7/5 个工具、撤单 2/3/0 个 —— Jaccard 0.57。

做法：`pipeline: true` 的配方命中后，harness 自己按 `tools` 顺序逐个调用（限时、失败可跳），
把结果拼成证据块，再让模型**一次**写成固定段落。模型不碰工具选择，Jaccard 必为 1.0。

失败语义：所有工具都失败 → 返回 ok=0，调用方回落到原来的多轮 tool loop（不至于因为管线挂了就没答案）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PER_TOOL_TIMEOUT = 60.0
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


async def run(recipe, symbols: list[str], executor, db=None, user=None,
              market: str = "CN", trace=None) -> tuple[list[PipelineRun], list[str]]:
    """按配方顺序跑工具。executor(name, args) -> str（由 chat 层注入，避免反向依赖）。"""
    results: list[PipelineRun] = []
    errors: list[str] = []
    for sym in symbols:
        pr = PipelineRun(symbol=sym)
        for tool in recipe.tools:
            _t0 = _time.perf_counter()
            try:
                out = await asyncio.wait_for(
                    executor(tool, {"symbol": sym, "market": market}),
                    timeout=PER_TOOL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                pr.failed += 1
                errors.append(f"{tool}:timeout")
                _record(trace, tool, sym, ok=False, error="timeout", ms=_ms(_t0))
                continue
            except Exception as exc:  # noqa: BLE001 - 单个工具炸了不该毁整条管线
                pr.failed += 1
                errors.append(f"{tool}:{type(exc).__name__}")
                _record(trace, tool, sym, ok=False, error=type(exc).__name__, ms=_ms(_t0))
                continue
            text = str(out or "").strip()
            if not text:
                pr.failed += 1
                _record(trace, tool, sym, ok=False, error="empty", ms=_ms(_t0))
                continue
            pr.outputs.append((tool, text))
            pr.ok += 1
            _record(trace, tool, sym, ok=True, ms=_ms(_t0), result=text)
        results.append(pr)
    return results, errors


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
    for pr in runs:
        blocks.append(f"\n===== 标的 {pr.symbol}（成功 {pr.ok} / 失败 {pr.failed}）=====")
        for tool, text in pr.outputs:
            blocks.append(f"\n--- {tool} ---\n{text}")
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
