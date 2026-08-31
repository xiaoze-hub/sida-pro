"""A股预测报告生成器 — DSA 双格式

借鉴 DSA 报告模板，为每次预测自动生成两种格式：
- dashboard_md  : 短版卡片精简版（前端预测页内嵌用，< 800 字）
- detail_md     : 完整研报版（推送/历史回查用，含四模型分解+情绪+回测+策略合成）

用法（从 forecast_server.py 调用）：
    from forecast_reports import generate_report, generate_backtest_report
    dash, detail = generate_report(run_id, result_dict)
"""

import json
import os
from datetime import datetime
from typing import Any

from forecast_traces import (
    get_run, list_model_outputs, list_sentiment_evals,
    list_reports_for_symbol, save_prediction_report,
    get_backtest, list_backtests_for_symbol,
)


# ── emoji 工具 ──────────────────────────────────────────────
def _dir_emoji(direction: str) -> str:
    return {"up": "🟢", "down": "🔻", "flat": "🟡"}.get(direction, "⚪")


def _action_emoji(action: str) -> str:
    a = (action or "").lower()
    if "加仓" in a or "买" in a or "建仓" in a:
        return "🎯"
    elif "减" in a or "卖" in a or "止损" in a:
        return "🛑"
    elif "持有" in a or "观望" in a:
        return "🔵"
    return "📌"


def _conf_emoji(conf: str) -> str:
    c = (conf or "").lower()
    if "高" in c:
        return "🟢"
    elif "低" in c:
        return "🔴"
    return "🟡"


# ── 模型名称中文化 ──────────────────────────────────────────
MODEL_CN = {
    "kronos": "Kronos(蒙特卡洛)",
    "chronos": "Chronos-Bolt(时序基础模型)",
    "xgboost": "XGBoost",
    "linear_reg": "线性回归",
    "linreg": "线性回归",
}


def _model_cn(name: str) -> str:
    return MODEL_CN.get(name, name)


def _emotion_temperature_score(sentiment: dict) -> int | None:
    """借鉴 TSP 飞书 AI 复盘的「情绪温度」量化(0-100)。

    维度:
      - 情绪面 adjustment_pct 映射到 0-100(±2% → 0-100 区间)
      - market_sentiment 文本定性加分(看多 +10,看空 -10)
      - notes 数量 > 3 加 5(情绪信号密集度)
    返回 None 表示无足够数据。
    """
    if not sentiment:
        return None
    adj = sentiment.get("adjustment_pct")
    if adj is None:
        return None
    # adjustment_pct 通常 -2 到 +2, 线性映射 0..100
    base = max(0, min(100, 50 + adj * 25))
    market = (sentiment.get("market_sentiment") or "").strip()
    if "看多" in market or "多头" in market or "强势" in market:
        base = min(100, base + 10)
    elif "看空" in market or "空头" in market or "弱势" in market:
        base = max(0, base - 10)
    notes = sentiment.get("notes") or []
    if isinstance(notes, list) and len(notes) >= 3:
        base = min(100, base + 5)
    return int(round(base))


def _emotion_temperature_label(score: int) -> str:
    """0-100 温度转中文标签(借鉴 TSP 飞书 AI 复盘)。"""
    if score >= 80:
        return "🔥 火热"
    if score >= 60:
        return "😊 偏暖"
    if score >= 40:
        return "😐 中性"
    if score >= 20:
        return "😟 偏冷"
    return "🥶 极冷"


def _model_direction_consensus(models: dict) -> dict | None:
    """统计四模型方向一致率(借鉴 TSP「投机维度」「指数维度」拆解思路)。

    返回: {"up": int, "down": int, "flat": int, "total": int, "dominant": "up"|"down"|"flat"}
    models 形如 {"kronos": {"median": [...], "direction": "up"}, "xgboost": {"direction": "up"}, ...}
    """
    if not models or not isinstance(models, dict):
        return None
    counts = {"up": 0, "down": 0, "flat": 0}
    total = 0
    for _, v in models.items():
        if not isinstance(v, dict):
            continue
        d = v.get("direction")
        if d in counts:
            counts[d] += 1
            total += 1
    if total == 0:
        return None
    # tie-break: up > flat > down (按倾向选择, 避免 flat 抢优先)
    dominant = max(counts, key=lambda k: (counts[k], {"up": 2, "flat": 1, "down": 0}[k]))
    return {**counts, "total": total, "dominant": dominant, "consensus_pct": round(counts[dominant] / total * 100)}


