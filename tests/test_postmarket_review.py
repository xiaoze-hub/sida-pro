# -*- coding: utf-8 -*-
"""盘后复盘 单测: src/core/postmarket_review.py

覆盖:
  - 委托号级拆单簇: 五条件(委托号连续/同价/密集/每笔<30万/总额>=30万)
    / 笔数不足 / 单笔超阈值(明摆着的大单) / seq 断裂 / 方向混杂
  - 撤单率: 正常 / 分母 0 → None
  - 主动买卖比: 正常 / 卖额 0 → None
  - dark_review_from_tck: 无文件 → available=False; 完整复盘(明盘+暗盘+主力净额)
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import postmarket_review as pr  # noqa: E402


# ---------------------------------------------------------------------------
# ① 委托号级拆单簇
# ---------------------------------------------------------------------------


def _order(seq, t_ms, price, vol, a28=0, a32=0):
    """构造 tag `00` 委托: 被成交的委托 a28 或 a32 非零。"""
    return {"seq": seq, "t": t_ms, "price": price, "vol": vol,
            "amt": price * vol, "a28": a28, "a32": a32}


def test_split_cluster_detected_via_orders():
    """6 笔连续委托号、同价、密集、每笔 8.85 万 < 30 万, 总额 53 万 → 拆单簇。"""
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 8850, a28=1) for i in range(6)]
    ev = pr.split_clusters_from_orders(orders, "2026-09-01")
    assert len(ev) == 1
    assert ev[0]["side"] == "卖"          # a28 非零 = 被主动买扫 = 挂卖
    assert ev[0]["count"] == 6
    assert ev[0]["amount"] == pytest.approx(10.00 * 8850 * 6, abs=1)


def test_split_cluster_buy_side_via_a32():
    """a32 非零 = 被主动卖砸掉 = 挂买(拆买单)。"""
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 8850, a32=1) for i in range(6)]
    ev = pr.split_clusters_from_orders(orders, "2026-09-01")
    assert ev[0]["side"] == "买"


def test_split_cluster_too_few():
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 8850, a28=1) for i in range(4)]
    assert pr.split_clusters_from_orders(orders, "2026-09-01") == []


def test_split_cluster_single_over_threshold():
    """单笔 >= 30万 → 明摆着的大单, 不算拆单。"""
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 40000, a28=1) for i in range(6)]
    assert pr.split_clusters_from_orders(orders, "2026-09-01") == []


def test_split_cluster_seq_break():
    """委托号 gap > 3 → 断簇。"""
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 8850, a28=1) for i in range(3)]
    orders += [_order(2000 + i, 143004000 + i * 1000, 10.00, 8850, a28=1) for i in range(3)]  # gap=1000
    assert pr.split_clusters_from_orders(orders, "2026-09-01") == []


def test_split_cluster_price_gap():
    """价格跳超过 1 价位 → 断簇。"""
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 8850, a28=1) for i in range(3)]
    orders += [_order(1003 + i, 143003000 + i * 1000, 10.50, 8850, a28=1) for i in range(3)]
    assert pr.split_clusters_from_orders(orders, "2026-09-01") == []


def test_split_cluster_side_mixed_breaks():
    """方向混杂(买+卖) → 断簇。"""
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 8850, a28=1) for i in range(3)]
    orders += [_order(1003 + i, 143003000 + i * 1000, 10.00, 8850, a32=1) for i in range(3)]
    assert pr.split_clusters_from_orders(orders, "2026-09-01") == []


def test_split_cluster_ignores_unfilled_orders():
    """未被成交的委托(a28/a32 均 0)不参与拆单识别。"""
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 8850) for i in range(6)]  # 无 a28/a32
    assert pr.split_clusters_from_orders(orders, "2026-09-01") == []


def test_split_cluster_empty():
    assert pr.split_clusters_from_orders([], "2026-09-01") == []


# ---------------------------------------------------------------------------
# ② 撤单率 / 主动买卖比
# ---------------------------------------------------------------------------


def test_cancel_rate_normal():
    cancels = [{"vol": 3000}, {"vol": 2000}]
    trades = [{"vol": 5000}, {"vol": 10000}]
    assert pr.cancel_rate(cancels, trades) == pytest.approx(5000 / 20000, abs=1e-6)


def test_cancel_rate_zero_denominator():
    assert pr.cancel_rate([], []) is None


def test_cancel_rate_ignores_non_dict():
    cancels = [{"vol": 1000}, None, "x"]
    trades = [{"vol": 9000}]
    assert pr.cancel_rate(cancels, trades) == pytest.approx(0.1, abs=1e-6)


def test_active_passive_ratio_normal():
    trades = [{"amt": 600, "dir": "B"}, {"amt": 300, "dir": "S"}]
    assert pr.active_passive_ratio(trades) == pytest.approx(2.0)


def test_active_passive_ratio_no_sell():
    assert pr.active_passive_ratio([{"amt": 600, "dir": "B"}]) is None


def test_active_passive_ratio_empty():
    assert pr.active_passive_ratio([]) is None


# ---------------------------------------------------------------------------
# ③ dark_review_from_tck
# ---------------------------------------------------------------------------


def test_review_no_tck_file(monkeypatch):
    monkeypatch.delenv("PANWATCH_TCK_DIR", raising=False)
    r = pr.dark_review_from_tck("000977", "2026-09-01")
    assert r["available"] is False
    assert "无 .tck" in (r["note"] or "")


def test_review_full(monkeypatch, tmp_path):
    """完整复盘: 明盘(单笔>30万) + 暗盘(拆单簇) + 主力净额 + 撤单率 + 主动买卖比。"""
    (tmp_path / "sz000977_20260901.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))

    # 明盘: 1 笔 40万 买
    trades = [{"dir": "B", "amt": 400000.0, "vol": 40000, "price": 10.0}]
    # 暗盘: 6 笔 8.85万 挂卖被主动买扫(拆卖单), 总额 53万
    orders = [_order(1000 + i, 143000000 + i * 1000, 10.00, 8850, a28=1) for i in range(6)]
    cancels = [{"vol": 10000}]

    import src.core.tdx_tick_parser as ttp
    monkeypatch.setattr(ttp, "parse_tck", lambda p: (trades, orders, cancels))

    r = pr.dark_review_from_tck("000977", "2026-09-01")
    assert r["available"] is True
    # 明盘净额 = +40万
    assert r["ming"]["net"] == pytest.approx(400000.0)
    assert r["ming"]["count"] == 1
    # 暗盘净额 = -53万(拆卖单 = 卖出)
    assert r["dark"]["net"] == pytest.approx(-531000.0, abs=1)
    assert r["dark"]["count"] == 6
    # 主力净额 = 明 + 暗
    assert r["main_net"] == pytest.approx(400000.0 - 531000.0, abs=1)
    # 撤单率 = 10000 / (10000 + 40000)
    assert r["cancel_rate"] == pytest.approx(10000 / 50000, abs=1e-6)
    # 主动买卖比: 只有买没有卖 → None
    assert r["active_passive_ratio"] is None
    # 拆单簇明细
    assert len(r["clusters"]) == 1


def test_review_empty_trades(monkeypatch, tmp_path):
    (tmp_path / "sz000977.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    import src.core.tdx_tick_parser as ttp
    monkeypatch.setattr(ttp, "parse_tck", lambda p: ([], [], []))
    r = pr.dark_review_from_tck("000977")
    assert r["available"] is False
    assert "无成交记录" in (r["note"] or "")


def test_review_parse_error(monkeypatch, tmp_path):
    (tmp_path / "sz000977.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    import src.core.tdx_tick_parser as ttp
    monkeypatch.setattr(ttp, "parse_tck", lambda p: (_ for _ in ()).throw(ValueError("bad")))
    r = pr.dark_review_from_tck("000977")
    assert r["available"] is False
    assert "bad" in (r["note"] or "")
