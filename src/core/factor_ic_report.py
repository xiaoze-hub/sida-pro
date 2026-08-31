"""因子 IC 归因报告:把 IC/IR 喂给 LLM,输出归因解读 + 调权建议(自然语言)。

与 `factor_calibration`(自动改权重)互补:本模块只做「给人看的解释」——
哪些因子在什么市态有效/失效、惩罚因子负 IC 是否正常、给出调权建议但**不自动改权重**。

模块设计:纯函数(build/parse)与 LLM 层(generate)分离,便于单测;LLM 失败/超时/非法
JSON 一律静默返回 None(降级,不抛异常阻塞上游)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from src.core.factor_eval import FACTOR_FIELDS, evaluate_factor_ic

logger = logging.getLogger(__name__)

# 惩罚因子(IC 预期为负;负 IC = 惩罚有效,不应误判为「因子失效」)
PENALTY_FACTORS = frozenset({"risk_penalty", "crowd_penalty"})

# 单次 LLM 调用超时(秒)
_LLM_TIMEOUT = 8

# 归因评级允许值 / 置信度允许值
ASSESSMENT_VALUES = frozenset({"有效", "失效", "存疑", "样本不足"})
CONFIDENCE_VALUES = frozenset({"高", "中", "低"})

# 字段长度上限(字)
_SUMMARY_MAX = 120
_NOTE_MAX = 40
_SUGGESTION_MAX = 80


def _fmt(v) -> str:
    """数值格式化:None → 'N/A';float → 保留 4 位小数。"""
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def build_ic_report_prompt(ic_result: dict) -> tuple[str, str]:
    """把 IC/IR 结果转成 (system_prompt, user_content)。

    纯函数:不碰 IO / LLM。factors 各因子转成 markdown 表格文本,便于 LLM 解读。
    """
    factors = ic_result.get("factors", {}) or {}
    horizon = ic_result.get("horizon")
    days = ic_result.get("days")
    market = ic_result.get("market")

    # 保持 6 因子标准顺序,外带任何未预知的因子码
    ordered = list(FACTOR_FIELDS) + [k for k in factors if k not in FACTOR_FIELDS]
    lines = ["| factor_code | ic | ir | sample_size | ic_periods |", "|---|---|---|---|---|"]
    for code in ordered:
        stat = factors.get(code) or {}
        flag = "(惩罚因子, IC 预期为负)" if code in PENALTY_FACTORS else ""
        lines.append(
            f"| {code}{flag} | {_fmt(stat.get('ic'))} | {_fmt(stat.get('ir'))} "
            f"| {stat.get('sample_size', 0)} | {stat.get('ic_periods', 0)} |"
        )
    table = "\n".join(lines)

    user = (
        f"因子 IC 评估结果(持有期 horizon={horizon} 交易日, 回看 days={days} 天, "
        f"market={market}):\n{table}\n\n请输出严格 JSON 归因解读。"
    )

    system = (
        "你是 A 股多因子量化策略的因子归因分析师。你会收到 evaluate_factor_ic 算出的各因子 "
        "IC(信息系数, Spearman 秩相关, 因子值与未来收益的相关)与 IR(信息比率, 日级 IC 序列 "
        "mean/std)。\n\n"
        "任务:基于这些指标,输出「哪些因子在什么市态有效/失效」的归因解读 + 调权建议"
        "(仅供人参考,不要自动改权重)。\n\n"
        "判断规则:\n"
        "1. 真实 alpha:|ic| 较高(一般 ≥0.05)且 sample_size 足够且 IR 方向稳定 → 判「有效」。\n"
        "2. 失效:ic 接近 0(约 |ic|<0.02)且样本足够 → 判「失效」。\n"
        "3. 样本不足:sample_size 过低不足以支撑结论 → 判「样本不足」。\n"
        "4. 市态依赖:结论仅基于当前回看窗口,不可外推;若 ic/ir 方向不稳或样本集中在单一市态,"
        "需在 note 里提示,可判「存疑」。\n"
        "5. 惩罚因子(risk_penalty / crowd_penalty):IC 预期为负。负 IC = 惩罚有效(符合预期);"
        "IC 接近 0 或翻正 = 惩罚失效。切勿把负 IC 误判为「因子失效」。\n"
        "6. final_score 是合成因子,其 IC 反映整体有效性,不是独立 alpha 源。\n\n"
        "严禁编造:只依据给定数值下结论;数值缺失(N/A)或样本不足就如实写「样本不足/信息不足」。\n\n"
        "只输出严格 JSON,不要任何其他文字、不要 markdown 代码块。输出格式:\n"
        '{"summary": "≤120字总评", '
        '"factor_assessment": [{"factor_code": "alpha_score", '
        '"assessment": "有效|失效|存疑|样本不足", "note": "≤40字"}], '
        '"adjustment_suggestion": "≤80字调权建议", "confidence": "高|中|低"}\n'
        "factor_assessment 必须覆盖所有给定因子。"
    )
    return system, user


def parse_ic_report_reply(reply: str | None) -> dict | None:
    """解析 LLM 返回的 JSON,验证字段并做长度裁剪。非法/缺关键字段 → None。

    输出:{summary, factor_assessment[{factor_code, assessment, note}],
          adjustment_suggestion, confidence}
    """
    if not reply:
        return None
    text = reply.strip()

    # 容忍 ```json ... ``` 包裹;否则取第一个 '{' 到最后一个 '}'。
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start : end + 1]

    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    # summary:必需,≤120 字
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    summary = summary.strip()[: _SUMMARY_MAX]

    # factor_assessment:必需,非空列表,逐项校验
    raw_fa = data.get("factor_assessment")
    if not isinstance(raw_fa, list):
        return None
    assessments: list[dict] = []
    for item in raw_fa:
        if not isinstance(item, dict):
            continue
        code = item.get("factor_code")
        assessment = item.get("assessment")
        note = item.get("note")
        if not isinstance(code, str) or not code.strip():
            continue
        if not isinstance(assessment, str) or assessment not in ASSESSMENT_VALUES:
            continue
        if not isinstance(note, str):
            note = ""
        assessments.append({
            "factor_code": code.strip(),
            "assessment": assessment,
            "note": note.strip()[: _NOTE_MAX],
        })
    if not assessments:
        return None

    # adjustment_suggestion:≤80 字(缺失给空串)
    suggestion = data.get("adjustment_suggestion")
    if not isinstance(suggestion, str):
        suggestion = ""
    suggestion = suggestion.strip()[: _SUGGESTION_MAX]

    # confidence:非法回落「中」
    confidence = data.get("confidence")
    if not isinstance(confidence, str) or confidence not in CONFIDENCE_VALUES:
        confidence = "中"

    return {
        "summary": summary,
        "factor_assessment": assessments,
        "adjustment_suggestion": suggestion,
        "confidence": confidence,
    }


def _run_coro(coro):
    """同步上下文执行异步协程:无运行中事件循环 → asyncio.run;有(如 FastAPI 请求
    上下文)→ 新建独立事件循环执行,避免 asyncio.run 抛
    "cannot be called from a running event loop"。(镜像 intraday_monitor._run_coro)"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    new_loop = asyncio.new_event_loop()
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()


