# -*- coding: utf-8 -*-
"""`.img` 盘口 入库 + API 暴露 单测(2026-10-02)。

覆盖:
  - 入库(schema 走版本化迁移 _m181; 不运行时建表):
      fixture 全量落库 / 幂等重导不新增 / user_id 隔离 / 缺文件·缺日期·缺代码显式降级
      / 缺失值写 NULL 不补 0 / 读回 JSON 保序且 None 保留
  - API `/api/orderbook-ob/img`(显式错误态):
      入库优先(source=img, origin=db) / as_of 取历史帧 / 无数据显式「无数据」
      / user_id 隔离 / 本地 .img 文件兜底(source=img-file) / 路由不被 /{symbol} 吞掉
  - **不回归**: 既有 `/api/orderbook-ob`(v0.13.43 硬超时 + 显式降级)语义不变

全离线: SQLite 内存库 + 仓库内真实样本 fixture; 不触网、不动真实库。
"""
import shutil
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import src.db.session as dbs  # noqa: E402
from src.core import img_orderbook_store as store  # noqa: E402
from src.core.tdx_img_parser import ImgSnapshot  # noqa: E402
from src.web.api import orderbook as obmod  # noqa: E402
from src.web.migrations import _m181_img_orderbook_frames  # noqa: E402

FIX_OPEN = ROOT / "tests" / "fixtures" / "sz002361_20260827_open.img"
FIX_0828 = ROOT / "tests" / "fixtures" / "sz002361_20260828_open.img"

CODE = "002361"
LAST_AS_OF = "2026-08-27T09:32:18"
OPEN_AS_OF = "2026-08-27T09:30:00"


@pytest.fixture
def engine(monkeypatch):
    """内存 SQLite + 只跑 _m181 迁移(不建其它表; 引擎注入 src.db.session.engine)。"""
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m181_img_orderbook_frames(conn)
    monkeypatch.setattr(dbs, "engine", eng)
    return eng


def _count(eng) -> int:
    with eng.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM img_orderbook_frames")).scalar()


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------


def test_migration_creates_table_and_is_idempotent(engine):
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(img_orderbook_frames)")).fetchall()}
    for c in ("trade_date", "as_of", "seq", "symbol", "market", "user_id", "source", "img_path",
              "bid_prices", "bid_vols", "ask_prices", "ask_vols", "bid_queue", "ask_queue",
              "bid_orders", "ask_orders", "spread", "bid_pressure"):
        assert c in cols, f"缺列 {c}"
    # 再跑一次迁移必须 no-op(幂等)
    with engine.begin() as conn:
        _m181_img_orderbook_frames(conn)


def test_ingest_real_fixture_all_frames(engine):
    res = store.ingest_img_file(FIX_OPEN, symbol=CODE)
    assert res["available"] is True
    assert res["frames_saved"] == 120 and res["n_frames"] == 120
    assert res["trade_date"] == "20260827"          # 从文件名提取
    assert res["as_of"] == LAST_AS_OF               # 最新一帧
    assert res["source"] == "img"
    assert _count(engine) == 120


def test_ingest_idempotent_reimport(engine):
    store.ingest_img_file(FIX_OPEN, symbol=CODE)
    res = store.ingest_img_file(FIX_OPEN, symbol=CODE)   # 重导
    assert res["frames_saved"] == 120
    assert _count(engine) == 120                          # 唯一键幂等, 不新增


def test_user_isolation_on_reads(engine):
    store.ingest_img_file(FIX_OPEN, symbol=CODE, user_id="u1")
    assert store.load_frames(CODE, user_id="u1") != []
    assert store.load_frames(CODE, user_id="u2") == []     # 不跨用户可见
    assert store.latest_frame(CODE, user_id="u2") is None


def test_ingest_missing_file_degrades(engine):
    res = store.ingest_img_file("/nonexistent/x.img", symbol=CODE, trade_date="20260827")
    assert res["available"] is False
    assert res["frames_saved"] == 0
    assert ".img 文件不存在" in res["note"]


def test_ingest_undated_filename_degrades(engine, tmp_path):
    p = tmp_path / "nope.img"
    shutil.copyfile(FIX_OPEN, p)
    res = store.ingest_img_file(p, symbol=CODE)   # 文件名无 8 位日期
    assert res["available"] is False
    assert "无法确定交易日" in res["note"]


def test_ingest_missing_symbol_degrades(engine):
    res = store.ingest_img_file(FIX_OPEN, symbol="")
    assert res["available"] is False
    assert "缺少 symbol" in res["note"]


