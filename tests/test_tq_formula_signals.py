"""TQ 条件选股信号: 解析 / 翻页 / 分片 / 落库 / 基线 + 网关契约锁。

fixture `tq_formula_mul_xg_sample.json` 是从真实 TQ 网关抓下来的 **未改写** 原始响应
(2026-09-25, MACD买入, 前 50 只, 窗口 20260905~20260924)。所以这些断言同时是口径回归锁:
信号键 = **OUTPUT1**(不是公式名), 命中 = Value **"1"**, 未命中 = "0"。

为什么这些断言值得存在
--------------------
① 服务端**按 800 只/页分页**(实测 batch_stock_count=800/has_more_batch=true):
   不续取就**静默漏掉大部分标的** —— 结果看着正常, 数字却是残缺的(老实现即如此)。
② 分片失败若不标记, 残缺结果会被当成"全市场命中家数" = **编造**。
③ 传全市场但不给时间窗 → 网关按"全部历史"算, 全市场扫描从 24s 拖到分钟级。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

import marketdata.vendors.tq as tqv
from src.collectors import tq_formula_signals as tfs

FIXTURE = Path(__file__).parent / "fixtures" / "tq_formula_mul_xg_sample.json"


@pytest.fixture
def sample() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- fixture 自检


def test_fixture_is_real_and_unmodified(sample):
    """fixture 自检: 真实响应结构 + 编码口径(防止被误改成空文件或改写数字)。"""
    assert sample["_signal_key"] == "OUTPUT1" and sample["_hit_value"] == "1"
    assert len(sample["codes"]) == 50
    res = sample["response"]
    assert res["ErrorId"] == "0"
    assert res["batch_stock_count"] == 50 and res["has_more_batch"] is False

    stocks = {k: v for k, v in res.items() if k not in tqv._FORMULA_META_KEYS}
    assert len(stocks) == 50, "本片应有 50 只"
    # 每只票的信号键只有 OUTPUT1; 值只有 0/1
    for code, block in stocks.items():
        assert set(block) == {"OUTPUT1"}, f"{code} 的信号键异常: {set(block)}"
        for row in block["OUTPUT1"]:
            assert str(row["Value"]) in ("0", "1"), f"{code} 的值异常: {row['Value']}"
            assert len(str(row["Date"])) == 8


# ------------------------------------------------------------------ 解析口径


def test_parse_counts_only_value_1(sample):
    """命中 = Value "1"; 指定日期只取当天那一行(不是"有信号就算")。"""
    res = sample["response"]
    day = "20260924"
    expected = sum(
        1 for block in res.values()
        if isinstance(block, dict) and block.get("OUTPUT1")
        for row in block["OUTPUT1"] if str(row.get("Date")) == day and str(row.get("Value")) == "1"
    )
    hits, used = tqv._formula_hits(res, day)
    assert used == day
    assert len(hits) == expected
    assert {h["signal"] for h in hits} == {"OUTPUT1"}
    assert all(h["date"] == day for h in hits)
    assert all(h["value"] == "1" for h in hits)


def test_parse_meta_keys_are_not_treated_as_stocks(sample):
    """BatchFormulaPaged/stock_total/next_stock_index 等元数据不能当股票解析。"""
    hits, _ = tqv._formula_hits(sample["response"], "20260924")
    assert not [h for h in hits if h["symbol"].startswith(("batch_", "stock_", "next_", "has_", "Error"))]


def test_parse_unknown_date_gives_no_hits(sample):
    """窗口里没有的日期 → 0 命中, **不得**拿最后一天的数据冒充。"""
    hits, used = tqv._formula_hits(sample["response"], "20200101")
    assert hits == [] and used == "20200101"
    # 而且必须能区分: 该日期**一行数据都没有**(=非交易日), 不是"有行但值全 0"
    assert tqv._date_rows_seen(sample["response"], "20200101") == 0
    assert tqv._date_rows_seen(sample["response"], "20260924") > 0


def test_scan_flags_no_data_day(monkeypatch):
    """回归锁: 非交易日 → date_has_data=False(落库方据此跳过, 不写 0 拉低基线)。"""
    def _fake_rpc(method, params, timeout=None, full=False):
        # 只有 20260924 的行(请求的是 20260925=非交易日)
        return {"600000.SH": {"OUTPUT1": [{"Date": "20260924", "Value": "1"}]}, "ErrorId": "0"}

    monkeypatch.setattr(tqv, "_rpc", _fake_rpc)
    out = tqv.formula_scan("MACD买入", date="20260925", codes=["600000.SH"])
    assert out["hit_count"] == 0
    assert out["date_rows"] == 0
    assert out["date_has_data"] is False, "该日无数据行 ⇒ 必须标记出来"

    # 有数据行的日期 → True
    out2 = tqv.formula_scan("MACD买入", date="20260924", codes=["600000.SH"])
    assert out2["date_has_data"] is True and out2["hit_count"] == 1


def test_parse_without_date_uses_last_row(sample):
    """不指定日期时用序列最后一行, 并把实际日期回带(用于落库)"""
    hits, used = tqv._formula_hits(sample["response"], "")
    assert used and len(used) == 8
    assert all(h["date"] == used for h in hits)


# ------------------------------------------------------------------ 分页(核心)


def test_paging_merges_all_pages(monkeypatch):
    """回归锁: 服务端 800 只/页 + has_more_batch → 必须续取到 false, 否则静默漏标。"""
    calls: list[int] = []

    def _fake_rpc(method, params, timeout=None, full=False):
        calls.append(int(params.get("batch_start_index", -1)))
        if params["batch_start_index"] == 0:
            return {"600000.SH": {"OUTPUT1": [{"Date": "20260924", "Value": "1"}]},
                    "BatchFormulaPaged": True, "batch_stock_count": 2, "stock_total": 3,
                    "next_stock_index": 2, "has_more_batch": True, "ErrorId": "0"}
        return {"600001.SH": {"OUTPUT1": [{"Date": "20260924", "Value": "1"}]},
                "BatchFormulaPaged": True, "batch_stock_count": 1, "stock_total": 3,
                "next_stock_index": 3, "has_more_batch": False, "ErrorId": "0"}

    monkeypatch.setattr(tqv, "_rpc", _fake_rpc)
    out = tqv.formula_xg_mul("MACD买入", ["600000.SH", "600001.SH", "600002.SH"])
    assert calls == [0, 2], "第二页必须带 batch_start_index=2"
    assert set(out) == {"600000.SH", "600001.SH"}, "两页结果都要合并, 元数据要剔除"


def test_paging_stops_when_cursor_stalls(monkeypatch):
    """游标不前进时必须停(否则 40 页死循环打客户端)。"""
    calls: list[int] = []

    def _fake_rpc(method, params, timeout=None, full=False):
        calls.append(int(params.get("batch_start_index", -1)))
        return {"600000.SH": {"OUTPUT1": []}, "next_stock_index": 0,
                "has_more_batch": True, "ErrorId": "0"}

    monkeypatch.setattr(tqv, "_rpc", _fake_rpc)
    tqv.formula_xg_mul("MACD买入", ["600000.SH"])
    assert len(calls) == 1, "游标未前进应立刻停止"


def test_error_19_returns_partial_without_raise(monkeypatch):
    """ErrorId=19 = 数据过大只能部分返回: 返回已取到的部分, **不抛**(抛了整批白跑)。"""
    def _fake_rpc(method, params, timeout=None, full=False):
        return {"600000.SH": {"OUTPUT1": [{"Date": "20260924", "Value": "1"}]},
                "ErrorId": "19", "Error": "data too large"}

    monkeypatch.setattr(tqv, "_rpc", _fake_rpc)
    out = tqv.formula_xg_mul("MACD买入", ["600000.SH"])
    assert set(out) == {"600000.SH"}


def test_other_errors_raise(monkeypatch):
    """其它 ErrorId 必须抛 —— 静默返回空会被误读成"今天没有票触发"。"""
    monkeypatch.setattr(
        tqv, "_rpc",
        lambda method, params, timeout=None, full=False: {"ErrorId": "10", "Error": "json has no end_time"},
    )
    with pytest.raises(RuntimeError, match="10"):
        tqv.formula_xg_mul("MACD买入", ["600000.SH"])


# ------------------------------------------------------------------ 全市场扫描


def test_scan_marks_incomplete_when_chunk_fails(monkeypatch):
    """诚实性回归锁: 分片失败 → complete=False + chunks_failed>0, 家数只算成功的片。"""
    calls: list[list[str]] = []

    def _fake_rpc(method, params, timeout=None, full=False):
        codes = list(params["stock_list"])
        calls.append(codes)
        if "600002.SH" in codes:
            raise RuntimeError("connection reset")
        return {c: {"OUTPUT1": [{"Date": "20260924", "Value": "1"}]} for c in codes} | {"ErrorId": "0"}

    monkeypatch.setattr(tqv, "_rpc", _fake_rpc)
    out = tqv.formula_scan("MACD买入", date="20260924", chunk=2,
                           codes=["600000.SH", "600001.SH", "600002.SH", "600003.SH"])
    assert out["complete"] is False
    assert out["chunks_failed"] == 1
    assert out["hit_count"] == 2, "失败片的票不能计入命中数"
    assert out["scanned"] == 2
    assert len(calls) == 2


def test_scan_passes_narrow_window(monkeypatch):
    """性能锁: 必须带窄时间窗 —— 不带日期网关按"全部历史"算, 24s 拖成分钟级。"""
    seen: list[dict] = []

    def _fake_rpc(method, params, timeout=None, full=False):
        seen.append(dict(params))
        return {"600000.SH": {"OUTPUT1": [{"Date": "20260924", "Value": "1"}]}, "ErrorId": "0"}

    monkeypatch.setattr(tqv, "_rpc", _fake_rpc)
    tqv.formula_scan("MACD买入", date="20260924", codes=["600000.SH"])
    assert seen and seen[0]["end_time"] == "20260924"
    assert seen[0]["start_time"] == "20260904", "回看 20 自然日(20260924 - 20)"
    assert seen[0]["count"] == 0, "count 必须为 0(不是 -1=全部历史)"


def test_scan_empty_pool_is_not_a_zero(monkeypatch):
    """取不到代码池 → complete=False + error; **不得**返回 hit_count=0 当"今日无信号"。"""
    monkeypatch.setattr(tqv, "stock_list", lambda *a, **k: [])
    out = tqv.formula_scan("MACD买入")
    assert out["complete"] is False and out["hit_count"] == 0 and "error" in out


# ------------------------------------------------------------------ 采集器


def _scan(**kw) -> dict:
    base = {"formula": "x", "date": "20260924", "scanned": 5570, "hit_count": 38,
            "hits": [{"symbol": "600000.SH", "signal": "OUTPUT1", "date": "20260924", "value": "1"}],
            "chunks_failed": 0, "complete": True, "per_signal": {"OUTPUT1": 38},
            "date_rows": 5570, "date_has_data": True}
    base.update(kw)
    return base


def test_fetch_rows_shape_and_complete_propagation(monkeypatch):
    monkeypatch.setattr(tfs, "formula_scan", lambda *a, **k: _scan())
    rows = tfs.fetch_formula_signals("20260924")
    assert len(rows) == len(tfs.FORMULA_SET)
    r = rows[0]
    assert r["trade_date"] == "20260924" and r["hit_count"] == 38
    assert r["complete"] == 1 and r["chunks_failed"] == 0
    assert json.loads(r["hits_json"]) == ["600000.SH"]


def test_fetch_propagates_incomplete_marker(monkeypatch):
    monkeypatch.setattr(tfs, "formula_scan", lambda *a, **k: _scan(complete=False, chunks_failed=3))
    rows = tfs.fetch_formula_signals("20260924")
    assert all(r["complete"] == 0 and r["chunks_failed"] == 3 for r in rows)


def test_fetch_survives_single_formula_failure(monkeypatch):
    """单公式失败只少一行(下次补), **不能**写 0 —— 0 会被读成"今天没人触发"。"""
    def _scan_boom(name, **kw):
        if name == tfs.FORMULA_SET[0][0]:
            raise RuntimeError("TQ 不可用")
        return _scan()

    monkeypatch.setattr(tfs, "formula_scan", _scan_boom)
    rows = tfs.fetch_formula_signals("20260924")
    assert len(rows) == len(tfs.FORMULA_SET) - 1
    assert tfs.FORMULA_SET[0][0] not in {r["formula_code"] for r in rows}


def test_fetch_skips_no_data_day_instead_of_writing_zero(monkeypatch):
    """非交易日: 不落 0(0 会把基线拉低) → fetch 返回空, sync 显式报错。"""
    from src.web.database import SessionLocal

    monkeypatch.setattr(tfs, "formula_scan",
                        lambda *a, **k: _scan(hit_count=0, hits=[], date_rows=0, date_has_data=False))
    assert tfs.fetch_formula_signals("20260925") == []

    db = SessionLocal()
    try:
        out = tfs.sync_formula_signals(db, trade_date="20260925")
        assert "error" in out
        n = db.execute(text(
            "SELECT COUNT(*) FROM tq_formula_signal_daily WHERE trade_date='20260925'")).scalar()
        assert n == 0, "非交易日绝不能写 0 行"
    finally:
        db.execute(text("DELETE FROM tq_formula_signal_daily"))
        db.commit()
        db.close()


def test_fetch_truncates_hits_but_flags_it(monkeypatch):
    many = [{"symbol": f"6000{i:02d}.SH", "signal": "OUTPUT1", "date": "20260924", "value": "1"}
            for i in range(tfs.HITS_STORE_LIMIT + 20)]
    monkeypatch.setattr(tfs, "formula_scan", lambda *a, **k: _scan(hits=many, hit_count=len(many)))
    rows = tfs.fetch_formula_signals("20260924")
    assert len(json.loads(rows[0]["hits_json"])) == tfs.HITS_STORE_LIMIT
    assert rows[0]["truncated"] == 1
    assert rows[0]["hit_count"] == len(many), "家数仍是全量, 只截清单"


# ------------------------------------------------------------- 落库 + 基线


def _clean(db):
    db.execute(text("DELETE FROM tq_formula_signal_daily"))
    db.commit()


def test_sync_upserts_idempotently(monkeypatch):
    from src.web.database import SessionLocal

    monkeypatch.setattr(tfs, "formula_scan", lambda *a, **k: _scan())
    db = SessionLocal()
    try:
        _clean(db)
        first = tfs.sync_formula_signals(db, trade_date="20260924")
        assert "error" not in first and first["rows"] == len(tfs.FORMULA_SET)
        n1 = db.execute(text("SELECT COUNT(*) FROM tq_formula_signal_daily")).scalar()
        second = tfs.sync_formula_signals(db, trade_date="20260924")
        assert second["rows"] == first["rows"]
        n2 = db.execute(text("SELECT COUNT(*) FROM tq_formula_signal_daily")).scalar()
        assert n1 == n2 == len(tfs.FORMULA_SET), "唯一键(trade_date+formula+arg)必须幂等"
    finally:
        _clean(db)
        db.close()


def test_sync_reports_error_when_nothing_fetched(monkeypatch):
    from src.web.database import SessionLocal

    def _scan_boom(*a, **k):
        raise RuntimeError("TQ 客户端未开")

    monkeypatch.setattr(tfs, "formula_scan", _scan_boom)
    db = SessionLocal()
    try:
        out = tfs.sync_formula_signals(db, trade_date="20260924")
        assert "error" in out, "全失败必须显式报错, 不能静默成功"
    finally:
        db.close()


def test_baseline_maths_and_none_when_no_history(monkeypatch):
    """基线: 最新值 + 近 N 日均值/极值; 没历史时基线为 None(**不填占位数字**)。"""
    from src.web.database import SessionLocal

    monkeypatch.setattr(tfs, "formula_scan", lambda *a, **k: _scan())
    db = SessionLocal()
    try:
        _clean(db)
        tfs.sync_formula_signals(db, trade_date="20260924")
        out = tfs.latest_with_baseline(db, days=20)
        assert out["trade_date"] == "20260924"
        item = out["items"][0]
        assert item["baseline_avg"] is None and item["samples"] == 0, "只有一天就没有基线"

        # 再造 3 个历史日: 38 / 10 / 20 → 均值 20.0(x3 天的历史)
        for day, n in (("20260923", 10), ("20260922", 20), ("20260921", 30)):
            db.execute(
                text(
                    "INSERT INTO tq_formula_signal_daily "
                    "(trade_date, formula_code, formula_name, formula_arg, hit_count, "
                    " scanned_count, chunks_failed, complete, truncated, hits_json) "
                    "VALUES (:d, :c, 'n', '', :n, 5570, 0, 1, 0, '[]')"
                ),
                {"d": day, "c": tfs.FORMULA_SET[0][0], "n": n},
            )
        db.commit()
        out2 = tfs.latest_with_baseline(db, days=20)
        it = next(i for i in out2["items"] if i["formula_code"] == tfs.FORMULA_SET[0][0])
        assert it["baseline_avg"] == 20.0 and it["baseline_max"] == 30 and it["baseline_min"] == 10
        assert it["samples"] == 3
    finally:
        _clean(db)
        db.close()


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import formula_signals as api

    app = FastAPI()
    app.include_router(api.router, prefix="/api/formula-signals")
    return TestClient(app)


def test_endpoint_degrades_honestly_when_empty():
    """没有采集数据时接口必须显式 degraded, 不能返回"0 家"当"今日无信号"。"""
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM tq_formula_signal_daily"))
        db.commit()
    finally:
        db.close()
    r = _client().get("/api/formula-signals?days=20")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == [] and body.get("degraded") is True
    assert "暂无采集数据" in body.get("note", "")


def test_endpoint_series_happy_path_and_404():
    """走真实 HTTP 层: 有数据时给序列+命中清单; 未知公式 404(不是 503/空 200)。"""
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM tq_formula_signal_daily"))
        db.execute(
            text(
                "INSERT INTO tq_formula_signal_daily "
                "(trade_date, formula_code, formula_name, formula_arg, hit_count, "
                " scanned_count, chunks_failed, complete, truncated, hits_json) "
                "VALUES ('20260924', 'MACD买入', 'MACD买入信号', '', 38, 5570, 0, 1, 0, "
                " '[\"600000.SH\",\"600001.SH\"]')"
            )
        )
        db.commit()
    finally:
        db.close()

    c = _client()
    r = c.get("/api/formula-signals/MACD买入?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hit_count"] == 38 and body["complete"] is True
    assert body["latest_hits"] == ["600000.SH", "600001.SH"]
    assert [s["trade_date"] for s in body["series"]] == ["20260924"]

    assert c.get("/api/formula-signals/不存在公式").status_code == 404

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM tq_formula_signal_daily"))
        db.commit()
    finally:
        db.close()
