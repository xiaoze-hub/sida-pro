"""Shadow Account HTML 报告(移植自 HKUDS/Vibe-Trading, MIT,简化为单页)。

生成移动端友好 HTML(WeasyPrint 可用时同时出 PDF,失败降级 HTML-only)。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.core.shadow_account.models import (
    AttributionBreakdown,
    ShadowBacktestResult,
    ShadowProfile,
)

logger = logging.getLogger(__name__)

_REASON_LABELS = {
    "rule_violation": "违反规则(情绪单)",
    "early_exit": "过早离场",
    "late_exit": "过晚离场",
}


def render_shadow_report(
    profile: ShadowProfile,
    result: ShadowBacktestResult | None,
    behavior: dict[str, Any] | None = None,
    *,
    out_dir: str | Path,
    title: str = "影子账户报告",
) -> tuple[str, str | None]:
    """渲染 HTML 报告(可选 PDF)。

    Args:
        profile: 已提取的 ShadowProfile。
        result: 回测归因结果(可 None —— 只出画像段)。
        behavior: compute_behavior 输出(4 项行为诊断)。
        out_dir: 输出目录。
        title: 报告标题。

    Returns:
        (html_path, pdf_path_or_None)。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"{profile.shadow_id}.html"

    sections = _build_sections(profile, result, behavior)
    html = _render_html(title=title, sections=sections, shadow_id=profile.shadow_id)
    html_path.write_text(html, encoding="utf-8")

    pdf_path: Path | None = None
    try:
        from weasyprint import HTML as WeasyHTML

        pdf_path = out / f"{profile.shadow_id}.pdf"
        WeasyHTML(string=html, base_url=str(out)).write_pdf(str(pdf_path))
    except Exception as exc:  # pragma: no cover — weasyprint 系统库缺失时降级
        logger.warning("PDF render failed, HTML-only: %s", exc)
        pdf_path = None

    return str(html_path), (str(pdf_path) if pdf_path else None)