def test_ingest_corrupt_file_degrades(engine, tmp_path):
    p = tmp_path / "corrupt_20260827.img"
    p.write_bytes(b"TEST" + b"\x00" * 4 + (5).to_bytes(8, "little") + b"\x00" * 8 + b"junk!")
    res = store.ingest_img_file(p, symbol=CODE)
    assert res["available"] is False
    assert "解析失败" in res["note"] and res.get("error")


def test_snapshot_to_row_keeps_missing_as_null():
    """缺失档位/占比写 NULL, **不补 0**; 显式 0 才是 0。"""
    snap = ImgSnapshot(t="09:30:00", bid_prices=[10.5, None], bid_vols=[0, None],
                       ask_prices=[None], ask_vols=[None])
    row = store.snapshot_to_row(snap, trade_date="20260827", symbol=CODE)
    import json
    assert json.loads(row["bid_prices"]) == [10.5, None]    # null 保留
    assert json.loads(row["bid_vols"]) == [0, None]         # 0 与 null 可区分
    assert row["bid_orders"] is None
    assert row["spread"] is None and row["bid_pressure"] is None
    assert row["ask_queue"] is None and row["bid_queue"] is None


def test_snapshot_to_row_skips_frameless():
    assert store.snapshot_to_row(ImgSnapshot(t=None), trade_date="20260827", symbol=CODE) is None


def test_load_frames_decodes_json_and_keeps_none(engine):
    store.ingest_img_file(FIX_OPEN, symbol=CODE)
    rows = store.load_frames(CODE, user_id="shared")
    assert len(rows) == 120
    first, last = rows[0], rows[-1]
    assert first["as_of"] == "2026-08-27T08:36:27"
    assert last["as_of"] == LAST_AS_OF
    assert last["bid_prices"][:3] == [10.23, 10.22, 10.21]
    assert isinstance(last["ask_queue"], list)
    # 首帧盘前无委托队列 → NULL(不是空 list, 也不是 0)
    assert first["bid_queue"] is None and first["ask_queue"] is None
    assert last["source"] == "img"


def test_latest_frame_returns_last(engine):
    store.ingest_img_file(FIX_OPEN, symbol=CODE)
    assert store.latest_frame(CODE, user_id="shared")["as_of"] == LAST_AS_OF


def test_persist_frames_never_raises(monkeypatch):
    class _Boom:
        def begin(self):
            raise RuntimeError("engine down")

    monkeypatch.setattr(store, "_engine", lambda: _Boom())
    assert store.persist_frames([{"trade_date": "20260827"}]) == 0
    assert store.persist_frames([]) == 0


def test_trade_date_from_path():
    assert store.trade_date_from_path("sz002361_20260827.img") == "20260827"
    assert store.trade_date_from_path("/a/b/002361.img") is None


# ---------------------------------------------------------------------------
# API 纯函数
# ---------------------------------------------------------------------------


def test_api_from_db_uses_ingested_frames(engine):
    store.ingest_img_file(FIX_OPEN, symbol=CODE)
    out = obmod.get_img_orderbook(CODE, user_id="shared")
    assert out["available"] is True
    assert out["source"] == "img" and out["origin"] == "db"
    assert out["as_of"] == LAST_AS_OF
    assert out["trade_date"] == "20260827"
    assert out["best_bid"] == 10.23 and out["best_ask"] == 10.24
    assert len(out["book"]["bid"]) == 10 and len(out["book"]["ask"]) == 10
    assert isinstance(out["bid_queue"], list) and isinstance(out["ask_queue"], list)
    assert out["bid_orders"] == 16 and out["ask_orders"] == 4
    assert out["shape"] in ("托盘", "压盘", "均衡")


def test_api_as_of_picks_historical_frame(engine):
    store.ingest_img_file(FIX_OPEN, symbol=CODE)
    out = obmod.get_img_orderbook(CODE, user_id="shared", as_of=OPEN_AS_OF)
    assert out["available"] is True
    assert out["as_of"] == OPEN_AS_OF
    assert out["best_bid"] == 10.27 and out["best_ask"] == 10.29


def test_api_as_of_before_all_frames_degrades(engine):
    store.ingest_img_file(FIX_OPEN, symbol=CODE)
    out = obmod.get_img_orderbook(CODE, user_id="shared", as_of="2000-01-01T00:00:00")
    assert out["available"] is False
    assert "无 as_of <=" in out["note"]
    assert out["book"] == {"bid": [], "ask": []}


