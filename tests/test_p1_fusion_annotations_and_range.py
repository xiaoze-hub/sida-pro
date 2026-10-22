"""P1 原生融合: 情景矩阵自动提议图上标注 + K 线圈选区间实算(2026-10-22)。

两条都要"真数字", 不许编:
  1. 情景矩阵算出支撑/阻力 → 必须能变成**待确认**的图上标注, 且**反复问同一只票
     不会把待确认列表刷成一堆重复行**(那是把用户当垃圾桶)。
  2. 用户圈选一段区间问「这段为什么跌/涨了多少」 → 涨跌幅/高低点/最大回撤/量比
     必须在**这一段**上实算; 区间里只有 1 根日线就老实写「不足」, 不拿别的窗口顶替。
"""
from __future__ import annotations

import pytest

from src.core.trend_profile import build_range_summary, compute_range_summary
from src.db.models import CopilotAction, KlineAnnotation
from src.db.session import SessionLocal
from src.harness import actions as A
from src.harness.viewstate import append_view_state_block, extract_selection_range

UID = "d3f1a0c2-77a9-4b1e-9c4d-p1fusion001"
SYMBOL = "600519"

#: 情景矩阵真实结构的最小切片（levels 由 MA/筹码带/区间高低算出）
MATRIX = {
    "available": True,
    "levels": {"price": 100.0, "support": 95.5, "resist": 108.25, "ma20": 108.25, "ma60": 95.5},
    "scenarios": [],
}


def _db():
    return SessionLocal()


def _cleanup(db) -> None:
    db.rollback()
    db.query(CopilotAction).filter(CopilotAction.user_id == UID).delete()
    db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).delete()
    db.commit()


def _pending_annotations(db, symbol: str = SYMBOL) -> list[CopilotAction]:
    return (
        db.query(CopilotAction)
        .filter(
            CopilotAction.user_id == UID,
            CopilotAction.kind == "annotation",
            CopilotAction.status == "pending",
        )
        .all()
    )


# ---------------- 情景矩阵 → 图上标注 ----------------

def test_propose_creates_support_and_resist_pending_annotations():
    db = _db()
    try:
        _cleanup(db)
        out = A.propose_scenario_annotations(db, user_id=UID, symbol=SYMBOL, market="SH", matrix=MATRIX)

        assert [a["payload"]["label"] for a in out] == ["支撑位", "阻力位"]
        assert all(a["kind"] == "annotation" and a["status"] == "pending" for a in out)
        assert all(a["dedup"] is False for a in out)

        by_label = {a["payload"]["label"]: a["payload"] for a in out}
        assert by_label["支撑位"]["price"] == 95.5
        assert by_label["支撑位"]["color"] == "up"
        assert "MA60" in by_label["支撑位"]["note"]
        assert by_label["阻力位"]["price"] == 108.25
        assert by_label["阻力位"]["color"] == "down"
        assert "MA20" in by_label["阻力位"]["note"]
        assert by_label["支撑位"]["symbol"] == SYMBOL and by_label["支撑位"]["market"] == "SH"

        # 只提议不落图: 用户没点确认, K 线图层必须还是空的
        assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
        assert len(_pending_annotations(db)) == 2
    finally:
        _cleanup(db)
        db.close()


def test_propose_dedupes_pending_instead_of_spamming():
    """同一个 (user, symbol, label) 只留一条 pending —— 反复诊断不该刷屏。"""
    db = _db()
    try:
        _cleanup(db)
        A.propose_scenario_annotations(db, user_id=UID, symbol=SYMBOL, market="SH", matrix=MATRIX)
        again = A.propose_scenario_annotations(db, user_id=UID, symbol=SYMBOL, market="SH", matrix=MATRIX)

        assert len(again) == 2
        assert all(a["dedup"] is True for a in again)
        assert len(_pending_annotations(db)) == 2
        # 复用同一条记录(id 不变), 不是"删一条插一条"
        first_ids = {a["id"] for a in again}
        third = A.propose_scenario_annotations(db, user_id=UID, symbol=SYMBOL, market="SH", matrix=MATRIX)
        assert {a["id"] for a in third} == first_ids
    finally:
        _cleanup(db)
        db.close()


def test_propose_refreshes_price_in_place_when_level_moves():
    db = _db()
    try:
        _cleanup(db)
        A.propose_scenario_annotations(db, user_id=UID, symbol=SYMBOL, market="SH", matrix=MATRIX)
        moved = {"available": True, "levels": {"support": 96.0, "resist": 108.25}}
        out = A.propose_scenario_annotations(db, user_id=UID, symbol=SYMBOL, market="SH", matrix=moved)

        sup = next(a for a in out if a["payload"]["label"] == "支撑位")
        assert sup["dedup"] is True
        assert sup["payload"]["price"] == 96.0, "关键位变了必须原地刷新价格"
        assert len(_pending_annotations(db)) == 2, "刷新价格不能多出一条"
        rows = [r for r in _pending_annotations(db)]
        assert len(rows) == 2
    finally:
        _cleanup(db)
        db.close()


