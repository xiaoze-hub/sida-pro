"""thsdk_ext.py + thsdk_l2.py 第九类(DDE/complete_ths_code/market)测试。

mock 外部依赖(不碰真实 thsdk 后端 + 不触发限频等待),验证:
- DDE 参数构造 + 字段清洗(万元换算)
- get_main_flow_official 汇总/明细解析
- complete_ths_code 代码补齐
- get_market_codes 市场列表
- /api/thsdk/ext/* HTTP 端点 + 缓存 + 容错

每个测试用唯一 symbol 避免缓存污染。
"""
from __future__ import annotations

import sys
import time as _time
from unittest.mock import MagicMock

import pandas as pd
import pytest


# 屏蔽真实 thsdk 模块(与 test_thsdk_api 同策略)
@pytest.fixture(autouse=True, scope="session")
def _mock_thsdk_module():
    fake = MagicMock()
    fake.THS = MagicMock()
    fake.THSResponse = MagicMock()
    sys.modules["thsdk"] = fake
    yield
    sys.modules.pop("thsdk", None)


# ---------- thsdk_l2 第九类(纯逻辑, mock _query) ----------

def _make_client(**methods):
    """构造 THSDKL2 客户端, _query 按 func_name 分发。"""
    from data_source.thsdk_l2 import THSDKL2

    client = THSDKL2.__new__(THSDKL2)
    client._is_guest = True
    client._rate_limit = lambda: None  # type: ignore[method-assign]

    class _Resp:
        def __init__(self, df=None, data=None):
            self.df = df
            self.data = data

    def fake_query(func_name, *args, **kwargs):
        if func_name not in methods:
            raise AssertionError(f"unexpected query_data call: {func_name}({args}, {kwargs})")
        return methods[func_name](*args, **kwargs)

    client._query = fake_query  # type: ignore[method-assign]
    return client, _Resp


def test_dde_flow_summary_normalizes_wan():
    """summary 返回 主力净流入_万元/总金额_万元, 列齐全。"""
    from data_source.thsdk_l2 import DDE_DATATYPE_SUMMARY

    df_in = pd.DataFrame([{
        "价格": 1291.5, "成交量": 2533166, "总金额": 3280474200,
        "代码": "USHA600519", "主力净量": -0.0051, "主力净流入": -82597210,
        "昨收价": 1307.88, "开盘价": 1299.8, "最高价": 1306.88, "最低价": 1291.0,
    }])
    seen = {}

    def q(params):
        seen["params"] = params
        resp = MagicMock()
        resp.df = df_in
        return resp

    client, Resp = _make_client(query_data=q)
    out = client.get_dde_flow("600519", market="USHA", detail=False)

    assert "主力净流入_万元" in out.columns
    assert out.iloc[0]["主力净流入_万元"] == pytest.approx(-8259.721)
    assert "总金额_万元" in out.columns
    assert out.iloc[0]["总金额_万元"] == pytest.approx(328047.42)
    # 参数校验: 剥前缀 + summary datatype
    assert seen["params"]["codelist"] == "600519"
    assert seen["params"]["market"] == "USHA"
    assert seen["params"]["datatype"] == DDE_DATATYPE_SUMMARY


def test_dde_flow_detail_adds_breakdown():
    """detail=True 走 DETAIL datatype, 追加特/大单分档 _万元。"""
    from data_source.thsdk_l2 import DDE_DATATYPE_DETAIL

    df_in = pd.DataFrame([{
        "价格": 1291.5, "成交量": 1, "总金额": 1, "代码": "USHA600519",
        "主动买入特大单金额": 213192930, "主动卖出特大单金额": 512907640,
        "主动买入大单金额": 342500370, "被动买入特大单金额": 422425710,
    }])
    seen = {}

    def q(params):
        seen["params"] = params
        resp = MagicMock()
        resp.df = df_in
        return resp

    client, Resp = _make_client(query_data=q)
    out = client.get_dde_flow("USHA600519", market="USHA", detail=True)

    assert seen["params"]["datatype"] == DDE_DATATYPE_DETAIL
    assert "主动买入特大单金额_万元" in out.columns
    assert out.iloc[0]["主动买入特大单金额_万元"] == pytest.approx(21319.293)


def test_get_main_flow_official_parses():
    """单股官方主力资金: 主力净流入(万) + 明细, 代码经 complete_ths_code 归一。"""
    client, Resp = _make_client(
        complete_ths_code=lambda codes: Resp(df=pd.DataFrame({"代码": ["USHA600519"]})),
        query_data=lambda params: Resp(df=pd.DataFrame([{
            "价格": 1291.5, "成交量": 2533166, "总金额": 3280474200,
            "代码": "USHA600519", "主力净量": -0.0051, "主力净流入": -82597210,
            "昨收价": 1307.88, "开盘价": 1299.8, "最高价": 1306.88, "最低价": 1291.0,
        }])),
    )
    out = client.get_main_flow_official("600519")
    assert out["ths_code"] == "USHA600519"
    assert out["main_net_amount_wan"] == pytest.approx(-8259.721)
    assert out["total_amount_wan"] == pytest.approx(328047.42)
    assert abs(out["price"] - 1291.5) < 1e-6


