"""启动早期信号读取/渲染测试（stub 假库，不连真 DB）。"""

try:
    from src.core import startup_signals as S
except ImportError:  # pragma: no cover
    from core import startup_signals as S  # type: ignore


class _Rows:
    def __init__(self, rows):
        self._r = rows

    def fetchall(self):
        return self._r


class _Db:
    """按 SQL 关键字返回不同候选行。"""

    def __init__(self, latest="2026-09-24", rows=None, base=None):
        self.latest, self.rows, self.base = latest, rows or [], base or []

    def execute(self, sql, params=None):
        q = str(sql)
        if "MAX(trade_date)" in q:
            return _Rows([(self.latest,)])
        if "AVG(hit_count)" in q:
            return _Rows(self.base)
        return _Rows(self.rows)


def _row(code, name, hits, scanned=5400, complete=1, truncated=0, syms=None):
    import json

    return (code, name, hits, scanned, complete, truncated, json.dumps(syms or []))


def test_no_data_is_honest():
    out = S.read_startup_signals(_Db(latest=None))
    assert out["ok"] is False and out["reason"] == "no_data"


def test_ratio_and_sort_by_ratio():
    db = _Db(
        rows=[_row("C116", "放量上攻", 120, syms=["002361.SZ", "600519.SH"]),
              _row("MSTAR", "早晨之星", 10)],
        base=[("C116", 40.0, 20), ("MSTAR", 2.0, 20)],
    )
    out = S.read_startup_signals(db)
    assert out["ok"] is True and out["trade_date"] == "2026-09-24"
    assert out["items"][0]["code"] == "MSTAR"        # ×5 排前
    assert out["items"][0]["ratio"] == 5.0
    c116 = [i for i in out["items"] if i["code"] == "C116"][0]
    assert c116["ratio"] == 3.0 and c116["symbols"] == ["002361.SZ", "600519.SH"]
    assert "客户端条件选股" in out["caliber"]


def test_missing_baseline_is_none_not_zero():
    out = S.read_startup_signals(_Db(rows=[_row("C120", "小步碎阳", 5)]))
    it = out["items"][0]
    assert it["baseline_avg"] is None and it["ratio"] is None
    assert "基线 —" in S.render_text(out)


def test_truncated_and_incomplete_are_flagged():
    out = S.read_startup_signals(_Db(rows=[_row("C123", "突破长期盘整", 900, complete=0, truncated=1)]))
    txt = S.render_text(out)
    assert "扫描不完整" in txt and "仅存前 500 只" in txt


def test_render_text_not_a_recommendation():
    out = S.read_startup_signals(_Db(rows=[_row("C116", "放量上攻", 3)]))
    txt = S.render_text(out)
    assert "不是推荐" in txt and "口径：" in txt


def test_render_empty_when_not_ok():
    assert S.render_text({"ok": False}) == ""
    assert S.render_text({}) == ""

def test_norm_day_accepts_iso_and_compact():
    """客户端只认紧凑格式; 传 ISO 过去会静默空跑(2026-09-25 实测)。"""
    from src.collectors.tq_formula_signals import _norm_day

    assert _norm_day("2026-09-24") == "20260924"
    assert _norm_day("20260924") == "20260924"
    assert _norm_day("2026/09/24") == "20260924"
    assert _norm_day("") == ""
    assert _norm_day("  2026-09-24  ") == "20260924"

def test_startup_formulas_are_wired():
    """防回归: 14 个启动信号公式必须真的在 FORMULA_SET 里被扫。

    只定义 STARTUP_FORMULAS 而忘了并进 FORMULA_SET = 公式永远不会被扫描,
    表现是"扫描成功但新公式一行都没有"(2026-09-25 实际踩到, 绕了一轮)。
    """
    from src.collectors.tq_formula_signals import BASE_FORMULAS, FORMULA_SET, STARTUP_FORMULAS

    codes = {c for c, _n, _a in FORMULA_SET}
    missing = [c for c, _n, _a in STARTUP_FORMULAS if c not in codes]
    assert missing == [], f"这些启动公式没接进 FORMULA_SET: {missing}"
    assert len(STARTUP_FORMULAS) == 14
    assert len(FORMULA_SET) == len(BASE_FORMULAS) + 14
    # 基础组也要在(防止改名时把基础公式弄丢)
    assert all(c in codes for c, _n, _a in BASE_FORMULAS)
