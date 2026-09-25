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