def test_propose_returns_empty_when_matrix_unavailable_or_levels_missing():
    db = _db()
    try:
        _cleanup(db)
        assert A.propose_scenario_annotations(
            db, user_id=UID, symbol=SYMBOL, matrix={"available": False, "reason": "日线不足"},
        ) == []
        assert A.propose_scenario_annotations(
            db, user_id=UID, symbol=SYMBOL, matrix={"available": True, "levels": {}},
        ) == []
        assert A.propose_scenario_annotations(db, user_id=UID, symbol="  ", matrix=MATRIX) == []
        # 非法关键位(负价/None/NaN)一律不画
        bad = {"available": True, "levels": {"support": -1, "resist": None}}
        assert A.propose_scenario_annotations(db, user_id=UID, symbol=SYMBOL, matrix=bad) == []
        assert _pending_annotations(db) == []
    finally:
        _cleanup(db)
        db.close()


def test_propose_builds_matrix_when_not_given(monkeypatch):
    """matrix 缺省时自己算 —— 调用方(诊断流程)不用重复取数。"""
    import src.core.scenario_engine as SE

    called = {}

    def _fake_build_matrix(db, symbol, market="CN"):
        called["args"] = (symbol, market)
        return MATRIX

    monkeypatch.setattr(SE, "build_matrix", _fake_build_matrix)
    db = _db()
    try:
        _cleanup(db)
        out = A.propose_scenario_annotations(db, user_id=UID, symbol=SYMBOL, market="SH")
        assert called["args"] == (SYMBOL, "SH")
        assert len(out) == 2
    finally:
        _cleanup(db)
        db.close()


# ---------------- 圈选区间实算 ----------------

#: 区间内故意造一段"冲高→崩→反抽": 峰 110 → 谷 88 是 -20.00% 回撤
_RANGE_BARS = [
    {"date": "2026-08-28", "open": 99, "high": 101, "low": 98, "close": 100.0, "volume": 1000},
    {"date": "2026-09-15", "open": 100, "high": 101, "low": 99, "close": 100.0, "volume": 1000},
    {"date": "2026-09-16", "open": 100, "high": 112, "low": 99.5, "close": 110.0, "volume": 2000},
    {"date": "2026-09-17", "open": 110, "high": 110, "low": 104, "close": 105.0, "volume": 1500},
    {"date": "2026-09-18", "open": 105, "high": 106, "low": 87, "close": 88.0, "volume": 3000},
    {"date": "2026-09-21", "open": 90, "high": 97, "low": 94, "close": 96.0, "volume": 1200},
    {"date": "2026-10-20", "open": 118, "high": 121, "low": 117, "close": 120.0, "volume": 500},
]


def test_compute_range_summary_is_exact_on_the_slice():
    m = compute_range_summary(_RANGE_BARS, "2026-09-15", "2026-09-21")

    assert m["available"] is True
    assert m["bar_count"] == 5, "只算圈选内的 5 根, 不许把区间外的 08-28/10-20 混进来"
    assert m["start_date"] == "2026-09-15" and m["end_date"] == "2026-09-21"
    assert m["start_close"] == 100.0 and m["end_close"] == 96.0
    assert m["change_pct"] == -4.0                       # (96-100)/100
    assert m["high"] == 112.0 and m["high_date"] == "2026-09-16"
    assert m["low"] == 87.0 and m["low_date"] == "2026-09-18"
    assert m["max_drawdown_pct"] == -20.0                # 110 → 88
    assert m["avg_volume"] == 1740.0                     # (1000+2000+1500+3000+1200)/5
    assert m["vol_ratio"] == pytest.approx(1.19, abs=0.01)   # 1740 / 全样本近 20 日均量


def test_compute_range_summary_reversed_and_iso_dates():
    """反着拖 + 带时分秒的 ISO 串也要能算（前端两种格式都推过）。"""
    fwd = compute_range_summary(_RANGE_BARS, "2026-09-15", "2026-09-21")
    rev = compute_range_summary(_RANGE_BARS, "2026-09-21T15:00:00", "2026-09-15T09:30:00")
    assert rev["available"] is True
    assert rev["change_pct"] == fwd["change_pct"]
    assert (rev["start_date"], rev["end_date"]) == ("2026-09-15", "2026-09-21")


def test_compute_range_summary_refuses_thin_slice_without_invention():
    m = compute_range_summary(_RANGE_BARS, "2026-09-16", "2026-09-16")   # 只有 1 根
    assert m["available"] is False
    assert m["reason"] == "圈选区间有效日线不足 2 根"
    assert "change_pct" not in m, "样本不足时不许给涨跌幅(哪怕是 0)"

    empty = compute_range_summary(_RANGE_BARS, "2020-01-01", "2020-02-01")
    assert empty["available"] is False and "不足 2 根" in empty["reason"]