def _build_sections(
    profile: ShadowProfile,
    result: ShadowBacktestResult | None,
    behavior: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    """组装 (标题, HTML 片段) 段落列表。"""
    sections: list[tuple[str, str]] = [
        ("交易行为画像", _section_profile(profile)),
    ]

    if behavior:
        sections.append(("行为诊断", _section_behavior(behavior)))

    if profile.rules:
        sections.append(("提炼规则(影子策略)", _section_rules(profile)))

    if result:
        sections.append(("差值归因(影子 vs 真实)", _section_attribution(result)))
        sections.append(("反事实交易 Top 5", _section_counterfactual(result)))

    sections.append(("免责声明", _section_disclaimer()))
    return sections


def _section_profile(profile: ShadowProfile) -> str:
    rules_count = len(profile.rules)
    return (
        f"<p>{profile.profile_text}</p>"
        f"<table>"
        f"<tr><td>盈利回合</td><td>{profile.profitable_roundtrips} / {profile.total_roundtrips}</td></tr>"
        f"<tr><td>典型持仓(中位数/p75)</td><td>{profile.typical_holding_days[0]:.1f} / {profile.typical_holding_days[1]:.1f} 天</td></tr>"
        f"<tr><td>提取规则数</td><td>{rules_count}</td></tr>"
        f"<tr><td>时间跨度</td><td>{profile.date_range[0][:10]} ~ {profile.date_range[1][:10]}</td></tr>"
        f"</table>"
    )


def _section_behavior(behavior: dict[str, Any]) -> str:
    if "error" in behavior:
        return f"<p>{behavior['error']}</p>"
    rows = []
    labels = {
        "disposition_effect": "处置效应(拿亏单更久)",
        "overtrading": "过度交易",
        "chasing_momentum": "追涨",
        "anchoring": "锚定",
    }
    for key, label in labels.items():
        item = behavior.get(key) or {}
        sev = item.get("severity", "low")
        emoji = {"high": "🔴", "medium": "🟠", "low": "🟢"}.get(sev, "🟢")
        rows.append(
            f"<tr><td>{emoji} {label}</td><td>{sev}</td><td>{item.get('evidence', '')}</td></tr>"
        )
    return f"<table>{''.join(rows)}</table>"


def _section_rules(profile: ShadowProfile) -> str:
    cards = []
    for r in profile.rules:
        cards.append(
            f'<div class="rule-card">'
            f"<b>{r.rule_id}</b> {r.human_text}"
            f'<div class="meta">支撑 {r.support_count} 笔盈利回合 · 覆盖率 {r.coverage_rate:.0%} · '
            f"持仓 {r.holding_days_range[0]}-{r.holding_days_range[1]} 天 · 样本 {', '.join(r.sample_trades[:2])}</div>"
            f"</div>"
        )
    return "".join(cards)


def _section_attribution(result: ShadowBacktestResult) -> str:
    a: AttributionBreakdown = result.attribution
    rows = [
        ("影子 PnL(符合规则的真实回合)", result.shadow_total_pnl),
        ("真实 PnL(全部回合)", result.real_total_pnl),
        ("差值(影子 − 真实)", result.delta_pnl),
        ("情绪单损失(违反规则)", a.noise_trades_pnl),
        ("过早离场机会成本", a.early_exit_pnl),
        ("过晚离场放大损失", a.late_exit_pnl),
        ("过度交易拖累", a.overtrading_pnl),
        ("错过信号(残差)", a.missed_signals_pnl),
    ]
    tr = "".join(
        f"<tr><td>{label}</td><td class='{'pos' if v >= 0 else 'neg'}'>{v:+,.0f}</td></tr>"
        for label, v in rows
    )
    return f"<table>{tr}</table>"


def _section_counterfactual(result: ShadowBacktestResult) -> str:
    trades = result.attribution.counterfactual_trades
    if not trades:
        return "<p>无显著反事实交易。</p>"
    rows = []
    for t in trades:
        reason = _REASON_LABELS.get(t.get("reason", ""), t.get("reason", ""))
        rows.append(
            f"<tr><td>{t['symbol']}</td><td>{t['buy_dt'][:10]} → {t['sell_dt'][:10]}</td>"
            f"<td>{t['hold_days']:.0f} 天</td><td class='{'pos' if t['pnl'] >= 0 else 'neg'}'>{t['pnl']:+,.0f}</td>"
            f"<td>{reason}</td><td class='{'pos' if t['impact'] >= 0 else 'neg'}'>{t['impact']:+,.0f}</td></tr>"
        )
    return (
        "<table><tr><th>代码</th><th>区间</th><th>持仓</th><th>真实 PnL</th>"
        f"<th>问题</th><th>影响</th></tr>{''.join(rows)}</table>"
    )


def _section_disclaimer() -> str:
    return (
        "<p>⚠️ 本报告仅为交易行为研究输出,不构成任何投资建议。"
        "影子策略是从你自己的历史交易中提炼的统计画像,不代表未来收益。</p>"
    )


def _render_html(*, title: str, sections: list[tuple[str, str]], shadow_id: str) -> str:
    body = "".join(
        f"<h2>{h}</h2>\n{s}" for h, s in sections
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
          max-width: 720px; margin: 0 auto; padding: 16px; color: #1a1a1a; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 16px; margin-top: 28px; border-left: 4px solid #e4573d;
        padding-left: 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  td, th {{ border: 1px solid #e5e5e5; padding: 6px 8px; text-align: left; }}
  th {{ background: #f5f5f5; }}
  .rule-card {{ border: 1px solid #e5e5e5; border-radius: 6px; padding: 8px 12px;
                margin: 8px 0; }}
  .rule-card .meta {{ color: #888; font-size: 12px; margin-top: 4px; }}
  .pos {{ color: #d33; }} .neg {{ color: #0a7d33; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta" style="color:#888;font-size:12px">shadow_id: {shadow_id} · 生成时间 {_now_str()}</p>
{body}
</body>
</html>
"""


def _now_str() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M")
