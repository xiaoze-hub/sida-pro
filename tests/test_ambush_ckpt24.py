"""受益落地 + 行情快照 prompt 测试(mock 外部, 不碰外网/LLM)。"""

from __future__ import annotations

from src.core import beneficiary_resolver as R
from src.core.event_catalyst_engine import build_catalyst_prompt


def test_resolve_exact_and_code(monkeypatch):
    import src.core.beneficiary_resolver as M

    monkeypatch.setattr(M, "_stock_index", lambda: {"神剑股份": "002361", "贵州茅台": "600519"})
    out = R.resolve_beneficiaries(["神剑股份", " Peg 600519 顺带 ", "不存在的名字XYZ"])
    by_name = {o["name"]: o for o in out}
    assert by_name["神剑股份"]["symbol"] == "002361"
    assert by_name["神剑股份"]["via"] == "exact"
    code_hit = [o for o in out if o["via"] == "code"]
    assert code_hit and code_hit[0]["symbol"] == "600519"
    assert not any(o["name"] == "不存在的名字XYZ" for o in out)


def test_resolve_fuzzy_low_confidence(monkeypatch):
    import src.core.beneficiary_resolver as M

    monkeypatch.setattr(M, "_stock_index", lambda: {"神剑股份": "002361"})
    monkeypatch.setattr("src.core.sector_filter.resolve_sector_codes", lambda name: [])
    out = R.resolve_beneficiaries(["神剑股分"])  # 一字之差
    assert out and out[0]["via"] == "fuzzy" and out[0]["confidence"] == "低"


def test_prompt_with_market_snapshot():
    _, user = build_catalyst_prompt("002361", ["公告A"], {"change_pct": 3.5, "volume_ratio": 2.1})
    assert "change_pct" in user and "3.5" in user
    _, user2 = build_catalyst_prompt("002361", ["公告A"], None)
    assert "行情快照" not in user2
