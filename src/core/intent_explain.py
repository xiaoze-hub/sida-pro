"""主力意图 AI 解释层(规则预判 + AI 解释双层设计)。

规则(compute_dark_flow)算出主力意图全套数据与 signal 结论文本, 但有些盘口
形态难归集(拆单/对倒/托盘出货/压盘吸筹), 交 DeepSeek 给"为什么 + 置信度 + 方向"。

设计原则(用户偏好):
- 规则仍是主, AI 只做解释, **不改结论**。
- 数据不足(data_status == "insufficient")或 dark 为 None 时, 不解释(返回 None)。
- LLM 任何失败(超时/异常/非法 JSON)一律静默降级 None, 不影响算法结论。

模块分层(纯函数 + LLM 层分离, 便于单测):
1. build_explain_prompt(dark) -> tuple[system, user]: 纯函数, 无 IO。
2. parse_explain_reply(reply) -> dict | None: 纯函数, 解析并校验 LLM JSON。
3. explain_main_intent(dark, db) -> dict | None: 主入口, 串起 prompt → LLM → parse。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

logger = logging.getLogger(__name__)

# LLM 调用超时(秒), 超时 → 静默降级 None
_LLM_TIMEOUT = 8
# 温度: 解释层要求稳定、少发散
_LLM_TEMPERATURE = 0.2
# why 字段字数上限(解析时硬截断, 防止超长)
_WHY_MAX_LEN = 80

# 方向白名单(与规则口径一致, LLM 只能四选一)
_DIRECTIONS = {"吸筹", "派发", "洗盘", "中性"}
# 置信度白名单
_CONFIDENCES = {"高", "中", "低"}

_SYSTEM_PROMPT = (
    "你是A股主力意图解释器。规则算法已给出\"结论\"与结构化盘口特征, "
    "你的任务是给出一句话\"为什么\"+置信度+方向归类, 帮助用户理解算法结论。\n"
    "硬性要求:\n"
    "1. 必须结合内外盘(buy_pct/sell_pct)、拆单(split_order)、筹码(absorb_zones/"
    "distribute_zones)、位置(position)等特征综合研判, 不能只看主力净额。\n"
    "2. why 必须引用特征数字佐证(例如\"超大单+5967万但大单-8433万\"), 不能空泛。\n"
    "3. 规则结论是事实依据, 你可以补充解释, 但不得推翻或改写规则结论方向。\n"
    "4. 特征缺失的字段按\"无\"处理, 严禁编造任何数字或事件。\n"
    "5. 只输出严格 JSON, 不要任何其他文字、注释或 markdown 围栏。\n"
    '输出格式(严格 JSON): {\"direction\": \"吸筹|派发|洗盘|中性\", '
    '"confidence": "高|中|低", "why": "一句话(≤80字), 必须引用特征数字"}'
)


def _num(v):
    """安全转 float, 失败/None 返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_wan(v) -> str:
    """元 → 万(带符号), None/非法 → '无'。"""
    n = _num(v)
    if n is None:
        return "无"
    return f"{n / 1e4:+.0f}万"


def _fmt_pct(v) -> str:
    """百分比保留 1 位(带符号/裸数字), None → '无'。"""
    n = _num(v)
    if n is None:
        return "无"
    return f"{n:.1f}%"


def _str(v, maxlen: int = 80) -> str:
    """安全字符串化并截断, None/空 → ''。"""
    s = str(v or "").strip()
    return s[:maxlen]