def _build_client(db=None):
    """构造 LLM 客户端:优先 db 场景绑定(factor_ic_report/chat),回落
    Settings/env(AI_BASE_URL/AI_API_KEY/AI_MODEL)。(镜像 intraday_monitor._build_counter_client)"""
    from src.core.ai_client import AIClient

    if db is not None:
        try:
            from src.core.ai_client import get_model_for_scene
            from src.web.models import AIService

            m = get_model_for_scene(db, "factor_ic_report") or get_model_for_scene(db, "chat")
            if m is not None:
                s = db.query(AIService).filter(AIService.id == m.service_id).first()
                if s is not None and s.base_url and s.api_key:
                    return AIClient(base_url=s.base_url, api_key=s.api_key, model=m.model)
        except Exception as e:
            logger.debug(f"因子IC归因场景绑定不可用(回落 Settings/env): {e}")
    from src.config import Settings

    settings = Settings()
    return AIClient(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
    )


async def _ai_report_chat(system: str, user: str, db=None) -> str:
    """单次 LLM 调用(8s 超时, wait_for 保证)。客户端在协程内构造,避免跨事件循环复用。"""
    client = _build_client(db)
    return await asyncio.wait_for(
        client.chat(system, user, temperature=0.2),
        timeout=_LLM_TIMEOUT,
    )


def generate_factor_ic_report(market: str = "CN", db=None) -> dict | None:
    """主入口:IC/IR → LLM 归因解读 + 调权建议(自然语言)。

    流程:evaluate_factor_ic → (factors 为空或全部 ic=None → 返回 None 不调 LLM)
          → build prompt → LLM → parse。异常/超时/非法 JSON 一律静默返回 None。
    """
    try:
        ic_result = evaluate_factor_ic(market=market, db=db)
    except Exception as e:
        logger.warning(f"[因子IC归因] evaluate_factor_ic 失败: {e}")
        return None
    if not isinstance(ic_result, dict):
        return None

    factors = ic_result.get("factors", {}) or {}
    # 因子为空,或全部 ic 缺失 → 无信息可喂,不调 LLM。
    if not factors or all((v or {}).get("ic") is None for v in factors.values()):
        return None

    system, user = build_ic_report_prompt(ic_result)
    try:
        reply = _run_coro(_ai_report_chat(system, user, db))
    except Exception as e:
        logger.warning(f"[因子IC归因] LLM 调用失败/超时: {e}")
        return None
    return parse_ic_report_reply(reply)
