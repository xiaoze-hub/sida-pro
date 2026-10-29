"""情景矩阵工具级组装（2026-10-23）：成本锚定只在真有持仓时出现 + 附加段失败隔离。

守两件事：
1. **不打印噪音**：诊断陌生票（无持仓）时不能出现「无持仓成本」这类文案 —— 那是拿行数凑数。
2. **失败隔离**：成本锚定 / 相似形态任何一段炸了，矩阵正文必须照旧返回 ——
   附加信息是增强项，不能因为它把整段情景搞没。
"""
from types import SimpleNamespace

import pytest

import src.core.scenario_compose as sc


class _Q:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    def join(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        self._sink.append(1)
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _DB:
    """按模型类名返回预设行的最小 DB。"""

    def __init__(self, by_model=None):
        self.by_model = by_model or {}
        self.filters: list[int] = []

    def query(self, *models, **_k):
        key = models[0].__name__ if models else ""
        rows = self.by_model.get(key, [])
        if len(models) > 1:                      # Position + Stock join
            rows = [(p, self.by_model.get("Stock", [None])[0]) for p in rows]
        return _Q(rows, self.filters)


def _matrix():
    return {
        "available": True,
        "levels": {"price": 35.0, "resist": 36.5, "support": 33.2,
                   "ma20": 35.4, "ma60": 33.0, "band_hi": 37.0, "band_lo": 32.0, "vwap": 34.9},
        "context": {"position": 52.0, "rs20": 1.2, "momentum_20": -3.9, "vol_ratio": 1.1},
        "scenarios": [{"name": "走强", "trigger": "t", "target": 37.0, "response": "r", "invalid": "i"}],
        "disclaimer": "不预测方向",
        "price": 35.0,
        "as_of": "2026-10-22",
    }


# ── 1. 成本来源 ─────────────────────────────────────────────────────────────


def test_owner_cost_from_real_position():
    # 四审: 只有有效正持仓才锚定 → 数量是必要条件。
    pos = SimpleNamespace(cost_price=34.2, quantity=100)
    db = _DB({"Position": [pos], "Stock": [SimpleNamespace(symbol="600111")]})
    assert sc.owner_cost(db, SimpleNamespace(id=1), "600111") == (34.2, None)


def test_owner_cost_from_paper_position_keeps_stop():
    paper = SimpleNamespace(entry_price=35.8, stop_loss=33.0)
    db = _DB({"PaperTradingPosition": [paper]})
    assert sc.owner_cost(db, SimpleNamespace(id=1), "002361") == (35.8, 33.0)


def test_owner_cost_none_when_not_held():
    db = _DB({})
    assert sc.owner_cost(db, SimpleNamespace(id=1), "600111") == (None, None)
    assert sc.owner_cost(None, None, "600111") == (None, None), "没有 db 不许抛"


def test_owner_cost_survives_db_failure():
    class _Boom:
        def query(self, *_a, **_k):
            raise RuntimeError("db down")

    assert sc.owner_cost(_Boom(), None, "600111") == (None, None)


# ── 2. 组装 ─────────────────────────────────────────────────────────────────


@pytest.fixture
def patched(monkeypatch):
    """把矩阵/形态/成本锚定全部替身化，专注验证「拼装与噪音」两件事。"""
    monkeypatch.setattr("src.core.scenario_engine.build_matrix", lambda db, s, m: _matrix())
    monkeypatch.setattr("src.core.scenario_engine.render", lambda mx: "关键位：阻力 36.5｜支撑 33.2")
    monkeypatch.setattr("src.core.analog_finder.build", lambda s, m: "[相似形态] 样本不足（n=3，至少 10）——不做推断。")
    return monkeypatch


def test_no_cost_block_when_user_does_not_hold(patched):
    out = sc.build_for_tool(_DB({}), "600111", "CN", user=SimpleNamespace(id=1))
    assert "关键位：阻力 36.5" in out
    assert "持仓成本锚定" not in out and "成本锚定" not in out, "没持仓不该打印成本噪音"
    assert "相似形态" in out


def test_cost_block_appears_when_held(patched):
    db = _DB({"Position": [SimpleNamespace(cost_price=34.2, quantity=100)],
              "Stock": [SimpleNamespace(symbol="600111")]})
    out = sc.build_for_tool(db, "600111", "CN", user=SimpleNamespace(id=1))
    assert "[持仓成本锚定]" in out and "34.20" in out
    assert "保本位" in out


def test_extra_block_failure_is_isolated(patched):
    patched.setattr("src.core.scenario_cost_anchor.enrich_with_cost",
                    lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    db = _DB({"Position": [SimpleNamespace(cost_price=34.2, quantity=100)],
              "Stock": [SimpleNamespace(symbol="600111")]})
    out = sc.build_for_tool(db, "600111", "CN", user=SimpleNamespace(id=1))
    assert "关键位：阻力 36.5" in out, "附加段炸了不许把矩阵正文吞掉"
    assert "持仓成本锚定" not in out


def test_analog_failure_is_isolated(patched):
    patched.setattr("src.core.analog_finder.build",
                    lambda s, m: (_ for _ in ()).throw(RuntimeError("boom")))
    out = sc.build_for_tool(_DB({}), "600111", "CN", user=None)
    assert "关键位：阻力 36.5" in out and "相似形态" not in out


def test_matrix_unavailable_still_returns_reason(patched):
    patched.setattr("src.core.scenario_engine.build_matrix",
                    lambda db, s, m: {"available": False, "reason": "关键位不足"})
    patched.setattr("src.core.scenario_engine.render", lambda mx: "[情景矩阵] 关键位不足——不硬编情景。")
    out = sc.build_for_tool(_DB({}), "600111", "CN", user=None)
    assert "不硬编情景" in out
    assert "持仓成本锚定" not in out, "矩阵都没算出来，不许再挂成本锚定"


def test_empty_symbol_rejected():
    assert "请提供股票代码" in sc.build_for_tool(_DB({}), "", "CN")


# ── 3. 脏成本与多账户(2026-10-29 三审 R6) ─────────────────────────────────


def test_owner_cost_ignores_non_finite_cost():
    """历史脏成本(NaN/Inf)不能当成本锚 —— `bool(nan)` 为 True 会把它一路带进文案。"""
    db = _DB({"Position": [SimpleNamespace(cost_price=float("nan"), quantity=100),
                           SimpleNamespace(cost_price=float("inf"), quantity=100)],
              "Stock": [SimpleNamespace(symbol="600111")]})
    detail = sc.owner_cost_detail(db, SimpleNamespace(id=1), "600111")
    assert detail.cost is None, "NaN/Inf 成本必须当未知"
    assert detail.method == "unknown"
    assert detail.unpriced == 2
    assert sc.owner_cost(db, SimpleNamespace(id=1), "600111") == (None, None)


def test_owner_cost_multi_account_weights_instead_of_first():
    """多账户成本不一致 → 按持仓量加权, 不再"随便取 first"。"""
    db = _DB({"Position": [
        SimpleNamespace(cost_price=10.0, quantity=100, account_id=1),
        SimpleNamespace(cost_price=20.0, quantity=300, account_id=2),
    ], "Stock": [SimpleNamespace(symbol="600111")]})
    detail = sc.owner_cost_detail(db, SimpleNamespace(id=1), "600111")
    assert detail.method == "weighted"
    assert detail.cost == pytest.approx((10.0 * 100 + 20.0 * 300) / 400)
    assert [a["account_id"] for a in detail.accounts] == [1, 2]
    assert sc.owner_cost(db, SimpleNamespace(id=1), "600111")[0] == pytest.approx(17.5)


def test_owner_cost_multi_account_same_cost_is_unambiguous():
    db = _DB({"Position": [
        SimpleNamespace(cost_price=12.5, quantity=100, account_id=1),
        SimpleNamespace(cost_price=12.5, quantity=200, account_id=2),
    ], "Stock": [SimpleNamespace(symbol="600111")]})
    detail = sc.owner_cost_detail(db, SimpleNamespace(id=1), "600111")
    assert detail.method == "single" and detail.cost == 12.5


def test_owner_cost_mixed_good_and_dirty_counts_unpriced():
    db = _DB({"Position": [
        SimpleNamespace(cost_price=15.0, quantity=100, account_id=1),
        SimpleNamespace(cost_price=float("nan"), quantity=100, account_id=2),
    ], "Stock": [SimpleNamespace(symbol="600111")]})
    detail = sc.owner_cost_detail(db, SimpleNamespace(id=1), "600111")
    assert detail.cost == 15.0
    assert detail.unpriced == 1, "脏行必须被计数(可解释), 而不是静默参与/静默丢弃"


def test_build_for_tool_explains_weighted_cost(patched):
    """多账户锚定必须在正文里说清"按持仓量加权 + 逐账户", 不能假装是某个账户的成本。"""
    db = _DB({"Position": [
        SimpleNamespace(cost_price=10.0, quantity=100, account_id=1),
        SimpleNamespace(cost_price=20.0, quantity=300, account_id=2),
    ], "Stock": [SimpleNamespace(symbol="600111")]})
    out = sc.build_for_tool(db, "600111", "CN", user=SimpleNamespace(id=1))
    assert "持仓量加权" in out
    assert "账户#1" in out and "账户#2" in out
    assert "不是某一个账户的原始成本" in out


def test_build_for_tool_reports_unknown_cost_instead_of_silence(patched):
    db = _DB({"Position": [SimpleNamespace(cost_price=float("nan"), quantity=100)],
              "Stock": [SimpleNamespace(symbol="600111")]})
    out = sc.build_for_tool(db, "600111", "CN", user=SimpleNamespace(id=1))
    assert "成本价无效或缺失" in out
    assert "持仓成本锚定" not in out, "成本未知时不许编一个锚点"
