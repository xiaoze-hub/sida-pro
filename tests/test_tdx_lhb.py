"""通达信龙虎榜缓存解析测试(GBK JSON → 规范化), 2026-09-10。"""
from __future__ import annotations

import io

import pytest

import src.core.tdx_lhb as tdx_lhb


BOARD_PAYLOAD = {
    "colheader": ["$ZQDM1", "$SC1", "date", "bzb", "szb", "jmr", "zmr", "zmc", "sl1", "sl2", "lx", "lb", "$ZQDM"],
    "data": [
        # 实测样例(600744 华银电力: 净买入 2.22亿, 陆股通席位 2)
        ["600744", "1", "20260910", "22.76", "6.13", "222344577.84", "304271813.68", "81927235.84", "2", "0",
         "涨幅偏离值达7%的证券", "1", "3746487"],
        # 002636 金安国纪: 机构席位 1
        ["002636", "0", "20260910", "10.00", "12.00", "140018316.00", "288375780.00", "148357464.00", "3", "1",
         "连续三个交易日内涨幅偏离值累计达20%的证券", "0", "3746237"],
        ["BADSYM", "1", "20260910", "0", "0", "0", "0", "0", "0", "0", "x", "0", "1"],
    ],
}

SEATS_PAYLOAD = {
    "colheader": ["$ZQDM", "sc", "date", "lb", "yyb", "czjl", "yzbq", "yyb1", "yyb2", "bje", "sje", "jmr", "zb",
                  "mrcgl1", "mrcgl3", "mrcgl5", "ygcb", "ygsy"],
    "data": [
        ["002636", "0", "20260910", "1", "买(1): 深股通专用", "http://page1.tdx.com.cn/x", "", "B", "1",
         "288375780.0000", "148357464.0000", "140018316.0000", "2.92", "49.33", "44.22", "41.48",
         "74.60312966761076", "2.48"],
        ["002636", "0", "20260910", "1", "卖(2): 机构专用", "http://page1.tdx.com.cn/x", "", "S", "2",
         "1000.5", "2000.5", "-1000.0", "1.10", "40.00", "41.00", "42.00", "10.5", "-1.25"],
    ],
}


def test_parse_board_normalizes_and_drops_bad_rows():
    rows = tdx_lhb.parse_board(BOARD_PAYLOAD)
    assert len(rows) == 2  # BADSYM 被丢弃
    a = rows[0]
    assert a["symbol"] == "600744" and a["market"] == 1
    assert a["trade_date"] == "20260910"
    assert a["buy_ratio"] == pytest.approx(22.76) and a["sell_ratio"] == pytest.approx(6.13)
    assert a["net_buy"] == pytest.approx(222344577.84)
    assert a["buy_amt"] == pytest.approx(304271813.68) and a["sell_amt"] == pytest.approx(81927235.84)
    assert a["lg_seats"] == 2 and a["inst_seats"] == 0
    assert a["reason"] == "涨幅偏离值达7%的证券"
    assert a["merged_3d"] == 1 and a["ref_id"] == "3746487"
    b = rows[1]
    assert b["inst_seats"] == 1 and b["lg_seats"] == 3


def test_parse_seats_side_rank_and_numbers():
    rows = tdx_lhb.parse_seats(SEATS_PAYLOAD)
    assert len(rows) == 2
    buy = rows[0]
    assert buy["side"] == "buy" and buy["rank"] == 1 and buy["dept"] == "深股通专用"
    assert buy["buy_amt"] == pytest.approx(288375780.0) and buy["net_buy"] == pytest.approx(140018316.0)
    assert buy["win1_pct"] == pytest.approx(49.33) and buy["win5_pct"] == pytest.approx(41.48)
    assert buy["est_cost"] == pytest.approx(74.60312966761076)
    sell = rows[1]
    assert sell["side"] == "sell" and sell["rank"] == 2 and sell["dept"] == "机构专用"
    assert sell["net_buy"] == pytest.approx(-1000.0)


