"""D3 AI 合规护栏 (2026-09-10): 面向用户的 LLM 提示词统一带"不构成投资建议"护栏。

- chat 系统提示词自 2026-08-14 已有合规声明(chat.py);
- 本轮把场景 Agent(经 build_system_prompt)与 3 个直接提示词点位(insights×2 / dashboard curate)
  统一接上 with_compliance, 并加源扫描棘轮防止后续新增提示词漏挂。
"""
from __future__ import annotations

from pathlib import Path

from src.core.ai_client import COMPLIANCE_CLAUSE, build_system_prompt, with_compliance


def test_with_compliance_appends_clause():
    out = with_compliance("你是A股分析师。")
    assert "不构成投资建议" in out
    assert "不提供个性化投资建议" in out or "个性化投资建议" in out
    assert out.startswith("你是A股分析师。")


def test_with_compliance_idempotent():
    once = with_compliance("你是A股分析师。")
    twice = with_compliance(once)
    assert twice == once  # 已含不重复追加
    assert twice.count("合规护栏") == 1


def test_with_compliance_empty_safe():
    assert with_compliance("") == ""
    assert with_compliance(None) is None  # type: ignore[arg-type]


def test_build_system_prompt_includes_clause():
    """所有经场景入口组装的 Agent 提示词都应带护栏(无 db/无画像路径)。"""
    out = build_system_prompt(None, "reports", "你是一名报告撰写员。", None)
    assert "你是一名报告撰写员。" in out
    assert "不构成投资建议" in out
    assert COMPLIANCE_CLAUSE.strip()[:8] in out


def test_direct_prompt_sites_wrapped():
    """源扫描棘轮: 已知 LLM 直接提示词点位必须经 with_compliance 包装(防新增漏挂)。"""
    root = Path(__file__).resolve().parents[1]
    for rel in ("src/web/api/insights.py", "src/web/api/dashboard.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "with_compliance" in text, f"{rel} 的 LLM 提示词未接合规护栏"