def test_api_no_data_is_explicit_no_data(engine, monkeypatch):
    """既无入库帧又无本地 .img → available:false + 「无数据」占位, 不补 0。"""
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    out = obmod.get_img_orderbook(CODE, user_id="shared")
    assert out["available"] is False
    assert out["source"] == "img"
    assert out["as_of"]                              # 有真实时间标注
    assert "无 .img" in out["note"]
    assert out["book"] == {"bid": [], "ask": []}
    assert out["bid_queue"] == "无数据" and out["ask_queue"] == "无数据"
    assert out["best_bid"] is None and out["bid_pressure"] is None


def test_api_user_scope_isolated(engine, monkeypatch):
    """别的用户入库的帧, 当前 scope 读不到(不能只验自己账号)。"""
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    store.ingest_img_file(FIX_OPEN, symbol=CODE, user_id="u1")
    out = obmod.get_img_orderbook(CODE, user_id="shared")
    assert out["available"] is False
    assert obmod.get_img_orderbook(CODE, user_id="u1")["available"] is True


def test_api_file_fallback_marks_source(engine, monkeypatch, tmp_path):
    """未入库但本地有 .img → source=img-file + as_of=文件内帧时间(标非实时)。"""
    img = tmp_path / "sz002361_20260827.img"
    shutil.copyfile(FIX_OPEN, img)
    monkeypatch.setenv("PANWATCH_IMG_DIR", str(tmp_path))
    out = obmod.get_img_orderbook(CODE, user_id="shared")
    assert out["available"] is True
    assert out["source"] == "img-file" and out["origin"] == "file"
    assert out["as_of"] == "09:32:18"
    assert out["trade_date"] == "20260827"
    assert "非实时" in out["note"]
    assert out["best_bid"] == 10.23


def test_api_empty_symbol_degrades(engine):
    out = obmod.get_img_orderbook("", user_id="shared")
    assert out["available"] is False and "symbol 为空" in out["note"]


def test_api_unknown_second_sample_ingests(engine):
    """第二份真实样本(不同 magic)也能入库(容器校验不绑死 magic)。"""
    res = store.ingest_img_file(FIX_0828, symbol=CODE, user_id="u9", trade_date="20260828")
    assert res["available"] is True and res["frames_saved"] == 60
    out = obmod.get_img_orderbook(CODE, user_id="u9")
    assert out["available"] is True and out["trade_date"] == "20260828"


# ---------------------------------------------------------------------------
# 端点(路由优先级 + 不回归既有端点)
# ---------------------------------------------------------------------------


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(obmod.router, prefix="/api/orderbook-ob")
    return TestClient(app)


def test_endpoint_img_returns_data(engine):
    store.ingest_img_file(FIX_OPEN, symbol=CODE)
    resp = _client().get("/api/orderbook-ob/img", params={"symbol": CODE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True and body["source"] == "img"
    assert body["as_of"] == LAST_AS_OF


def test_endpoint_img_requires_symbol_not_shadowed(engine):
    """/img 不被 /{symbol} 吞掉: 缺 symbol 返回 422(而非把 'img' 当代码)。"""
    resp = _client().get("/api/orderbook-ob/img")
    assert resp.status_code == 422


def test_endpoint_img_degraded_is_200(engine, monkeypatch):
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    resp = _client().get("/api/orderbook-ob/img", params={"symbol": CODE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False and body["source"] == "img"
    assert body["as_of"]


def test_existing_orderbook_endpoint_not_regressed(engine, monkeypatch):
    """既有 /api/orderbook-ob 成功路径逐字段不变(v0.13.43 语义不回退)。"""
    fake = {
        "events": [], "ob_series": [{"ts": 1.0, "dt": "2026-10-01T10:30:00", "ob": 0.5,
                                     "label": "买压", "bid_amt10": 1.0, "ask_amt10": 1.0}],
        "ghost_ratio": 0.0, "summary": "ok",
    }
    monkeypatch.setattr(obmod.orderbook_engine, "run", lambda *a, **k: fake)
    monkeypatch.setattr(obmod.orderbook_engine, "THS", object())
    resp = _client().get("/api/orderbook-ob/002361")
    assert resp.status_code == 200
    assert resp.json() == {
        "available": True, "ob_series": fake["ob_series"], "events": [],
        "ghost_ratio": 0.0, "note": "ok",
    }


def test_existing_orderbook_endpoint_degrades_200(engine, monkeypatch):
    """既有端点降级语义不变: 上游失败 → 200 + available:false + source/as_of。"""
    def _down(*a, **k):
        raise RuntimeError("thsdk down")

    monkeypatch.setattr(obmod.orderbook_engine, "run", _down)
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    resp = _client().get("/api/orderbook-ob", params={"symbol": CODE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["source"] == "thsdk" and body["as_of"]
