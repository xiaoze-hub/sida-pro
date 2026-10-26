# -*- coding: utf-8 -*-
"""设置中心密钥安全 P0 回归测试(docs/design/20261023_06_pages_settings.md W-3 / W-4 / 3.7)

覆盖五条验收线:
1. PUT /settings/{secret} 收到 ``"********"`` → 400, DB 保持原值
2. PUT 收到 ``"  ********  "``(带空格) / ``"*********"`` → 同样 400(旧的精确相等判据会被绕过)
3. 正常新值可写入(且两侧空白被 strip)
4. GET /datasources 的 config 里凭证子键已脱敏 + 带 ``has_<key>`` 标记
5. PUT /settings/{secret} 的响应体不含明文

另外补两条推导出来的守卫:
- 敏感键写空值必须显式 ``clear=true``, 否则 400
- PUT /datasources 收到掩码回填时按"未修改"处理(否则前端把 ******** 提交回来就毁凭证)

隔离: 全内存 SQLite; 自建 app 只挂 settings / datasources 两个 router;
marketdata 客户端与孤儿判定全部打桩, **不发真实网络请求、不碰生产库**。
"""
from __future__ import annotations

import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import AppSettings, Base, DataSource
from src.web.database import get_db
from src.web.api import datasources as ds_api
from src.web.api import settings as settings_api
from src.web.api.settings import SECRET_MASK, is_mask_placeholder

# 假凭证(测试专用, 不是任何真实 key)
FAKE_TOKEN = "sk-test-1234"
FAKE_COOKIES = "xq_a_token=fake-not-real"
FAKE_OLD_TOKEN = "sk-test-old-0000"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def app(db, monkeypatch):
    # 打掉 marketdata 客户端(route 内部 lazy import)与孤儿判定(会 import server 模块)
    stub = types.ModuleType("src.core.marketdata_client")

    class _MD:
        def health(self):
            return {}

    setattr(stub, "get_market_data", lambda: _MD())
    monkeypatch.setitem(sys.modules, "src.core.marketdata_client", stub)
    monkeypatch.setattr(ds_api, "_is_orphan", lambda *_a, **_k: False)

    application = FastAPI()
    application.include_router(settings_api.router, prefix="/settings")
    application.include_router(ds_api.router, prefix="/datasources")

    def _override_db():
        yield db

    application.dependency_overrides[get_db] = _override_db
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def secret_row(db):
    row = AppSettings(key="wudao_mcp_token", value=FAKE_OLD_TOKEN, description="")
    db.add(row)
    db.commit()
    return row


def _stored(db, key: str) -> str:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    return (row.value if row else "") or ""


# ── 判据单元: is_mask_placeholder ────────────────────────────────────────────

def test_mask_placeholder_shape():
    assert is_mask_placeholder("********")
    assert is_mask_placeholder("  ********  ")
    assert is_mask_placeholder("*********")   # 多敲一个星号
    assert is_mask_placeholder("········")    # 圆点代替星号
    # 长度 <6 的单星号不算掩码占位(判据要求 ≥6, 见 SECRET_MASK 长度)
    assert not is_mask_placeholder("*")

def test_mask_placeholder_rejects_real_values():
    assert not is_mask_placeholder(FAKE_TOKEN)
    assert not is_mask_placeholder("sk-****1234")  # 带前后缀 => 是真值形态, 允许写
    assert not is_mask_placeholder("")
    assert not is_mask_placeholder(None)


# ── 验收 ①②: 掩码(含带空格)一律拒写, 且 DB 不被覆盖 ──────────────────────

@pytest.mark.parametrize("bad", [
    SECRET_MASK,
    f"  {SECRET_MASK}  ",
    f"\n{SECRET_MASK}\t",
    "*********",
])
def test_put_secret_rejects_mask_and_keeps_old_value(client, db, secret_row, bad):
    r = client.put("/settings/wudao_mcp_token", json={"value": bad})
    assert r.status_code == 400, r.text
    assert "掩码" in r.json()["detail"]
    assert _stored(db, "wudao_mcp_token") == FAKE_OLD_TOKEN


# ── 验收 ③: 正常新值可写 ────────────────────────────────────────────────────

def test_put_secret_accepts_real_value(client, db, secret_row):
    r = client.put("/settings/wudao_mcp_token", json={"value": f"  {FAKE_TOKEN}  "})
    assert r.status_code == 200, r.text
    # 两侧空白被 strip, 否则带尾随空格的真 token 会静默失效
    assert _stored(db, "wudao_mcp_token") == FAKE_TOKEN


def test_put_secret_creates_row_when_absent(client, db):
    r = client.put("/settings/zhitu_token", json={"value": FAKE_TOKEN})
    assert r.status_code == 200, r.text
    assert _stored(db, "zhitu_token") == FAKE_TOKEN


# ── 验收 ⑤: PUT 敏感键响应不回显明文 ───────────────────────────────────────