def _build_one_liner(direction: str, expected_pct: float, action: str, confidence: str,
                     sentiment: dict, models: dict) -> str:
    """借鉴 TSP「一句话定调」:基于多维信号合成一段 narrative(1-2 句)。

    设计原则(借鉴 TSP 飞书 AI 复盘):
      - 短(<= 60 字,手机单屏可见)
      - 信息密集(包含方向 + 共识 + 情绪温度)
      - 不编造数据(全用 result 里有的字段)
    """
    parts = []

    # 1. 方向 + 一致性(借鉴 TSP「指数强弱排序」)
    consensus = _model_direction_consensus(models)
    dir_cn = {"up": "看多", "down": "看空", "flat": "横盘"}.get(direction, direction)
    if consensus and consensus["consensus_pct"] >= 75:
        parts.append(f"四模型高度一致{consensus['consensus_pct']}%{dir_cn}")
    elif consensus and consensus["consensus_pct"] >= 50:
        parts.append(f"四模型{consensus['consensus_pct']}%{dir_cn}")
    else:
        parts.append(f"四模型分歧明显,共识偏向 {dir_cn}")

    # 2. 预期幅度
    pct = f"{abs(expected_pct):.1f}%"
    if expected_pct > 0:
        parts.append(f"预期上行 {pct}")
    elif expected_pct < 0:
        parts.append(f"预期下行 {pct}")
    else:
        parts.append("预期横盘")

    # 3. 情绪温度(借鉴 TSP「情绪温度 67 = 偏暖」)
    temp_score = _emotion_temperature_score(sentiment)
    if temp_score is not None:
        temp_label = _emotion_temperature_label(temp_score)
        parts.append(f"市场情绪 {temp_label}({temp_score})")

    # 4. 操作建议(短)
    action_short = {"买入": "建议介入", "持有": "建议持仓观望", "卖出": "建议减仓"}.get(action, action)
    if confidence in ("高", "中高"):
        parts.append(f"{action_short}({confidence}置信)")
    else:
        parts.append(f"{action_short}")

    # 用「;」分隔,符合 TSP 飞书版"结构化 narrative"风格
    return " · ".join(parts)


def _build_tomorrow_tone(direction: str, expected_pct: float, action: str,
                         confidence: str, sentiment: dict) -> str:
    """借鉴 TSP「明日基调」:结尾给一段展望性 narrative。

    设计:
      - 包含方向 + 置信度(让用户知道 AI 站位)
      - 不超过 50 字
      - 高置信度给明确指引,中低置信度给"等待确认"风格(借鉴 TSP「明日基调:均衡」)
    """
    dir_cn = {"up": "偏多", "down": "偏空", "flat": "震荡"}.get(direction, "震荡")
    abs_pct = abs(expected_pct)

    # 高置信 + 大幅度 → 明确基调
    if confidence in ("高", "中高") and abs_pct >= 3:
        if direction == "up":
            return f"高置信看多,可考虑顺势而为,但注意回踩风险。"
        if direction == "down":
            return f"高置信看空,建议观望或减仓,等待企稳信号。"
    # 中等置信 → 中性基调
    if confidence == "中":
        return f"中等置信偏{action},建议持仓观望,等待明日开盘信号确认。"
    # 低置信 → 等待基调(借鉴 TSP「均衡」风格)
    return f"信号偏弱,明日基调:观望,建议不追高/杀跌,等市场给出明确方向。"


