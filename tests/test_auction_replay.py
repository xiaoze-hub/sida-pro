"""竞价盘后回放（2026-10-22）。

盘后打开竞价页，实时源没数据 → 旧行为只回一句"无当日竞价快照"，而库里存着
上一个交易日 200+ 条。这条用例钉住"回放归档 + 明确标非实时"。
"""

from __future__ import annotations

from src.core import auction_pool as ap


class _Rec(dict):
    def __getattr__(self, k):
        return self.get(k)


def test_replays_latest_archive_when_live_is_empty(monkeypatch):
    monkeypatch.setattr(ap, "fetch_auction_anomaly", lambda market: [])
    monkeypatch.setattr(
        ap,
        "_load_latest_archived",
        lambda limit=500: ("2026-10-20", [_Rec(symbol="600111", name="北方稀土", gap_pct=3.2)]),
    )
    out = ap.fetch_auction_payload("CN")
    assert out["mode"] == "replay"
    assert out["replay_date"] == "2026-10-20"
    assert out["available"] is True and out["count"] == 1
    assert out["is_live"] is False
    assert "回放" in out["reason_text"] and "非实时" in out["reason_text"]


def test_empty_when_no_archive_either(monkeypatch):
    monkeypatch.setattr(ap, "fetch_auction_anomaly", lambda market: [])
    monkeypatch.setattr(ap, "_load_latest_archived", lambda limit=500: (None, []))
    out = ap.fetch_auction_payload("CN")
    assert out["mode"] == "empty"
    assert out["available"] is False
    assert out["count"] == 0
    assert out["reason"] in {"non_auction_session", "non_trading_day", "no_anomaly", "source_unavailable"}


def test_live_records_keep_mode_live_or_snapshot(monkeypatch):
    monkeypatch.setattr(ap, "fetch_auction_anomaly", lambda market: [
        {"symbol": "600111", "name": "北方稀土", "gap_pct": 1.0},
    ])
    called = {"n": 0}
    monkeypatch.setattr(ap, "_load_latest_archived", lambda limit=500: called.__setitem__("n", 1) or (None, []))
    out = ap.fetch_auction_payload("CN")
    assert out["mode"] in {"live", "snapshot"}
    assert called["n"] == 0, "实时有数据时不该去读归档"