def test_board_reads_gbk_file_and_reports_freshness(tmp_path, monkeypatch):
    import json

    d = tmp_path / "tdx_lhb"
    d.mkdir()
    (d / "func_lhbfx101_1.jsn").write_bytes(json.dumps([BOARD_PAYLOAD], ensure_ascii=False).encode("gbk"))
    monkeypatch.setenv("TDX_LHB_CACHE_DIR", str(d))
    out = tdx_lhb.board()
    assert out["available"] is True
    assert out["count"] == 2 and out["trade_date"] == "20260910"
    assert out["synced_at"] is not None


def test_board_missing_file_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("TDX_LHB_CACHE_DIR", str(tmp_path / "nope"))
    out = tdx_lhb.board()
    assert out["available"] is False and out["items"] == []


def test_seats_reads_file_by_ref_id(tmp_path, monkeypatch):
    import json

    d = tmp_path / "tdx_lhb"
    (d / "seats").mkdir(parents=True)
    (d / "seats" / "3746237.jsn").write_bytes(json.dumps([SEATS_PAYLOAD], ensure_ascii=False).encode("gbk"))
    monkeypatch.setenv("TDX_LHB_CACHE_DIR", str(d))
    out = tdx_lhb.seats("3746237")
    assert out["available"] is True and out["count"] == 2
    assert out["items"][0]["dept"] == "深股通专用"
    # 非法 ref_id
    assert tdx_lhb.seats("../etc/passwd")["available"] is False
    assert tdx_lhb.seats("")["available"] is False
    # 不存在的 ref_id
    assert tdx_lhb.seats("999999")["available"] is False


def test_board_and_seats_support_client_native_layout(tmp_path, monkeypatch):
    """客户端原生布局: <root>/list/func_lhbfx101_1.jsn + <root>/lhbfx/<id>.jsn。"""
    import json

    d = tmp_path / "cloud_cache"
    (d / "list").mkdir(parents=True)
    (d / "lhbfx").mkdir(parents=True)
    (d / "list" / "func_lhbfx101_1.jsn").write_bytes(json.dumps([BOARD_PAYLOAD], ensure_ascii=False).encode("gbk"))
    (d / "lhbfx" / "3746487.jsn").write_bytes(json.dumps([SEATS_PAYLOAD], ensure_ascii=False).encode("gbk"))
    monkeypatch.setenv("TDX_LHB_CACHE_DIR", str(d))
    assert tdx_lhb.board()["available"] is True
    assert tdx_lhb.seats("3746487")["available"] is True


def test_archive_endpoints_expose_tdx_lhb(tmp_path, monkeypatch):
    """接口层: /api/archive/dragon-tiger-tdx(+ /seats) 读缓存; 无缓存如实 available=false。"""
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.web.api.market_archive as archive

    d = tmp_path / "cloud_cache"
    (d / "list").mkdir(parents=True)
    (d / "lhbfx").mkdir(parents=True)
    (d / "list" / "func_lhbfx101_1.jsn").write_bytes(json.dumps([BOARD_PAYLOAD], ensure_ascii=False).encode("gbk"))
    (d / "lhbfx" / "3746237.jsn").write_bytes(json.dumps([SEATS_PAYLOAD], ensure_ascii=False).encode("gbk"))
    monkeypatch.setenv("TDX_LHB_CACHE_DIR", str(d))

    app = FastAPI()
    app.include_router(archive.router, prefix="/api/archive")
    client = TestClient(app)

    r = client.get("/api/archive/dragon-tiger-tdx")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["count"] == 2 and body["trade_date"] == "20260910"
    assert body["items"][0]["symbol"] == "600744"

    r = client.get("/api/archive/dragon-tiger-tdx/seats", params={"ref_id": "3746237"})
    assert r.status_code == 200
    seats = r.json()
    assert seats["available"] is True and seats["count"] == 2

    # 无缓存(切到空目录) → 如实不可用
    monkeypatch.setenv("TDX_LHB_CACHE_DIR", str(tmp_path / "empty"))
    assert client.get("/api/archive/dragon-tiger-tdx").json()["available"] is False