def test_put_secret_response_contains_no_plaintext(client, secret_row):
    r = client.put("/settings/wudao_mcp_token", json={"value": FAKE_TOKEN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"key": "wudao_mcp_token", "updated": True, "masked": SECRET_MASK}
    assert FAKE_TOKEN not in r.text


def test_put_secret_response_masked_not_leaked_via_or_update(client, db, secret_row):
    """写回同一个 secret 后, GET /settings 也只给掩码。"""
    client.put("/settings/wudao_mcp_token", json={"value": FAKE_TOKEN})
    rows = client.get("/settings").json()
    row = next(x for x in rows if x["key"] == "wudao_mcp_token")
    assert row["value"] == SECRET_MASK
    assert FAKE_TOKEN not in str(rows)


# ── 加固: 空值必须显式 clear, 非敏感键不受影响 ──────────────────────────────

def test_put_secret_empty_without_clear_rejected(client, db, secret_row):
    r = client.put("/settings/wudao_mcp_token", json={"value": "   "})
    assert r.status_code == 400, r.text
    assert _stored(db, "wudao_mcp_token") == FAKE_OLD_TOKEN


def test_put_secret_empty_with_clear_flag_allowed(client, db, secret_row):
    r = client.put("/settings/wudao_mcp_token", json={"value": "", "clear": True})
    assert r.status_code == 200, r.text
    assert _stored(db, "wudao_mcp_token") == ""


def test_put_non_secret_key_still_plain(client, db):
    r = client.put("/settings/stock_link_platform", json={"value": "xueqiu"})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "xueqiu"
    assert _stored(db, "stock_link_platform") == "xueqiu"


def test_http_proxy_not_masked(client, db, monkeypatch):
    """http_proxy 已移出敏感集合(代理地址不是凭证), 读写都应明文。"""
    # 端点会 lazy import server.apply_proxy_env 去改进程级 env —— 测试里必须打桩,
    # 否则会把 HTTP_PROXY 泄漏给同一 session 后续用例(它们会去连 10.0.0.2)。
    stub = types.ModuleType("server")
    setattr(stub, "apply_proxy_env", lambda _v: None)
    monkeypatch.setitem(sys.modules, "server", stub)

    r = client.put("/settings/http_proxy", json={"value": "http://10.0.0.2:7890"})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "http://10.0.0.2:7890"
    rows = client.get("/settings").json()
    row = next(x for x in rows if x["key"] == "http_proxy")
    assert row["value"] == "http://10.0.0.2:7890"


# ── 验收 ④: GET /datasources 凭证脱敏 + has_* 标记 ─────────────────────────

@pytest.fixture()
def tushare_source(db):
    src = DataSource(
        name="Tushare",
        type="kline",
        provider="tushare",
        config={"token": FAKE_TOKEN, "base_url": "https://api.example.test", "timeout": 5},
        enabled=True,
        priority=0,
    )
    db.add(src)
    db.commit()
    return src


def test_get_datasources_masks_secret_subkeys(client, tushare_source):
    r = client.get("/datasources")
    assert r.status_code == 200, r.text
    item = r.json()[0]
    assert item["config"]["token"] == SECRET_MASK
    assert item["has_token"] is True
    # 非凭证字段必须保留, 否则前端表单回填不了
    assert item["config"]["base_url"] == "https://api.example.test"
    assert item["config"]["timeout"] == 5
    # 响应全文不得出现明文
    assert FAKE_TOKEN not in r.text


def test_get_datasources_masks_api_key_pool_and_cookies(client, db):
    db.add(DataSource(
        name="雪球", type="news", provider="xueqiu",
        config={"cookies": FAKE_COOKIES, "api_keys": [FAKE_TOKEN, "sk-test-5678"]},
        enabled=True, priority=0,
    ))
    db.commit()
    r = client.get("/datasources")
    item = r.json()[0]
    assert item["config"]["cookies"] == SECRET_MASK
    assert item["config"]["api_keys"] == [SECRET_MASK, SECRET_MASK]
    assert item["key_count"] == 2          # 条数仍准确
    assert item["has_cookies"] is True and item["has_api_keys"] is True
    assert FAKE_COOKIES not in r.text and FAKE_TOKEN not in r.text


def test_get_datasources_unconfigured_secret_has_false_flag(client, db):
    db.add(DataSource(name="空源", type="kline", provider="tushare",
                      config={"token": ""}, enabled=True, priority=0))
    db.commit()
    item = client.get("/datasources").json()[0]
    assert item["has_token"] is False
    assert item["config"]["token"] == ""


# ── PUT /datasources 掩码回填守卫(否则前端把掩码提交回来就毁凭证) ──────────

def test_put_datasource_masked_config_keeps_real_token(client, db, tushare_source):
    r = client.put(f"/datasources/{tushare_source.id}", json={
        "priority": 3,
        "config": {"token": SECRET_MASK, "base_url": "https://api.example.test", "timeout": 5},
    })
    assert r.status_code == 200, r.text
    db.expire_all()
    row = db.query(DataSource).filter(DataSource.id == tushare_source.id).first()
    assert row.config["token"] == FAKE_TOKEN      # 真值没被星号覆盖
    assert row.priority == 3


def test_put_datasource_missing_secret_key_keeps_old(client, db, tushare_source):
    r = client.put(f"/datasources/{tushare_source.id}", json={
        "config": {"base_url": "https://api.example.test", "timeout": 9},
    })
    assert r.status_code == 200, r.text
    db.expire_all()
    row = db.query(DataSource).filter(DataSource.id == tushare_source.id).first()
    assert row.config["token"] == FAKE_TOKEN
    assert row.config["timeout"] == 9


def test_put_datasource_new_secret_value_replaces(client, db, tushare_source):
    r = client.put(f"/datasources/{tushare_source.id}", json={
        "config": {"token": "sk-test-new-9999", "timeout": 5},
    })
    assert r.status_code == 200, r.text
    db.expire_all()
    row = db.query(DataSource).filter(DataSource.id == tushare_source.id).first()
    assert row.config["token"] == "sk-test-new-9999"