def test_build_range_summary_renders_two_line_evidence(monkeypatch):
    import src.db.klines_repo as KR

    monkeypatch.setattr(KR, "load_qfq_bars", lambda *a, **k: list(_RANGE_BARS))
    text = build_range_summary(SYMBOL, "2026-09-15", "2026-09-21", "SH")

    assert len(text.splitlines()) == 2, "进 system prompt 的证据串只占两行"
    assert "圈选区间 2026-09-15~2026-09-21" in text
    assert "区间涨跌 -4.00%" in text
    assert "高 112.0（2026-09-16）" in text and "低 87.0（2026-09-18）" in text
    assert "最大回撤 -20.00%" in text
    assert "[数据源: PG 前复权日线 · 圈选区间实算]" in text


def test_build_range_summary_thin_slice_says_so(monkeypatch):
    import src.db.klines_repo as KR

    monkeypatch.setattr(KR, "load_qfq_bars", lambda *a, **k: list(_RANGE_BARS))
    text = build_range_summary(SYMBOL, "2026-09-16", "2026-09-16", "SH")
    assert text == "[圈选区间] 有效日线不足 2 根——不做推断。"


def test_build_range_summary_degrades_when_db_fails(monkeypatch):
    import src.db.klines_repo as KR

    def _boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(KR, "load_qfq_bars", _boom)
    text = build_range_summary(SYMBOL, "2026-09-15", "2026-09-21")
    assert "不做推断" in text and "RuntimeError" in text


# ---------------- viewstate 注入 ----------------

def test_extract_selection_range_accepts_both_shapes():
    assert extract_selection_range({"selection": {"from": "2026-09-15", "to": "2026-10-15"}}) == (
        "2026-09-15", "2026-10-15")
    assert extract_selection_range({"selection": {"start": "2026-10-15", "end": "2026-09-15"}}) == (
        "2026-09-15", "2026-10-15")
    assert extract_selection_range(
        {"selection": {"from": "2026-09-15T00:00:00", "to": "2026-10-15T00:00:00"}}
    ) == ("2026-09-15", "2026-10-15")
    assert extract_selection_range({"selection": {"from": "2026-09-15"}}) is None
    assert extract_selection_range({"selection": {}}) is None
    assert extract_selection_range({"selection": None}) is None
    assert extract_selection_range(None) is None


def test_append_view_state_block_injects_range_summary(monkeypatch):
    """圈选了一段 → 区间实算必须自动进 system prompt(用户不用再问'哪一段')。"""
    import src.db.klines_repo as KR
    import src.harness.viewstate as VS

    monkeypatch.setattr(KR, "load_qfq_bars", lambda *a, **k: list(_RANGE_BARS))
    monkeypatch.setattr(VS, "load_view_state", lambda *a, **k: {
        "page": "kline", "symbol": SYMBOL, "market": "SH", "period": "1d",
        "selection": {"from": "2026-09-15", "to": "2026-09-21"},
    })
    messages = [{"role": "system", "content": "BASE"}, {"role": "user", "content": "这段为什么跌"}]
    append_view_state_block(messages, UID, 1)

    content = messages[0]["content"]
    assert content.startswith("BASE")
    assert "用户圈选 2026-09-15~2026-09-21" in content      # 原有锚点保留
    assert "区间涨跌 -4.00%" in content
    assert "最大回撤 -20.00%" in content
    assert "圈选区间实算" in content
    assert messages[1]["content"] == "这段为什么跌", "只动 system 消息"


def test_append_view_state_block_skips_range_without_selection(monkeypatch):
    import src.db.klines_repo as KR
    import src.harness.viewstate as VS

    called = {"n": 0}

    def _loader(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(KR, "load_qfq_bars", _loader)
    monkeypatch.setattr(VS, "load_view_state", lambda *a, **k: {
        "page": "kline", "symbol": SYMBOL, "market": "SH",
    })
    messages = [{"role": "system", "content": "BASE"}]
    append_view_state_block(messages, UID, 1)

    assert "圈选区间" not in messages[0]["content"]
    assert called["n"] == 0, "没有圈选就不该去读日线(别为不存在的需求查库)"


def test_append_view_state_block_survives_db_failure(monkeypatch):
    """读日线炸了也必须只降级——视图锚点照注入, 绝不把对话拖死。"""
    import src.db.klines_repo as KR
    import src.harness.viewstate as VS

    def _boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(KR, "load_qfq_bars", _boom)
    monkeypatch.setattr(VS, "load_view_state", lambda *a, **k: {
        "page": "kline", "symbol": SYMBOL, "selection": {"from": "2026-09-15", "to": "2026-09-21"},
    })
    messages = [{"role": "system", "content": "BASE"}]
    append_view_state_block(messages, UID, 1)   # 不抛

    assert "K线页" in messages[0]["content"]