# ── 主报告生成器 ────────────────────────────────────────────
def generate_report(
    run_id: int,
    result: dict,
    backtest_data: dict | None = None,
) -> tuple[str, str]:
    """生成 dashboard_md + detail_md 双格式报告。

    参数:
        run_id       : prediction_runs.id
        result       : /predict 返回的完整 result dict
        backtest_data: 可选，/backtest 返回的回测结果(用于"历史命中率"段落)

    返回:
        (dashboard_md, detail_md)
    """
    run = get_run(run_id) or {}
    model_outputs = list_model_outputs(run_id)
    sentiment_evals = list_sentiment_evals(run_id)

    symbol = result.get("symbol", "")
    stock_name = result.get("stock_name", "")
    last_close = result.get("last_close", 0)
    last_date = result.get("last_date", "")
    target_date = result.get("target_date", "")
    pred_days = result.get("pred_days", 5)
    prediction = result.get("prediction", [])
    direction = result.get("direction", "flat")
    expected_pct = result.get("expected_pct", 0)
    rec = result.get("recommendation", {}) or {}
    sentiment = result.get("sentiment", {}) or {}
    models = result.get("models", {}) or {}

    # DB 读取时 models/recommendation/sentiment 可能是 JSON 字符串(forecast_history 序列化)
    def _coerce(obj):
        if isinstance(obj, str):
            try:
                return json.loads(obj)
            except Exception:
                return {}
        return obj

    rec = _coerce(rec) or {}
    sentiment = _coerce(sentiment) or {}
    models = _coerce(models) or {}

    elapsed = result.get("elapsed_ms", 0)

    dir_cn = {"up": "看多", "down": "看空", "flat": "横盘"}.get(direction, direction)
    dir_emoji = _dir_emoji(direction)
    action = rec.get("action", "持有")
    action_emoji = _action_emoji(action)
    confidence = rec.get("confidence", "中")
    conf_emoji = _conf_emoji(confidence)

    title = f"{symbol} {stock_name}" if stock_name else symbol
    pred_end = prediction[-1] if prediction else last_close

    # ── Dashboard 短版 ──────────────────────────────────
    dash_lines = [
        f"## 📊 {title} 预测报告",
        f"",
        f"🎯 基准 {last_date} 收盘 **{last_close:.2f}** → 目标 {target_date} 预测 **{pred_end:.2f}**",
        f"{dir_emoji} 方向: **{dir_cn}** 预期涨幅 **{expected_pct:+.1f}%** 预测周期 {pred_days}日",
        f"",
        f"{action_emoji} 操作: **{action}** {conf_emoji} 置信度: {confidence}",
    ]

    if rec.get("target_price"):
        dash_lines.append(f"🎯 理想买入: {rec.get('ideal_buy', last_close)} → 目标价: {rec['target_price']}")
    if rec.get("stop_loss"):
        dash_lines.append(f"🛑 止损: {rec['stop_loss']}")
    if rec.get("take_profit"):
        dash_lines.append(f"🎊 止盈: {rec['take_profit']}")

    dash_lines.append("")
    dash_lines.append(f"📌 {rec.get('summary', '多模型加权投票，综合基本面与情绪面修正。')}")

    # 模型一览
    if model_outputs:
        dash_lines.append("")
        dash_lines.append("| 模型 | 预测价 | 方向 | 耗时 |")
        dash_lines.append("|---|---|---|---|")
        for m in model_outputs:
            mc = m.get("model_pred_close")
            md = m.get("model_pred_direction", "")
            mt = m.get("run_time_ms", 0)
            mc_str = f"{mc:.2f}" if mc else "—"
            md_str = {"up": "↑", "down": "↓", "flat": "→"}.get(md, md)
            dash_lines.append(f"| {_model_cn(m.get('model_name', ''))} | {mc_str} | {md_str} | {mt}ms |")

    # 情绪面
    adj = sentiment.get("adjustment_pct", 0)
    if adj != 0:
        dash_lines.append("")
        dash_lines.append(f"💭 情绪修正: **{adj:+.2f}%** ({sentiment.get('market_sentiment', '中性')})")

    # 回测命中率
    if backtest_data and backtest_data.get("direction_accuracy_pct") is not None:
        acc = backtest_data["direction_accuracy_pct"]
        dash_lines.append("")
        dash_lines.append(f"📈 历史回测命中率: **{acc}%** ({backtest_data.get('source', '')})")

    dash_lines.append("")
    dash_lines.append(f"⏱ 耗时 {elapsed}ms · {datetime.now().strftime('%Y-%m-%d %H:%M')} 生成")

    dashboard_md = "\n".join(dash_lines)

    # ── Detail 完整版 ────────────────────────────────────
    detail_lines = [
        f"# 📊 {title} 预测报告",
        f"",
        f"> 基准日 {last_date} | 目标日 {target_date} | 预测周期 {pred_days} 个交易日",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Run ID: {run_id}",
        f"",
        f"## 🎯 一句话核心",
        f"",
        f"{dir_emoji} **{dir_cn}** · 预期涨幅 **{expected_pct:+.1f}%** · {action_emoji} **{action}** · {conf_emoji} 置信度 {confidence}",
        f"",
        f"> {rec.get('summary', '')}",
        f"",
        f"## 📊 数据面",
        f"",
        f"| 项目 | 值 |",
        f"|---|---|",
        f"| 基准收盘 | {last_close:.2f} |",
        f"| 预测目标日收盘 | {pred_end:.2f} |",
        f"| 预期涨跌幅 | {expected_pct:+.1f}% |",
        f"| 预测方向 | {dir_cn} |",
        f"| 预测天数 | {pred_days} 日 |",
    ]

    if prediction:
        detail_lines.append("")
        detail_lines.append("### 逐日预测序列")
        detail_lines.append("")
        detail_lines.append("| T+N | 预测价 | 涨幅 |")
        detail_lines.append("|---|---|---|")
        for i, p in enumerate(prediction, 1):
            pct = (p / last_close - 1) * 100 if last_close else 0
            detail_lines.append(f"| T+{i} | {p:.2f} | {pct:+.1f}% |")

    # 四模型分解
    if model_outputs:
        detail_lines.append("")
        detail_lines.append("## 🧠 四模型分解")
        detail_lines.append("")
        detail_lines.append("| 模型 | 预测价 | 方向 | 置信度 | 权重 | 耗时 |")
        detail_lines.append("|---|---|---|---|---|---|")
        for m in model_outputs:
            mc = m.get("model_pred_close")
            mc_str = f"{mc:.2f}" if mc else "—"
            md = m.get("model_pred_direction", "")
            md_cn = {"up": "↑看多", "down": "↓看空", "flat": "→横盘"}.get(md, md)
            mc_conf = m.get("model_confidence")
            mc_conf_str = f"{mc_conf:.1%}" if mc_conf is not None else "—"
            mw = m.get("model_weight")
            mw_str = f"{mw:.1%}" if mw is not None else "—"
            detail_lines.append(f"| {_model_cn(m.get('model_name', ''))} | {mc_str} | {md_cn} | {mc_conf_str} | {mw_str} | {m.get('run_time_ms', 0)}ms |")

    # 模型原始输出 JSON (折叠)
    if models:
        detail_lines.append("")
        detail_lines.append("<details><summary>📋 模型原始输出 JSON</summary>")
        detail_lines.append("")
        detail_lines.append("```json")
        # 精简输出，去掉过大字段
        slim = {}
        for k, v in models.items():
            if v is None:
                slim[k] = None
            elif isinstance(v, dict):
                slim[k] = {kk: vv for kk, vv in v.items() if kk in ("median", "mean", "samples", "confidence")}
            elif isinstance(v, list):
                slim[k] = [round(float(x), 2) if isinstance(x, (int, float)) else x for x in v[:10]]
            else:
                slim[k] = v
        detail_lines.append(json.dumps(slim, ensure_ascii=False, indent=2))
        detail_lines.append("```")
        detail_lines.append("</details>")

    # 情绪面分析
    detail_lines.append("")
    detail_lines.append("## 💭 情绪面分析")
    detail_lines.append("")
    adj = sentiment.get("adjustment_pct", 0)
    detail_lines.append(f"- 市场情绪: {sentiment.get('market_sentiment', '中性')}")
    detail_lines.append(f"- 情绪修正系数: **{adj:+.2f}%**")
    _notes = sentiment.get("notes") or []
    if isinstance(_notes, list) and _notes:
        for _n in _notes[:4]:
            detail_lines.append(f"- {_n}")
    elif isinstance(_notes, str) and _notes:
        detail_lines.append(f"- {_notes}")

    if sentiment_evals:
        detail_lines.append("")
        detail_lines.append("### LLM 情绪打分明细")
        detail_lines.append("")
        detail_lines.append("| 来源 | 事件 | 得分 | 修正 | 延迟 | 说明 |")
        detail_lines.append("|---|---|---|---|---|---|")
        for e in sentiment_evals:
            ev = (e.get("events_text", "") or "")[:30]
            sc = e.get("score", "—")
            ad = e.get("adjustment_pct", 0)
            lat = e.get("latency_ms", 0)
            reason = (e.get("reason", "") or "")[:40]
            src = e.get("source", "")
            detail_lines.append(f"| {src} | {ev} | {sc} | {ad:+.1f}% | {lat}ms | {reason} |")

    # 历史对比
    past_reports = list_reports_for_symbol(symbol, limit=3)
    if past_reports:
        detail_lines.append("")
        detail_lines.append("## 📜 历史预测对比")
        detail_lines.append("")
        detail_lines.append("| 日期 | 方向 | 预期 | 目标价 | |")
        detail_lines.append("|---|---|---|---|---|")
        for pr in past_reports[:3]:
            r = pr.get("run_data", {})
            detail_lines.append(
                f"| {pr.get('created_at', '')[:10]} | "
                f"{_dir_emoji(r.get('final_direction', ''))} | "
                f"{r.get('final_expected_pct', 0):+.1f}% | "
                f"{r.get('final_target_price', '—')} | |"
            )

    # 回测命中率
    if backtest_data and backtest_data.get("direction_accuracy_pct") is not None:
        detail_lines.append("")
        detail_lines.append("## 📈 历史回测表现")
        detail_lines.append("")
        acc = backtest_data["direction_accuracy_pct"]
        source = backtest_data.get("source", "")
        detail_lines.append(f"- 整体方向命中率: **{acc}%**")
        if backtest_data.get("llm_adjustment_win_pct") is not None:
            detail_lines.append(f"- LLM 修正胜率: {backtest_data['llm_adjustment_win_pct']}%")
        detail_lines.append(f"- 数据源: {source}")

        model_hits = backtest_data.get("models", {})
        if model_hits:
            detail_lines.append("")
            detail_lines.append("| 模型 | 样本 | 命中 | 准确率 |")
            detail_lines.append("|---|---|---|---|")
            for name, mh in model_hits.items():
                detail_lines.append(
                    f"| {_model_cn(name)} | {mh.get('samples', 0)} | {mh.get('hits', 0)} | {mh.get('accuracy_pct', 0)}% |"
                )

        samples = backtest_data.get("recent_samples", [])
        if samples:
            detail_lines.append("")
            detail_lines.append("### 近期回测样本")
            detail_lines.append("")
            detail_lines.append("| 日期 | 预测价 | 实际价 | 方向命中 |")
            detail_lines.append("|---|---|---|---|")
            for s in samples[-5:]:
                if "target_date" in s:
                    detail_lines.append(
                        f"| {s['target_date']} | {s.get('final_pred_pct', 0):+.1f}% | "
                        f"{s.get('actual_pct', 0):+.1f}% | "
                        f"{'✅' if s.get('final_pred_dir') == s.get('actual_dir') else '❌'} |"
                    )
                else:
                    detail_lines.append(
                        f"| {s.get('date', '')} | {s.get('pred_close', 0):.2f} | "
                        f"{s.get('actual_close', 0):.2f} | "
                        f"{'✅' if s.get('hit') else '❌'} |"
                    )

    # 策略合成
    detail_lines.append("")
    detail_lines.append("## 🧩 策略合成")
    detail_lines.append("")
    detail_lines.append(f"{action_emoji} **操作建议: {action}**")
    detail_lines.append(f"{conf_emoji} 置信度: {confidence}")
    if rec.get("ideal_buy"):
        detail_lines.append(f"🎯 理想买入价: {rec['ideal_buy']}")
    if rec.get("target_price"):
        detail_lines.append(f"🎯 目标价: {rec['target_price']}")
    if rec.get("stop_loss"):
        detail_lines.append(f"🛑 止损价: {rec['stop_loss']}")
    if rec.get("take_profit"):
        detail_lines.append(f"🎊 止盈价: {rec['take_profit']}")

    risk_note = rec.get("risk_note", "")
    if risk_note:
        detail_lines.append("")
        detail_lines.append(f"🚨 风险提醒: {risk_note}")

    detail_lines.append("")
    detail_lines.append("---")
    detail_lines.append(f"*本报告由 PanWatch 预测引擎自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    detail_md = "\n".join(detail_lines)

    return dashboard_md, detail_md


# ── 回测报告生成器 ──────────────────────────────────────────
def generate_backtest_report(
    backtest_id: int,
    backtest_data: dict,
    symbol: str,
) -> tuple[str, str]:
    """生成回测报告的 Dashboard + Detail 双格式。

    参数:
        backtest_id   : backtest_results.id
        backtest_data : /backtest 返回的完整 dict
        symbol        : 股票代码

    返回:
        (dashboard_md, detail_md)
    """
    acc = backtest_data.get("direction_accuracy_pct", 0)
    source = backtest_data.get("source", "")
    model_hits = backtest_data.get("models", {})
    llm_win = backtest_data.get("llm_adjustment_win_pct")
    samples = backtest_data.get("recent_samples", [])
    runs_used = backtest_data.get("runs_used") or backtest_data.get("windows_tested", 0)

    # Dashboard 短版
    dash_lines = [
        f"## 📈 {symbol} 回测报告",
        f"",
        f"📊 整体命中率: **{acc}%** ({source})",
        f"🧪 样本数: {runs_used}",
    ]
    if llm_win is not None:
        dash_lines.append(f"💭 LLM 修正胜率: **{llm_win}%**")

    if model_hits:
        dash_lines.append("")
        dash_lines.append("| 模型 | 准确率 |")
        dash_lines.append("|---|---|")
        for name, mh in model_hits.items():
            dash_lines.append(f"| {_model_cn(name)} | {mh.get('accuracy_pct', 0)}% |")

    # Detail 完整版
    detail_lines = [
        f"# 📈 {symbol} 回测报告",
        f"",
        f"> 回测 ID: {backtest_id} | 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"## 📊 回测概况",
        f"",
        f"| 项目 | 值 |",
        f"|---|---|",
        f"| 数据源 | {source} |",
        f"| 样本数 | {runs_used} |",
        f"| 方向命中率 | **{acc}%** |",
    ]
    if llm_win is not None:
        detail_lines.append(f"| LLM 修正胜率 | {llm_win}% |")

    if model_hits:
        detail_lines.append("")
        detail_lines.append("## 🧠 逐模型命中率")
        detail_lines.append("")
        detail_lines.append("| 模型 | 样本 | 命中 | 准确率 |")
        detail_lines.append("|---|---|---|---|")
        for name, mh in model_hits.items():
            detail_lines.append(
                f"| {_model_cn(name)} | {mh.get('samples', 0)} | {mh.get('hits', 0)} | {mh.get('accuracy_pct', 0)}% |"
            )

    if samples:
        detail_lines.append("")
        detail_lines.append("## 📋 全部样本")
        detail_lines.append("")
        if "target_date" in samples[0]:
            detail_lines.append("| 目标日 | 预测方向 | 预期% | 实际% | 命中 |")
            detail_lines.append("|---|---|---|---|---|")
            for s in samples:
                detail_lines.append(
                    f"| {s.get('target_date', '')} | "
                    f"{{'up':'↑','down':'↓','flat':'→'}}.get(s.get('final_pred_dir',''),'') | "
                    f"{s.get('final_pred_pct', 0):+.1f}% | "
                    f"{s.get('actual_pct', 0):+.1f}% | "
                    f"{'✅' if s.get('final_pred_dir') == s.get('actual_dir') else '❌'} |"
                )
        else:
            detail_lines.append("| 日期 | 预测价 | 实际价 | 命中 |")
            detail_lines.append("|---|---|---|---|")
            for s in samples:
                detail_lines.append(
                    f"| {s.get('date', '')} | {s.get('pred_close', 0):.2f} | "
                    f"{s.get('actual_close', 0):.2f} | "
                    f"{'✅' if s.get('hit') else '❌'} |"
                )

    detail_lines.append("")
    detail_lines.append("---")
    detail_lines.append(f"*PanWatch 预测引擎回测 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    return "\n".join(dash_lines), "\n".join(detail_lines)


# ── 企微推送专用版（手机友好的精简排版）─────────────────────────────
def generate_wecom_report(result: dict, backtest_data: dict | None = None) -> str:
    """生成专为企业微信手机端优化的推送文案。

    与 detail_md 的区别:
    - 不用表格(| col |) / 斜杠分隔(手机上挤、不可读)
    - 四模型逐行展示
    - 去掉原始 JSON 代码块(超长且手机无意义)
    - 用 emoji 分段 + --- 分隔线
    - 控制长度(企微单条上限 4096 字节, 中文约 1300 字安全)
    """
    symbol = result.get("symbol", "")
    stock_name = result.get("stock_name", "")
    last_close = result.get("last_close", 0)
    last_date = result.get("last_date", "")
    target_date = result.get("target_date", "")
    pred_days = result.get("pred_days", 5)
    prediction = result.get("prediction", [])
    direction = result.get("direction", "flat")
    expected_pct = result.get("expected_pct", 0)
    rec = result.get("recommendation", {}) or {}
    sentiment = result.get("sentiment", {}) or {}
    models = result.get("models", {}) or {}

    def _coerce(obj):
        if isinstance(obj, str):
            try:
                return json.loads(obj)
            except Exception:
                return {}
        return obj
    rec = _coerce(rec) or {}
    sentiment = _coerce(sentiment) or {}
    models = _coerce(models) or {}

    dir_cn = {"up": "看多", "down": "看空", "flat": "横盘"}.get(direction, direction)
    dir_emoji = _dir_emoji(direction)
    action = rec.get("action", "持有")
    action_emoji = _action_emoji(action)
    confidence = rec.get("confidence", "中")
    conf_emoji = _conf_emoji(confidence)
    title = f"{symbol} {stock_name}" if stock_name else symbol
    pred_end = prediction[-1] if prediction else last_close

    L = []
    # 标题
    L.append(f"# 📊 {title}")
    L.append("")
    # 一句话核心
    L.append(f"{dir_emoji} **{dir_cn}** · 预期 **{expected_pct:+.1f}%** · {action_emoji} **{action}** · {conf_emoji} {confidence}")
    L.append(f"🎯 {last_date} 收 {last_close:.2f} → {target_date} 看 {pred_end:.2f}（{pred_days}日）")
    # LLM 总结(自然语言投资结论)
    summary = rec.get("summary", "")
    if summary:
        L.append("")
        L.append(f"📝 {summary}")
    # ✨ 借鉴 TSP 飞书 AI 复盘:一句话定调(基于多维信号合成的 narrative)
    # 用情绪面 + 资金面 + 方向 + 置信度综合成一段 narrative
    one_liner = _build_one_liner(direction, expected_pct, action, confidence,
                                 sentiment=sentiment, models=models)
    if one_liner:
        L.append("")
        L.append(f"### 🎯 一句话定调")
        L.append(f"_{one_liner}_")
    L.append("")
    L.append("---")
    # 数据面(要点)
    L.append("## 📊 数据面")
    L.append(f"- 基准收盘：**{last_close:.2f}**")
    L.append(f"- 预测目标收盘：**{pred_end:.2f}**")
    L.append(f"- 预期涨跌幅：**{expected_pct:+.1f}%**")
    if prediction:
        seq = " → ".join(f"T+{i}:{p:.2f}" for i, p in enumerate(prediction, 1))
        L.append(f"- 逐日序列：{seq}")
    L.append("")
    L.append("---")
    # 四模型(逐行, 不用表格)
    L.append("## 🧠 四模型分解")
    # 优先用 model_outputs 风格字段, 兜底用 models
    mo = result.get("model_outputs") or []
    if mo:
        for m in mo:
            mc = m.get("model_pred_close")
            mc_str = f"{mc:.2f}" if mc else "—"
            md = {"up": "↑看多", "down": "↓看空", "flat": "→横盘"}.get(m.get("model_pred_direction", ""), m.get("model_pred_direction", ""))
            mc_conf = m.get("model_confidence")
            conf_str = f"{mc_conf:.1%}" if mc_conf is not None else "—"
            mw = m.get("model_weight")
            w_str = f"{mw:.1%}" if mw is not None else "—"
            L.append(f"· {_model_cn(m.get('model_name', ''))}：{mc_str} · {md} · 置信 {conf_str} · 权重 {w_str}")
    elif models:
        for name, v in models.items():
            if not isinstance(v, dict):
                continue
            med = v.get("median")
            if isinstance(med, list) and med:
                mc_str = f"{med[-1]:.2f}"
            elif isinstance(v, list) and v:
                mc_str = f"{v[-1]:.2f}"
            else:
                mc_str = "—"
            L.append(f"· {_model_cn(name)}：预测 {mc_str}")
    L.append("")
    L.append("---")
    # 情绪面
    adj = sentiment.get("adjustment_pct", 0)
    L.append("## 💭 情绪面")
    L.append(f"- 市场情绪：{sentiment.get('market_sentiment', '中性')}")
    # ✨ 借鉴 TSP 飞书 AI 复盘:情绪温度量化(0-100 + 标签)
    temp_score = _emotion_temperature_score(sentiment)
    if temp_score is not None:
        temp_label = _emotion_temperature_label(temp_score)
        L.append(f"- 情绪温度：**{temp_score} / 100** · {temp_label}（借鉴 TSP AI 复盘量化模型）")
    if adj:
        L.append(f"- 情绪修正：**{adj:+.2f}%**")
    notes = sentiment.get("notes") or []
    if isinstance(notes, list) and notes:
        for n in notes[:4]:
            L.append(f"- {n}")
    elif isinstance(notes, str) and notes:
        L.append(f"- {notes}")
    L.append("")
    L.append("---")
    # 资金面(东财口径主力净流入, 经 PanWatch tdx)
    cf = result.get("capital_flow") or []
    # ✨ 借鉴 Vibe-Trading「估算不了就声明」: 即使 cf 非空也可能缺关键日期
    # 用 return_data_status=True 拿到数据完整度,据此显式标注
    from forecast_utils import calc_capital_score
    _cap = calc_capital_score(
        cf, last_close=result.get("last_close", 0), return_data_status=True
    )
    # type: ignore[union-attr] -- return_data_status=True 保证返回 tuple
    cap_score: float = _cap[0]  # type: ignore[assignment]
    cap_status: str = _cap[1]  # type: ignore[assignment]
    if isinstance(cf, list) and cf:
        # 数据缺失/部分缺失时显式标注(借鉴 Vibe-Trading「估算不了就声明」)
        if cap_status == "missing":
            L.append("## 💰 资金面（主力净流入·东财口径）")
            L.append("- ⚠️ 资金面数据缺失(无任何主力资金记录),跳过资金面判断(借鉴 Vibe-Trading「估算不了就声明」原则)")
        else:
            L.append("## 💰 资金面（主力净流入·东财口径）")
            for r in cf:
                d = r.get("date", "")
                net = r.get("main_net", 0)
                arrow = "🟢" if net > 0 else ("🔴" if net < 0 else "⚪")
                unit = f"{net/1e8:+.2f}亿" if abs(net) >= 1e8 else f"{net/1e4:+.0f}万"
                L.append(f"- {d} {arrow} {unit}")
            # 趋势判断(近N日合计)—— 仅在 partial/complete 时有意义
            period_item = next((r for r in cf if "近" in str(r.get("date","")) and "日" in str(r.get("date",""))), None)
            if period_item:
                pnet = period_item.get("main_net", 0)
                if pnet > 0:
                    trend = f"近5日主力净流入合 {pnet/1e8:+.2f}亿，持续吸筹 💪"
                elif pnet < 0:
                    trend = f"近5日主力净流出合 {pnet/1e8:+.2f}亿，主力撤退 ⚠️"
                else:
                    trend = "近5日资金面中性"
                L.append(f"- **趋势**：{trend}")
            # 部分数据时显式提示(借鉴 Vibe-Trading「估算不了就声明」)
            if cap_status == "partial":
                L.append("- ⚠️ 部分数据缺失(只拿到当日/近N日中的一项),评分置信度降低")
            # 资金面联动策略结论
            if cap_score > 0.15:
                L.append(f"- **对策略影响**：资金面偏多(评分 {cap_score:+.2f})，确认看多方向，置信度上调 ✅")
            elif cap_score < -0.15:
                L.append(f"- **对策略影响**：资金面偏空(评分 {cap_score:+.2f})，与价格方向背离需警惕 ⚠️")
            else:
                L.append(f"- **对策略影响**：资金面中性(评分 {cap_score:+.2f})")
    else:
        # 资金面数据完全缺失 —— 借鉴 Vibe-Trading「估算不了就声明」
        L.append("## 💰 资金面（主力净流入·东财口径）")
        L.append("- ⚠️ 资金面数据缺失(无任何主力资金记录),跳过资金面判断")
    # 龙虎榜(游资信号, 经 marketdata ftshare)
    dt = result.get("dragon_tiger") or []
    if isinstance(dt, list) and dt:
        L.append("## 🐉 龙虎榜（游资动向）")
        first = dt[0]
        if first.get("on_list"):
            net = first.get("net_buy") or 0
            arrow = "🟢" if net > 0 else ("🔴" if net < 0 else "⚪")
            L.append(f"- ✅ **{result.get('symbol','')} 上榜**（{first.get('trade_date','')}）：游资净买入 {arrow} {net/1e4:+.0f}万")
            cp = first.get("change_pct")
            cp_str = f"{cp:.2f}%" if (cp is not None and abs(cp) <= 20) else "—(数据源异常)"
            L.append(f"- 收盘价 {first.get('close')} · 涨跌幅 {cp_str} · 买入 {first.get('buy_amt',0)/1e8:.2f}亿 / 卖出 {first.get('sell_amt',0)/1e8:.2f}亿")
            L.append("- **对策略影响**：获游资介入，短线情绪强，但需甄别席位性质（拉萨/机构/一线游资）⚡")
        else:
            mc = first.get("market_count", 0)
            mnet = first.get("market_net_buy", 0) or 0
            arrow = "🟢" if mnet > 0 else ("🔴" if mnet < 0 else "⚪")
            L.append(f"- 该标的未上榜；全市场龙虎榜 {mc} 只，游资净买入合计 {arrow} {mnet/1e8:+.2f}亿")
            tone = "游资活跃度偏高 🔥" if mc >= 40 else ("游资偏谨慎 😐" if mc <= 20 else "游资活跃度中性")
            L.append(f"- **市场情绪**：{tone}")
    L.append("")
    L.append("---")
    # 策略合成
    L.append("## 🧩 策略合成")
    L.append(f"{action_emoji} 操作建议：**{action}** · {conf_emoji} 置信度 {confidence}")
    if rec.get("ideal_buy"):
        L.append(f"🎯 理想买入价：{rec['ideal_buy']}")
    if rec.get("target_price"):
        L.append(f"🎯 目标价：{rec['target_price']}")
    if rec.get("stop_loss"):
        L.append(f"🛑 止损价：{rec['stop_loss']}")
    if rec.get("risk_note"):
        L.append(f"🚨 风险：{rec['risk_note']}")
    # 回测命中率
    if backtest_data and backtest_data.get("direction_accuracy_pct") is not None:
        L.append("")
        L.append(f"📈 历史回测命中率：**{backtest_data['direction_accuracy_pct']}%**")
    L.append("")
    L.append("---")
    # ✨ 借鉴 TSP 飞书 AI 复盘:「明日基调」+「免责声明」(结尾 narrative)
    tomorrow_tone = _build_tomorrow_tone(direction, expected_pct, action, confidence,
                                         sentiment=sentiment)
    if tomorrow_tone:
        L.append("")
        L.append(f"### 🔮 明日基调")
        L.append(f"_{tomorrow_tone}_")
    L.append("")
    L.append("---")
    # ✨ AI 免责声明(借鉴 TSP「本报告由 AI 基于公开行情数据生成,仅供参考,不构成任何投资建议」)
    L.append("> ⚠️ 本报告由数智分析 AI 生成,基于公开行情数据与多模型融合预测,**仅供学习研究,不构成任何投资建议**。交易有风险,入市需谨慎。")
    L.append(f"*PanWatch 自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}*")

    text = "\n".join(L)
    # 长度保护(企微 4096 字节, 中文 3 字节/字)
    if len(text.encode("utf-8")) > 3800:
        text = text[:1200].rstrip() + "\n…(完整版见 PanWatch App)"
    return text