def build_explain_prompt(dark: dict) -> tuple[str, str]:
    """从 dark(dict)提取结构化特征, 拼 system + user prompt。

    纯函数: 无 IO、无 DB、无 LLM 调用, 可单测。

    Args:
        dark: compute_dark_flow 返回的原始 dict(或同构 dict)。

    Returns:
        (system_prompt, user_content) 二元组。dark 非 dict 时降级为空特征
        (不抛异常, 主入口会在更早处拦截 None)。
    """
    d = dark if isinstance(dark, dict) else {}

    # ---- 核心资金字段(元 → 万) ----
    main_net = d.get("main_net")
    big_net = d.get("big_net")
    mid_net = d.get("mid_net")
    # 散户字段兼容两种命名: small_net(腾讯口径) 或 retail_net
    retail_net = d.get("small_net")
    if retail_net is None:
        retail_net = d.get("retail_net")

    intensity = d.get("main_intensity")
    if intensity is None:
        intensity = d.get("participation")
    buy_ratio = d.get("main_buy_ratio")
    if buy_ratio is None:
        buy_ratio = d.get("buy_ratio")

    # ---- 特征行 ----
    feat: list[str] = [
        f"主力净额={_fmt_wan(main_net)}",
        f"超大单净额={_fmt_wan(big_net)}",
        f"大单净额={_fmt_wan(mid_net)}",
        f"散户净额={_fmt_wan(retail_net)}",
        f"主力参与度={_fmt_pct(intensity)}",
        f"主力买占比={_fmt_pct(buy_ratio)}",
    ]

    # ---- 内外盘(口诀方向) ----
    io = d.get("inner_outer") or {}
    if isinstance(io, dict):
        buy_pct = io.get("buy_pct")
        sell_pct = io.get("sell_pct")
        position = io.get("position")
        io_parts = [f"内盘买占比={_fmt_pct(buy_pct)}", f"外盘卖占比={_fmt_pct(sell_pct)}"]
        if position:
            io_parts.append(f"现价位置={_str(position, 40)}")
        feat.append("内外盘: " + "/".join(io_parts))

    # ---- 规则结论 signal ----
    signal = d.get("signal")
    feat.append(f"规则结论signal={_str(signal, 200)}")

    # ---- 5 日阶段 ----
    phase = d.get("phase")
    if phase:
        feat.append(f"5日阶段={_str(phase, 80)}")

    # ---- 背离(超大单/大单背离 + 量价背离) ----
    divergence = d.get("divergence")
    if isinstance(divergence, dict) and divergence.get("type"):
        feat.append(
            f"超大单大单背离={_str(divergence.get('type'), 40)}"
            f"/{_str(divergence.get('detail'), 80)}"
        )
    price_divergence = d.get("price_divergence")
    if isinstance(price_divergence, dict) and price_divergence.get("type"):
        feat.append(
            f"量价背离={_str(price_divergence.get('type'), 40)}"
            f"/{_str(price_divergence.get('detail'), 80)}"
        )

    # ---- 时段节奏 ----
    rhythm = d.get("rhythm")
    if isinstance(rhythm, dict) and rhythm.get("pattern"):
        feat.append(
            f"时段节奏={_str(rhythm.get('pattern'), 40)}"
            f"/{_str(rhythm.get('detail'), 80)}"
        )

    # ---- 拆单识别 ----
    split_order = d.get("split_order")
    if isinstance(split_order, dict) and split_order:
        feat.append(f"拆单识别={_str(split_order.get('detail') or split_order, 120)}")
    elif split_order:
        feat.append(f"拆单识别={_str(split_order, 120)}")

    # ---- 承接/派发价位 ----
    absorb_zones = d.get("absorb_zones") or []
    distribute_zones = d.get("distribute_zones") or []
    if absorb_zones:
        az = ", ".join(f"{_fmt_wan(z.get('big_net'))}@{z.get('price')}" for z in absorb_zones[:3])
        feat.append(f"承接位(大单净买/价位)={az}")
    if distribute_zones:
        dz = ", ".join(f"{_fmt_wan(z.get('big_net'))}@{z.get('price')}" for z in distribute_zones[:3])
        feat.append(f"派发位(大单净卖/价位)={dz}")

    # ---- 数据状态(供 AI 感知数据充分性, 但主入口已拦截 insufficient) ----
    feat.append(f"数据状态={_str(d.get('data_status'), 40) or '无'}")

    user = "规则结论与结构化盘口特征如下:\n" + "\n".join(f"- {f}" for f in feat) + \
        "\n\n请输出严格 JSON(方向/置信度/why 一句话为什么)。"

    return _SYSTEM_PROMPT, user