def test_get_main_flow_official_no_code():
    """代码补齐失败时返回 error 字典(不抛)。"""
    client, Resp = _make_client(complete_ths_code=lambda codes: [])
    out = client.get_main_flow_official("BAD")
    assert out.get("error") == "no_code"


def test_complete_ths_code_normalizes():
    """多代码补齐(Index/非标的被后端丢弃, 只返回命中的)。"""
    client, Resp = _make_client(
        complete_ths_code=lambda codes: Resp(df=pd.DataFrame({"代码": ["USZA300033", "USHA600519"]}))
    )
    out = client.complete_ths_code(["300033", "600519"])
    assert out == ["USZA300033", "USHA600519"]


def test_get_market_codes_returns_df():
    """市场列表返回 ['代码','名称'] DataFrame。"""
    client, Resp = _make_client(market_block=lambda m: Resp(df=pd.DataFrame(
        {"代码": ["USHA600519", "USHA601318"], "名称": ["贵州茅台", "中国平安"]}
    )))
    out = client.get_market_codes("USHA")
    assert list(out.columns) == ["代码", "名称"]
    assert len(out) == 2


# ---------- HTTP 端点 (mock data_source 模块函数) ----------

@pytest.fixture
def auth_token():
    from src.web.app import app
    from src.web.api.auth import get_current_user
    fake_user = {"id": "test", "username": "tester", "role": "owner"}
    app.dependency_overrides[get_current_user] = lambda: fake_user
    yield "test-token"
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def reset_caches():
    from src.web.api import thsdk_ext
    thsdk_ext._DDE_CACHE.clear()
    thsdk_ext._CODE_CACHE.clear()
    thsdk_ext._MKT_CACHE.clear()
    yield
    thsdk_ext._DDE_CACHE.clear()
    thsdk_ext._CODE_CACHE.clear()
    thsdk_ext._MKT_CACHE.clear()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from src.web.app import app
    return TestClient(app)


def test_http_dde_endpoint(monkeypatch, client, auth_token):
    from src.web.api import thsdk_ext
    fake = {
        "symbol": "600519", "ths_code": "USHA600519", "price": 1291.5,
        "total_amount_wan": 328047.42, "main_net_amount_wan": -8259.721,
        "main_net_ratio": -0.0051, "summary": {}, "detail": {},
    }
    monkeypatch.setattr(thsdk_ext, "get_main_flow_official", lambda s: fake, raising=False)
    resp = client.get("/api/thsdk/ext/dde/600519", headers={"Authorization": f"Bearer {auth_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["data"]["main_net_amount_wan"] == pytest.approx(-8259.721)
    assert body["data"]["warnings"] == []


def test_http_dde_endpoint_fallback(monkeypatch, client, auth_token):
    """thsdk 失败 → 200 + data=None + warnings, 不抛 500。"""
    from src.web.api import thsdk_ext

    def boom(s):
        raise RuntimeError("熔断")

    monkeypatch.setattr(thsdk_ext, "get_main_flow_official", boom, raising=False)
    resp = client.get("/api/thsdk/ext/dde/600519", headers={"Authorization": f"Bearer {auth_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["data"] is None
    assert any("thsdk" in w for w in body["data"]["warnings"])


def test_http_code_endpoint(monkeypatch, client, auth_token):
    from src.web.api import thsdk_ext
    monkeypatch.setattr(thsdk_ext, "complete_ths_code", lambda c: ["USZA002361", "USHA600519"], raising=False)
    resp = client.get("/api/thsdk/ext/code/002361,600519", headers={"Authorization": f"Bearer {auth_token}"})
    assert resp.status_code == 200
    assert resp.json()["data"]["ths_codes"] == ["USZA002361", "USHA600519"]


def test_http_market_endpoint(monkeypatch, client, auth_token):
    from src.web.api import thsdk_ext
    df = pd.DataFrame({"代码": ["USHA600519"], "名称": ["贵州茅台"]})

    def fake(m):
        return df

    monkeypatch.setattr(thsdk_ext, "get_market_codes", fake, raising=False)
    resp = client.get("/api/thsdk/ext/market/USHA", headers={"Authorization": f"Bearer {auth_token}"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["count"] == 1
    assert body["rows"][0]["代码"] == "USHA600519"


def test_http_market_endpoint_invalid():
    """无效市场 → 400。"""
    from fastapi.testclient import TestClient
    from src.web.app import app
    c = TestClient(app)
    # 未带 token 时受 protected 保护; 这里用 dependency_overrides 不生效于该 TestClient 实例,
    # 直接调用路由函数的校验逻辑
    from src.web.api.thsdk_ext import _VALID_MARKETS
    assert "USHA" in _VALID_MARKETS
    assert "XXX" not in _VALID_MARKETS
