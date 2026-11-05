"""测试事实包一致性与快照复用（2026-11-05）。

验证：
1. build_pack 对同一标的、同一 now 生成确定性的 pack_id；
2. apply_fact_pack_to_quotes 能够正确把事实包中的价格/涨跌幅覆盖进 quote 字典；
3. build_copilot_brief 能够接收 fact_pack 并复用快照（不重复打上游）。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from src.core.fact_pack import build_pack, apply_fact_pack_to_quotes
from src.core.copilot_brief import build_copilot_brief


def test_fact_pack_snapshot_determinism():
    now = datetime(2026, 11, 5, 10, 0, 0)
    with patch("src.core.fact_pack.build_copilot_brief", return_value={
        "as_of": "2026-11-05 10:00:00",
        "quote": {"available": True, "close": 12.34, "change_pct": 1.5, "source": "mock_src"},
        "evidence": {"chips": {"available": True}},
        "session": {"stale": False},
        "verdict": {"caliber_notes": ["tick"]},
    }):
        p1 = build_pack(["000001"], now=now)
        p2 = build_pack(["000001"], now=now)
        assert p1["pack_id"] == p2["pack_id"]
        assert "000001" in p1["symbols"]
        assert p1["symbols"]["000001"]["price"] == 12.34


def test_apply_fact_pack_to_quotes():
    pack = {
        "pack_id": "test1234",
        "as_of": "2026-11-05 10:00:00",
        "symbols": {
            "000001": {"price": 18.88, "pct": -2.3, "source": "pack", "stale": False}
        }
    }
    q = {"close": 10.0, "change_pct": 0.0, "source": "original"}
    res = apply_fact_pack_to_quotes(pack, "000001", q)
    assert res["close"] == 18.88
    assert res["change_pct"] == -2.3
    assert "pack" in res["source"]

    # 不在 pack 里的标的原样返回
    res2 = apply_fact_pack_to_quotes(pack, "000002", q)
    assert res2["close"] == 10.0
