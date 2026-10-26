"""分析维度缺口采集（确定性后置解析，2026-10-22 第三次改道）。

**为什么不用工具调用**（三次真机验收的结论，别再把这条路捡回来）：

| 版本 | 做法 | 结果 |
|---|---|---|
| v0.43.0 | `note_new_dimension` 挂进配方，模型可见 | 工具被调用，但只传 `symbol` |
| v0.43.2 | 让记录类工具不吃取数预算（以为没余量重试） | 同样只传 `symbol` |
| v0.43.3 | 提示词加字面调用示例 | 同样只传 `symbol` |

隔离实验把根因钉死了：`agnes-3.0-flash` 在**连续 10 个只吃 `symbol` 的工具**之后，
对第 11 个工具会无视 schema 直接照抄 `{"symbol": ...}` —— 9 次实验 6 次如此，
**把 `symbol` 从 schema 里删掉它照样吐**。工具调用这条路在这个模型上不可靠，
不是提示词问题，调参救不回来。

**改道：回答生成完之后，用一次「不带任何工具」的独立调用做缺口判定。**
这正是最早探针里 3/3 全对的条件（单工具、无工具序列污染）。

红线：
- **只在深度诊断 + 回答里出现「未取到」类标记时才发这次调用** —— 不给每轮问答加成本；
- 输出必须是 JSON 数组，逐条校验（2~40 字、最多 3 条），校验不过就**整条丢弃**，
  **绝不猜、绝不补**；
- 采集全程旁路：任何异常都吞掉记日志，绝不影响本轮作答；
- 只写 `harness_proposals`（人工审核队列），**配方一个字都不改**。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 只有深度诊断会产出「六层段落 + 未取到」这种结构，其它配方不做缺口判定。
WATCH_RECIPES = ("deep_diagnosis",)

# 回答里出现这些词 = 本轮确实有维度没取到。命中任意一个才值得发一次判定调用。
MISS_MARKERS = ("未取到", "暂无", "无法获取", "没有这块数据", "未覆盖", "数据缺失", "取不到")

MIN_DIM_LEN = 2
MAX_DIM_LEN = 40
MAX_GAPS = 3

SYSTEM_PROMPT = (
    "你是数据缺口审核员。给你一段用户的提问和一段分析回答，"
    "判断用户明确想看、但这轮分析里确实没给出数据的分析维度。\n"
    "严格规则：\n"
    "1. 只输出 JSON 数组，不要任何解释、不要 markdown 代码块；\n"
    "2. 每个元素形如 {\"dimension\": \"股东户数变化\", \"reason\": \"只有十大股东名单，没有户数变化趋势\"}；\n"
    "3. dimension 必须是 2~40 字的名词短语，取自用户真的问过的内容，不要发明；\n"
    "4. 回答里确实给了数据维度不要列；最多 3 条；没有缺口就输出 []。"
)

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)
_WS = re.compile(r"\s+")


def should_check(recipe: str | None, answer: str) -> bool:
    """要不要为这一轮做缺口判定。

    - 只认深度诊断（其它配方的「暂无」含义不同，误判率高）；
    - 回答里必须出现「未取到」类标记 —— 没标记说明这轮没缺口，不值得多发一次调用。
    """
    if not recipe or recipe not in WATCH_RECIPES:
        return False
    text = answer or ""
    return any(m in text for m in MISS_MARKERS)


def build_user_content(question: str, answer: str, *, max_answer: int = 2500) -> str:
    """拼判定输入。回答截断到 max_answer 字（六层结构关键信息都在前半段）。"""
    q = (question or "").strip()
    a = (answer or "").strip()
    if len(a) > max_answer:
        a = a[:max_answer] + "…"
    return f"【用户提问】\n{q}\n\n【本轮分析回答】\n{a}"


def parse_gaps(raw: str) -> list[dict[str, str]]:
    """从模型输出里解析缺口数组。**校验不过就丢，不做任何修补。**

    容忍模型多写几句解释（先抓第一个 JSON 数组），但元素必须严格合规：
    必须是 dict、dimension 是 2~40 字的字符串、reason 是字符串（可空）。
    """
    text = (raw or "").strip()
    if not text:
        return []
    m = _JSON_ARRAY.search(text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in data:
        if len(out) >= MAX_GAPS:
            break
        if not isinstance(item, dict):
            continue
        dim = str(item.get("dimension") or "").strip()
        if not (MIN_DIM_LEN <= len(dim) <= MAX_DIM_LEN):
            continue
        key = _WS.sub("", dim).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"dimension": dim, "reason": str(item.get("reason") or "").strip()[:500]})
    return out


async def collect(
    ai_client: Any,
    db: Any,
    *,
    question: str,
    answer: str,
    user_id: Any,
    conversation_id: int | None = None,
    symbol: str | None = None,
    recipe: str | None = None,
) -> list[dict]:
    """判定并记录本轮的分析维度缺口。**旁路：任何失败都只是少一条记录。**

    返回本次新记录/累加的提案列表（便于测试与轨迹）。
    """
    from src.core.harness_proposals import propose

    if not should_check(recipe, answer):
        return []

    try:
        raw = await ai_client.chat(
            SYSTEM_PROMPT,
            build_user_content(question, answer),
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - 采集是旁路, 判定失败不影响作答
        logger.info("[gap-collector] 缺口判定调用失败(忽略): %s", exc)
        return []

    gaps = parse_gaps(raw)
    if not gaps:
        return []

    saved: list[dict] = []
    for g in gaps:
        try:
            row = propose(
                db,
                user_id,
                dimension=g["dimension"],
                reason=g["reason"] or "用户在提问里要求了这个维度，本轮分析未给出数据。",
                evidence=(answer or "")[:1500],
                conversation_id=conversation_id,
                symbol=symbol,
                recipe=recipe,
            )
            saved.append(row)
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖累其余
            logger.info("[gap-collector] 提案入库失败(忽略): %s", exc)
    return saved


def spawn(
    ai_client: Any,
    *,
    question: str | None,
    answer: str,
    user_id: Any,
    conversation_id: int | None = None,
    symbol: str | None = None,
    recipe: str | None = None,
) -> None:
    """把缺口判定丢到后台跑（fire-and-forget），**不占本轮响应时间**。

    自己开一条 session：请求作用域的 `db` 在响应返回后就关了，
    在后台任务里复用它会在收尾时炸。判定不需要请求上下文，独立 session 最干净。
    """
    if not should_check(recipe, answer):
        return

    async def _run() -> None:
        try:
            from src.db.session import SessionLocal

            with SessionLocal() as db:
                await collect(
                    ai_client, db,
                    question=question or "", answer=answer, user_id=user_id,
                    conversation_id=conversation_id, symbol=symbol, recipe=recipe,
                )
        except Exception as exc:  # noqa: BLE001 - 采集是旁路, 失败只记日志
            logger.info("[gap-collector] 后台采集失败(忽略): %s", exc)

    try:
        asyncio.create_task(_run())
    except RuntimeError:  # 没有事件循环(同步调用/已关闭) → 直接放弃
        logger.info("[gap-collector] 无事件循环，跳过本轮缺口判定")
