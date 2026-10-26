# -*- coding: utf-8 -*-
"""首封时间×封成比 强度影子落库 + 只读读取 + API 降级测试(2026-10-11 P0-1)。

全 mock(禁真网络); DB 走测试 SQLite(_init_test_db 建全表含 m188)。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import limit_strength_shadow as sh  # noqa: E402


# ─────────────── build_rows ───────────────

def test_build_rows_labels_and_no_data():
    rows = sh.build_rows([
        {"symbol": "002361", "first_time": "09:26:00", "seal_amount": 1e8,
         "circulating_mktcap": 2e9, "open_times": 0},
        {"symbol": "600519", "first_time": "14:50:00", "seal_amount": 1e7,
         "circulating_mktcap": 5e9, "open_times": 2},
        {"symbol": "300750"},  # 全缺 → no_data
        {"symbol": ""},  # 无码 → 剔除
    ])
    assert len(rows) == 3
    by = {r["symbol"]: r for r in rows}
    assert by["002361"]["strength_label"] == "S" and by["002361"]["status"] == "ok"
    # 尾盘 + C 档 → C(降不动)
    assert by["600519"]["status"] == "ok" and by["600519"]["strength_label"] == "C"
    assert by["300750"]["status"] == "no_data" and by["300750"]["strength_label"] is None


def test_build_rows_uses_ratio_when_given():
    rows = sh.build_rows([{"symbol": "002361", "first_time": "09:26:00", "ratio": 0.06}])
    assert rows[0]["strength_label"] == "S" and rows[0]["seal_ratio"] == pytest.approx(0.06)


# ─────────────── collect_and_mark (mock providers) ───────────────

def _mk_providers():
    def codes(td):
        return ["002361", "600519", "300750"]

    def snapshot(cs):
        return {
            "002361": {"first_time": "09:26:00", "open_times": 0},
            "600519": {"first_time": "14:50:00", "open_times": 0},
            "300750": {"first_time": None, "open_times": None},
        }

    def more(cs):
        return {
            "002361": {"seal_amount": 1e8, "circulating_mktcap": 2e9},
            "600519": {"seal_amount": 1e7, "circulating_mktcap": 5e9},
            "300750": {"seal_amount": 1e7, "circulating_mktcap": 5e9},
        }

    return codes, snapshot, more


def test_collect_and_mark_dry_run():
    codes, snap, more = _mk_providers()
    out = sh.collect_and_mark("20261010", codes_provider=codes, snapshot_provider=snap,
                              more_provider=more, names_provider=lambda td: {"002361": "顺灏股份"},
                              write=False)
    assert out["ok"] is True
    assert out["rows"] == 3 and out["labeled"] == 2 and out["no_data"] == 1


def test_collect_and_mark_no_codes():
    out = sh.collect_and_mark("20261010", codes_provider=lambda td: [],
                              snapshot_provider=lambda c: {}, more_provider=lambda c: {},
                              write=False)
    assert out["ok"] is False and out["rows"] == 0


# ─────────────── 落库幂等 + 读取(测试 DB) ───────────────

_TD = "20261010"
_SYMS = ("009971", "009972")


@pytest.fixture()
def _cleanup():
    yield
    from sqlalchemy import text

    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text(f"DELETE FROM {sh._TABLE} WHERE trade_date = :d"), {"d": _TD})
        db.commit()
    finally:
        db.close()


def _rows():
    return sh.build_rows([
        {"symbol": _SYMS[0], "name": "甲", "first_time": "09:26:00",
         "seal_amount": 1e8, "circulating_mktcap": 2e9, "open_times": 0, "source": "tq_zdt"},
        {"symbol": _SYMS[1], "name": "乙", "first_time": "14:50:00",
         "seal_amount": 1e7, "circulating_mktcap": 5e9, "open_times": 3, "source": "tq_zdt"},
    ])


def test_mark_rows_idempotent(_cleanup):
    r1 = sh.mark_rows(_TD, _rows(), write=True)
    assert r1["written"] == 2 and r1["labeled"] == 2
    # 幂等: 再写一次行数不翻倍(upsert by (trade_date, symbol))
    sh.mark_rows(_TD, _rows(), write=True)
    got = sh.labels_for_date(_TD)
    assert got["available"] is True and len(got["labels"]) == 2
    assert got["labels"][_SYMS[0]]["strength_label"] in ("S", "A", "B", "C")


def test_labels_for_date_missing(_cleanup):
    got = sh.labels_for_date("19990101")
    assert got["available"] is False and got["labels"] == {}
    assert got["note"]


def test_labels_map(_cleanup):
    sh.mark_rows(_TD, _rows(), write=True)
    m = sh.labels_map([_TD, "19990101"])
    assert _TD in m and set(m[_TD]) <= set(_SYMS)
    assert "19990101" not in m


# ─────────────── 迁移门禁: m188 存在 ───────────────

def test_migration_188_registered():
    from src.web.migrations import MIGRATIONS

    versions = [m.version for m in MIGRATIONS]
    assert 188 in versions
    # 版本号从 101 起**连续无缺口**(不写死上界 —— 新增迁移不应让本钉子变红)
    assert versions == list(range(101, max(versions) + 1))
    m188 = next(m for m in MIGRATIONS if m.version == 188)
    assert m188.name == "limit_strength_shadow"
    assert m188.runner.__name__.startswith("_m188_")


# ─────────────── API 端点(降级 + 读取) ───────────────

def _build_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import limit_strength as ls_api

    app = FastAPI()
    app.include_router(ls_api.router, prefix="/api/limit-strength")
    return TestClient(app)


def test_endpoint_usage():
    body = _build_client().get("/api/limit-strength").json()
    assert body["endpoint"] == "/api/limit-strength"
    assert "影" in body["note"]


def test_endpoint_shadow_no_data(monkeypatch):
    monkeypatch.setattr(sh, "labels_for_date",
                        lambda d=None: {"available": False, "trade_date": None,
                                        "labels": {}, "counts": {}, "note": "无数据"})
    body = _build_client().get("/api/limit-strength/shadow").json()
    assert body["available"] is False and body["labels"] == {}


def test_endpoint_shadow_returns_labels(_cleanup, monkeypatch):
    sh.mark_rows(_TD, _rows(), write=True)
    body = _build_client().get(f"/api/limit-strength/shadow?date={_TD}").json()
    assert body["available"] is True
    assert len(body["labels"]) == 2 and sum(body["counts"].values()) == 2


def test_endpoint_run_degrades(monkeypatch):
    monkeypatch.setattr(sh, "collect_and_mark", lambda trade_date=None: {"ok": False,
                        "trade_date": "20261010", "reason": "当日无涨停股", "rows": 0})
    body = _build_client().post("/api/limit-strength/shadow/run?date=20261010").json()
    assert body["ok"] is False
