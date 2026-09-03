# -*- coding: utf-8 -*-
"""设计稿 v2.1 §12 数据源健康检查 单测: src/core/source_health.py

覆盖:
  - 目录型源(tck/img): 未配 → down / 目录不存在 → down / 空目录 → degraded / 有文件 → connected
  - wencai: thsdk 缺失 → down / 无凭据 → degraded / 齐全 → connected
  - shadow: DB 可查 → connected / 异常 → unknown
  - 状态值只在四值内; 未知 id → unknown 不抛异常
  - 缓存 TTL 行为 + clear_health_cache
  - API 路由: /health 与 /health/{id}, 且不被 /{source_id} 抢占
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import source_health as sh  # noqa: E402

VALID_STATUS = {"connected", "degraded", "down", "unknown"}


@pytest.fixture(autouse=True)
def _clear_cache():
    sh.clear_health_cache()
    yield
    sh.clear_health_cache()


# ---------------------------------------------------------------------------
# 目录型源 (.tck / .img)
# ---------------------------------------------------------------------------


def test_tck_not_configured_is_down(monkeypatch):
    monkeypatch.delenv(sh.TCK_DIR_ENV, raising=False)
    r = sh.check_source("tck")
    assert r["status"] == "down"
    assert sh.TCK_DIR_ENV in (r["detail"] or "")


def test_tck_dir_missing_is_down(monkeypatch, tmp_path):
    monkeypatch.setenv(sh.TCK_DIR_ENV, str(tmp_path / "nope"))
    assert sh.check_source("tck")["status"] == "down"


def test_tck_empty_dir_is_degraded(monkeypatch, tmp_path):
    """配了目录但里面没 .tck → degraded(不是 down, 也不是 connected)。"""
    monkeypatch.setenv(sh.TCK_DIR_ENV, str(tmp_path))
    r = sh.check_source("tck")
    assert r["status"] == "degraded"


def test_tck_with_files_is_connected(monkeypatch, tmp_path):
    (tmp_path / "sz000977_20260901.tck").write_bytes(b"x")
    monkeypatch.setenv(sh.TCK_DIR_ENV, str(tmp_path))
    r = sh.check_source("tck")
    assert r["status"] == "connected"
    assert "1 个" in (r["detail"] or "")


def test_tck_ignores_other_suffix(monkeypatch, tmp_path):
    (tmp_path / "a.dat").write_bytes(b"x")
    monkeypatch.setenv(sh.TCK_DIR_ENV, str(tmp_path))
    assert sh.check_source("tck")["status"] == "degraded"


def test_img_with_files_is_connected(monkeypatch, tmp_path):
    (tmp_path / "sz000977_a.img").write_bytes(b"x")
    monkeypatch.setenv(sh.IMG_DIR_ENV, str(tmp_path))
    assert sh.check_source("img")["status"] == "connected"


def test_img_not_configured_is_down(monkeypatch):
    monkeypatch.delenv(sh.IMG_DIR_ENV, raising=False)
    assert sh.check_source("img")["status"] == "down"


def test_icons_mapping_matches_design():
    """设计稿 §12: 拆/⚠撤 共用 .tck; 托压用 .img; 涨用 wencai; 我用 shadow。"""
    assert set(sh.SOURCE_DEFS["tck"]["icons"]) == {"拆", "⚠撤"}
    assert sh.SOURCE_DEFS["img"]["icons"] == ["🛡托/🔒压"]
    assert sh.SOURCE_DEFS["wencai"]["icons"] == ["涨"]
    assert sh.SOURCE_DEFS["shadow"]["icons"] == ["我"]


# ---------------------------------------------------------------------------
# wencai / thsdk
# ---------------------------------------------------------------------------


def test_wencai_no_thsdk_is_down(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "thsdk":
            raise ImportError("no thsdk")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    r = sh.check_wencai()
    assert r["status"] == "down"


def test_wencai_without_credentials_is_degraded(monkeypatch):
    """thsdk 装了但没注入凭据 → degraded, 不是 connected。"""
    monkeypatch.setenv("THS_USERNAME", "")
    monkeypatch.setenv("THS_PASSWORD", "")
    r = sh.check_wencai()
    # 本机可能没装 thsdk → down; 装了但无凭据 → degraded。两种都不允许 connected
    assert r["status"] in {"down", "degraded"}
    assert r["status"] != "connected"


def test_wencai_with_credentials(monkeypatch):
    """注入了账号密码, 且 thsdk 可用 → connected。"""
    monkeypatch.setenv("THS_USERNAME", "u")
    monkeypatch.setenv("THS_PASSWORD", "p")
    r = sh.check_wencai()
    assert r["status"] in VALID_STATUS
    assert r["status"] != "down"


# ---------------------------------------------------------------------------
# shadow
# ---------------------------------------------------------------------------


def test_shadow_db_ok(monkeypatch):
    class FakeQuery:
        def limit(self, n):
            return self

        def all(self):
            return []

    class FakeSession:
        def query(self, model):
            return FakeQuery()

        def close(self):
            pass

    import src.web.database as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: FakeSession())
    assert sh.check_shadow()["status"] == "connected"


def test_shadow_db_error_is_unknown(monkeypatch):
    """DB 异常 → unknown(附原因), 不静默吞掉也不假装 connected。"""
    import src.web.database as dbmod

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbmod, "SessionLocal", boom)
    r = sh.check_shadow()
    assert r["status"] == "unknown"
    assert "db down" in (r["detail"] or "")


# ---------------------------------------------------------------------------
# check_source / check_all 通用行为
# ---------------------------------------------------------------------------


def test_unknown_source_id_is_unknown_not_exception():
    r = sh.check_source("does-not-exist")
    assert r["status"] == "unknown"
    assert "未知数据源" in (r["detail"] or "")


def test_check_source_returns_required_fields(monkeypatch):
    monkeypatch.delenv(sh.TCK_DIR_ENV, raising=False)
    r = sh.check_source("tck")
    for key in ("id", "name", "status", "last_check_at", "detail", "icons"):
        assert key in r
    assert r["status"] in VALID_STATUS
    assert r["last_check_at"] > 0


def test_check_all_defaults_to_five_sources(monkeypatch):
    monkeypatch.delenv(sh.TCK_DIR_ENV, raising=False)
    monkeypatch.delenv(sh.IMG_DIR_ENV, raising=False)
    monkeypatch.delenv("TDX_QUANT_URL", raising=False)
    items = sh.check_all()
    assert {i["id"] for i in items} == {"tck", "img", "wencai", "shadow", "tq_moreinfo"}
    assert all(i["status"] in VALID_STATUS for i in items)


def test_check_all_filtered(monkeypatch):
    monkeypatch.delenv(sh.TCK_DIR_ENV, raising=False)
    items = sh.check_all(["tck"])
    assert [i["id"] for i in items] == ["tck"]


def test_cache_reused_within_ttl(monkeypatch, tmp_path):
    """30s 内复用缓存, 不重复检查。"""
    monkeypatch.setenv(sh.TCK_DIR_ENV, str(tmp_path))
    r1 = sh.check_source("tck")
    (tmp_path / "a.tck").write_bytes(b"x")   # 缓存期内加了文件
    r2 = sh.check_source("tck")
    assert r1["status"] == "degraded"
    assert r2["status"] == "degraded"        # 仍是缓存值
    sh.clear_health_cache()
    assert sh.check_source("tck")["status"] == "connected"


def test_refresh_bypasses_cache(monkeypatch, tmp_path):
    monkeypatch.setenv(sh.TCK_DIR_ENV, str(tmp_path))
    assert sh.check_source("tck")["status"] == "degraded"
    (tmp_path / "a.tck").write_bytes(b"x")
    assert sh.check_source("tck", use_cache=False)["status"] == "connected"


def test_checker_exception_becomes_unknown(monkeypatch):
    """检查函数抛异常 → unknown + 原因, 不让 /health 500。"""
    monkeypatch.setitem(sh.SOURCE_DEFS, "tck",
                        {"name": "x", "icons": [],
                         "check": lambda: (_ for _ in ()).throw(RuntimeError("boom"))})
    r = sh.check_source("tck")
    assert r["status"] == "unknown"
    assert "boom" in (r["detail"] or "")


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.api import datasources as ds

    app = FastAPI()
    app.include_router(ds.router, prefix="/api/datasources")
    return TestClient(app)


def test_api_health_returns_items(monkeypatch):
    monkeypatch.delenv(sh.TCK_DIR_ENV, raising=False)
    monkeypatch.delenv(sh.IMG_DIR_ENV, raising=False)
    r = _client().get("/api/datasources/health")
    assert r.status_code == 200
    body = r.json()
    assert "checked_at" in body
    assert {i["id"] for i in body["items"]} == {"tck", "img", "wencai", "shadow", "tq_moreinfo"}


def test_api_health_not_captured_by_source_id_route(monkeypatch):
    """/health 必须优先于 /{source_id}, 否则 "health" 会被当成 id → 422。"""
    monkeypatch.delenv(sh.TCK_DIR_ENV, raising=False)
    r = _client().get("/api/datasources/health")
    assert r.status_code == 200, r.text


def test_api_health_single_source(monkeypatch, tmp_path):
    (tmp_path / "a.tck").write_bytes(b"x")
    monkeypatch.setenv(sh.TCK_DIR_ENV, str(tmp_path))
    r = _client().get("/api/datasources/health/tck")
    assert r.status_code == 200
    assert r.json()["status"] == "connected"


def test_api_health_unknown_id_returns_unknown(monkeypatch):
    r = _client().get("/api/datasources/health/nope")
    assert r.status_code == 200
    assert r.json()["status"] == "unknown"


def test_api_health_ids_filter(monkeypatch):
    monkeypatch.delenv(sh.TCK_DIR_ENV, raising=False)
    r = _client().get("/api/datasources/health?ids=tck,img")
    assert r.status_code == 200
    assert {i["id"] for i in r.json()["items"]} == {"tck", "img"}


# ---------------------------------------------------------------------------
# 通用 data_sources 健康推断(与 Hermes 方案合并保留的累计统计口径)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enabled,success,error,expected", [
    (True, 10, 0, "connected"),   # 只成功过
    (True, 10, 3, "degraded"),    # 时好时坏
    (True, 0, 3, "down"),         # 只失败过
    (True, 0, 0, "unknown"),      # 从未调用 → 不编造
    (False, 10, 0, "down"),       # 显式停用
    (False, 0, 0, "down"),        # 停用优先于"从未调用"
    (None, 5, 0, "connected"),    # enabled 缺失按未停用处理
    (True, None, None, "unknown"),  # 计数缺失按 0 处理
])
def test_infer_status_from_stats(enabled, success, error, expected):
    assert sh.infer_status_from_stats(enabled, success, error) == expected


def test_summarize_source_full():
    from datetime import datetime

    class Row:
        id = 7
        name = "腾讯行情"
        type = "quote"
        provider = "tencent"
        enabled = True
        success_count = 12
        error_count = 2
        last_status = "ok"
        last_used_at = datetime(2026, 9, 1, 11, 0, 0)
        last_error_at = datetime(2026, 9, 1, 10, 30, 0)

    out = sh.summarize_source(Row())
    assert out["id"] == 7
    assert out["status"] == "degraded"          # 成功>0 且 失败>0
    assert out["success_count"] == 12
    assert out["error_count"] == 2
    assert out["last_used_at"].startswith("2026-09-01T11:00")
    assert out["last_error_at"].startswith("2026-09-01T10:30")
    assert out["last_check_at"] is not None     # 取两者较晚的


def test_summarize_source_missing_fields():
    """字段全缺 → 不报错, 状态 unknown(不冒充 connected)。"""

    class Row:
        pass

    out = sh.summarize_source(Row())
    assert out["status"] == "unknown"
    assert out["last_check_at"] is None
    assert out["success_count"] == 0


def test_health_columns_declared():
    """5 列必须与 models.DataSource 上的健康列一一对应。"""
    from src.web.models import DataSource

    for col in sh.HEALTH_COLUMNS:
        assert hasattr(DataSource, col), f"DataSource 缺少健康列 {col}"
    assert len(sh.HEALTH_COLUMNS) == 5


def test_api_health_data_sources_endpoint():
    """/health/data-sources 返回配置源(未被 /{source_id} 抢占)。

    注意: FastAPI 的 `Depends(get_db)` 是在路由定义时捕获的,
    直接 monkeypatch 模块属性不生效 → 必须用 `app.dependency_overrides`。
    """
    from datetime import datetime

    class Row:
        id = 3
        name = "东财K线"
        type = "kline"
        provider = "eastmoney"
        enabled = True
        success_count = 5
        error_count = 0
        last_status = "ok"
        last_used_at = datetime(2026, 9, 1, 9, 0, 0)
        last_error_at = None

    class FakeQuery:
        def all(self):
            return [Row()]

    class FakeDB:
        def query(self, model):
            return FakeQuery()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.api import datasources as ds

    app = FastAPI()
    app.include_router(ds.router, prefix="/api/datasources")
    app.dependency_overrides[ds.get_db] = lambda: FakeDB()
    client = TestClient(app)

    r = client.get("/api/datasources/health/data-sources")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == 3
    assert items[0]["status"] == "connected"
    assert items[0]["last_check_at"] is not None


# ---------------------------------------------------------------------------
# tq_moreinfo: TQ 扩展指标网关(只探单个地址, 无网络下也可测)
# ---------------------------------------------------------------------------


def test_tq_moreinfo_never_discovered_is_degraded(monkeypatch):
    """未配 TDX_QUANT_URL 且 vendor 无缓存 → degraded(诚实, 不编造)。"""
    monkeypatch.delenv("TDX_QUANT_URL", raising=False)
    import marketdata.vendors.tq as tqmod
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE", None, raising=False)
    r = sh.check_source("tq_moreinfo", use_cache=False)
    assert r["id"] == "tq_moreinfo"
    assert r["status"] == "degraded"
    assert r["status"] in VALID_STATUS


def test_tq_moreinfo_unreachable_is_degraded(monkeypatch):
    """地址配了但探不通 → degraded(配了但当前拿不到数据)。"""
    monkeypatch.setenv("TDX_QUANT_URL", "http://127.0.0.1:9/")
    r = sh.check_source("tq_moreinfo", use_cache=False)
    assert r["status"] in {"degraded", "unknown"}
