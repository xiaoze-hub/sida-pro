"""参谋运行时(Agent 内核 P0, 2026-10-20)。

把「这一轮能不能调这个工具、调了几次、花了多久、结果是什么」从 chat.py 里抽出来。
现在只承担三件事, 但它们是后面所有治理能力的挂点:

1. **工具白名单** —— 配方(@盘前策略官之类)声明的工具集在这里生效, 不再是提示词里的软约束;
2. **调用预算** —— 单轮最多 N 次工具调用, 超了直接返回拒绝文案, 防止模型死循环烧钱;
3. **计时 + 轨迹** —— 每次调用落 `copilot_steps`, 让回答可回放。

P0 刻意保守: 默认白名单为空(不限制), 预算 40 次(远高于现网实测的 2~6 次), 只记录不拦截。
等 P2 评测集跑起来, 再按配方收紧。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.harness.trace import CopilotTrace

DEFAULT_MAX_TOOL_CALLS = 40

# 超预算时给模型的回执(它会拿去作答, 所以要说人话)
BUDGET_EXCEEDED = (
    "已达到本次问答的工具调用上限。请基于已经拿到的数据作答；"
    "确实还缺关键数据的维度，明确写「本次未取到」，不要推测。"
)


@dataclass
class AgentRuntime:
    """一轮问答的运行时上下文。"""

    trace: CopilotTrace | None = None
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    allowed_tools: frozenset[str] | None = None
    calls: int = 0
    denied: list[str] = field(default_factory=list)

    def allow(self, tool: str) -> tuple[bool, str]:
        """(是否放行, 拒绝原因)。白名单 = None 表示不限制。"""
        if self.calls >= self.max_tool_calls:
            return False, BUDGET_EXCEEDED
        if self.allowed_tools is not None and tool not in self.allowed_tools:
            self.denied.append(tool)
            return False, (
                f"工具 {tool} 不在当前配方允许的范围内。请用允许的工具重新取数；"
                "拿不到就直说拿不到，不要编造。"
            )
        return True, ""

    async def call(self, tool: str, args: dict, executor, **kwargs) -> str:
        """执行一次工具调用并记录轨迹。executor 统一签名为 ``(name, args, **kwargs)``。

        异常不外抛: 工具失败要以「工具返回错误文本」的形式回到模型, 由模型决定
        换工具还是如实说明, 这正是现网 `_execute_tool` 的既有契约。
        """
        ok, reason = self.allow(tool)
        if not ok:
            if self.trace is not None:
                self.trace.step(tool, args, ok=False, error=reason, result=reason, elapsed_ms=0)
            return reason

        self.calls += 1
        started = time.perf_counter()
        error: str | None = None
        try:
            result = await executor(tool, args, **kwargs)
            return result
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self.trace is not None:
                self.trace.step(
                    tool,
                    args,
                    ok=error is None,
                    error=error,
                    result=locals().get("result"),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
