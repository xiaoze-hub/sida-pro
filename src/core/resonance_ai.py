"""三指标共振 AI 判定(2026-09-11, 老板"要接入ai分析, 给出是否共振; 现在没办法知道到底有没有共振")。

输入(全部来自我们自己的实现, 口径唯一):
- 趋势: GS(gs_strategy.eval_gs/trend_label) → G区/G信号/…
- 强度: AI机构活跃度(ai_activity) → 数值/档位/连强天数
- 资金: 主力净流入(通达信 SUPAMO, 元; 缺则显式 None)
- 序列: 近 N 日活跃度(供 AI 判断趋势变化)

输出: 结构化判定(强共振/弱共振/未共振/无法判定) + 置信度 + 依据/风险/关注点 + 一句话结论。
纪律: 缺数据必须说"无法判定"并列缺项, 禁止编造; 提示词统一挂合规护栏(with_compliance)。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

VERDICT_STRONG = "强共振"
VERDICT_WEAK = "弱共振"
VERDICT_NONE = "未共振"
VERDICT_UNKNOWN = "无法判定"

SYSTEM_PROMPT = (
    "你是 SIDA(数智分析)的「三指标共振」分析员。三个指标定义:\n"
    "1) 趋势(GS策略): G区/G信号=趋势向上; S区/S信号=趋势向下; 无数据=不可用。\n"
    "2) 强度(AI机构活跃度): 数值, 档位 弱(<1.56)/生命(1.56~3)/强势(3~6)/大牛(≥6); 阈值 1.56/3/6。\n"
    "3) 资金(主力净流入): 正=净流入, 负=净流出, 单位元; 缺失=不可得。\n"
    "判定口径: 趋势在 G 区 + 活跃度≥3(强势线) + 资金净流入 = **强共振**; 满足两项 = **弱共振**; "
    "否则 **未共振**; 关键项缺失导致无法判断时, 必须输出 **无法判定** 并列出缺失项。\n"
    "要求: 只依据给出的数据推理, 禁止脑补或引用外部信息; 语言精炼; 必须输出 JSON。\n"
    '输出 JSON(不要多余文字): {"resonance":"强共振|弱共振|未共振|无法判定","confidence":0~1,'
    '"summary":"一句话结论(≤40字)","reasons":["依据1","依据2"],"risks":["风险/背离1"],'
    '"watch":["下一步该看什么1"],"missing":["缺失项(如有)"]}'
)


def build_user_content(
    symbol: str,
    name: str,
    detail: dict,
    series: list[dict] | None = None,
) -> str:
    """把三指标数据拼成给 LLM 的用户消息(纯函数, 便于测试)。"""

    def fmt_fund(v) -> str:
        if v is None:
            return "无数据"
        try:
            yi = float(v) / 1e8
        except (TypeError, ValueError):
            return "无数据"
        return f"{yi:+.2f}亿"

    lines = [
        f"标的: {name or ''}({symbol})",
        f"数据日期: {detail.get('trade_date') or '未知'}",
        "三指标现状:",
        f"- 趋势(GS): {detail.get('trend') or '无数据'}",
        f"- 强度(AI机构活跃度): {detail.get('activity') if detail.get('activity') is not None else '无数据'}"
        f" (档位 {detail.get('level') or '未知'})",
        f"- 资金(主力净流入): {fmt_fund(detail.get('fund_net'))}",
        f"规则引擎判定(供参考, 可修正): {detail.get('level3') or '无'}",
    ]
    if series:
        recent = series[-10:]
        seq = ", ".join(
            f"{str(i.get('date'))[-4:]}:{i.get('activity') if i.get('activity') is not None else '-'}" for i in recent
        )
        lines.append(f"近10日活跃度序列(MMDD:值): {seq}")
    lines.append("请按口径给出共振判定与依据; 数据缺失的项目必须列入 missing, 不得猜测。")
    return "\n".join(lines)


def parse_ai_verdict(content: str | None) -> dict:
    """解析 LLM 输出的 JSON(容错: 提取首个 {...} 块); 失败 → 无法判定(不编造)。"""
    if not content:
        return {
            "resonance": VERDICT_UNKNOWN,
            "confidence": None,
            "summary": "AI 未返回内容",
            "reasons": [],
            "risks": [],
            "watch": [],
            "missing": [],
            "parse_error": True,
        }
    raw = content.strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    data = None
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            data = None
    if not isinstance(data, dict):
        return {
            "resonance": VERDICT_UNKNOWN,
            "confidence": None,
            "summary": raw[:80],
            "reasons": [],
            "risks": [],
            "watch": [],
            "missing": [],
            "parse_error": True,
        }
    verdict = str(data.get("resonance") or "").strip()
    if verdict not in (VERDICT_STRONG, VERDICT_WEAK, VERDICT_NONE, VERDICT_UNKNOWN):
        verdict = VERDICT_UNKNOWN

    def _list(key: str) -> list[str]:
        v = data.get(key)
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()][:6]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    conf = data.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf))) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    return {
        "resonance": verdict,
        "confidence": conf,
        "summary": str(data.get("summary") or "").strip()[:120],
        "reasons": _list("reasons"),
        "risks": _list("risks"),
        "watch": _list("watch"),
        "missing": _list("missing"),
        "parse_error": False,
    }