def parse_explain_reply(reply: str | None) -> dict | None:
    """解析并校验 LLM 返回的 JSON。非法/空/字段不合法 → None。

    纯函数: 无 IO。

    Args:
        reply: LLM 原始返回文本(可能含 ```json 围栏)。

    Returns:
        {direction, confidence, why} 或 None。
        - direction ∈ {吸筹, 派发, 洗盘, 中性}
        - confidence ∈ {高, 中, 低}
        - why: 非空, 硬截断 ≤80 字
    """
    if not reply or not reply.strip():
        return None
    text = reply.strip()
    # 容忍 markdown 围栏
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    direction = str(data.get("direction") or "").strip()
    confidence = str(data.get("confidence") or "").strip()
    why = str(data.get("why") or "").strip()

    if direction not in _DIRECTIONS:
        return None
    if confidence not in _CONFIDENCES:
        return None
    if not why:
        return None
    if len(why) > _WHY_MAX_LEN:
        why = why[:_WHY_MAX_LEN] + "…"
    return {"direction": direction, "confidence": confidence, "why": why}


def _run_coro(coro):
    """同步上下文执行异步协程: 无运行中事件循环 → asyncio.run; 有(如 FastAPI
    请求上下文)→ 新建独立事件循环执行, 避免 asyncio.run 抛
    "cannot be called from a running event loop"。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    new_loop = asyncio.new_event_loop()
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()


def _build_explain_client(db=None):
    """构造解释层 LLM 客户端: 优先 db 场景绑定(intraday_monitor/chat), 回落
    Settings/env(AI_BASE_URL/AI_API_KEY/AI_MODEL)。与 intraday_monitor 反证层
    同构, 最简可靠路径。"""
    from src.core.ai_client import AIClient

    if db is not None:
        try:
            from src.core.ai_client import get_model_for_scene
            from src.web.models import AIService

            m = get_model_for_scene(db, "intraday_monitor") or get_model_for_scene(db, "chat")
            if m is not None:
                s = db.query(AIService).filter(AIService.id == m.service_id).first()
                if s is not None and s.base_url and s.api_key:
                    return AIClient(base_url=s.base_url, api_key=s.api_key, model=m.model)
        except Exception as e:
            logger.debug(f"解释层场景绑定不可用(回落 Settings/env): {e}")
    from src.config import Settings

    settings = Settings()
    return AIClient(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
    )


async def _llm_chat(system: str, user: str, db=None) -> str:
    """单次 LLM 调用(8s 超时, wait_for 保证)。独立函数便于测试 monkeypatch
    AIClient.chat。客户端在协程内构造, 避免跨事件循环复用 httpx client。"""
    client = _build_explain_client(db)
    return await asyncio.wait_for(
        client.chat(system, user, temperature=_LLM_TEMPERATURE),
        timeout=_LLM_TIMEOUT,
    )


def explain_main_intent(dark: dict, db=None) -> dict | None:
    """主力意图 AI 解释主入口。

    规则给结论(dark 内 signal), DeepSeek 给"为什么 + 置信度 + 方向"。
    任何失败(数据不足/超时/异常/非法 JSON)一律静默返回 None, 不影响规则结论。

    Args:
        dark: compute_dark_flow 返回的原始 dict(或同构 dict)。
        db: 可选 db session(场景模型绑定; None 回落 Settings/env)。

    Returns:
        {direction, confidence, why} 或 None。
    """
    # 数据不足/空直接不解释
    if not isinstance(dark, dict):
        return None
    if dark.get("data_status") in ("insufficient", "suspect"):
        return None

    try:
        system, user = build_explain_prompt(dark)
        raw = _run_coro(_llm_chat(system, user, db))
        return parse_explain_reply(raw)
    except Exception as e:
        logger.debug(f"主力意图 AI 解释失败(静默降级, 不影响规则结论): {e}")
        return None
