"""方案B: 通达信(TDX)原生板块目录 + 批量实时 — /boards 接口 layer (2026-09-10 老板拍板)。

口径:
- 板块目录 = TDX get_sector_list 587 个(881xxx 二级行业 / 880xxx 概念), 排除 880081/880082
  (轮动趋势/板块趋势, 非板块);
- 实时批量 = get_pricevol(现价/昨收/量) + AMO 公式(成交额, 万元) + SUPAMO(主力资金, 万元);
- 涨速由自身轮询价差自算(compute_speed), 量比/涨速缺失一律 None(不猜);
- source=auto 时 TDX 不可用回落 thsdk 老链路(行为与 v0.5.52 一致)。
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.core.tdx_boards as tdx
import src.web.api.boards as boards_api
from src.web import models as M  # noqa: F401
from src.web.database import Base, get_db
from src.web.models import Board, BoardDaily


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(boards_api.router, prefix="/api/boards")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), Session


def _seed(Session):
    db = Session()
    db.add(Board(block_code="URFI0001", name="半导体", board_type="industry"))
    db.add(BoardDaily(block_code="URFI0001", date=date(2026, 9, 10), change_pct=1.0, fund_net=100.0, volume=1000.0))
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def _clear_caches():
    boards_api._clear_live_cache()
    tdx._clear_caches()
    yield
    boards_api._clear_live_cache()
    tdx._clear_caches()


# ── 纯函数 ────────────────────────────────────────────────────────────────

def test_is_tdx_block_code_and_type():
    assert tdx.is_tdx_block_code("881290.SH")
    assert tdx.is_tdx_block_code("880741.SH")
    assert not tdx.is_tdx_block_code("URFI881155")
    assert not tdx.is_tdx_block_code("600150.SH")
    assert tdx.block_type_of("881290.SH") == "industry"
    assert tdx.block_type_of("880741.SH") == "concept"
    assert tdx.block_type_of("600150.SH") is None


def test_compute_speed_window():
    now = 1_000_000.0
    # 5 分钟前 100 → 现在 101 = +1.0%
    assert tdx.compute_speed([(now - 300, 100.0), (now, 101.0)], now) == pytest.approx(1.0)
    # 样本太旧(超出容差) → None, 不猜
    assert tdx.compute_speed([(now - 900, 100.0), (now, 101.0)], now) is None
    # 单点 → None
    assert tdx.compute_speed([(now, 101.0)], now) is None
    # 基准价为 0 → None(防除零)
    assert tdx.compute_speed([(now - 300, 0.0), (now, 101.0)], now) is None


def test_tdx_code_to_symbol():
    assert tdx.tdx_code_to_symbol("600150.SH") == "600150"
    assert tdx.tdx_code_to_symbol("000001.SZ") == "000001"
    assert tdx.tdx_code_to_symbol("920230.BJ") == "920230"
    assert tdx.tdx_code_to_symbol("00700.HK") is None


# ── RPC 解析(打桩 _rpc, 跑真实解析逻辑) ────────────────────────────────────

def _fake_rpc(payloads):
    calls = []

    def _rpc(method, params, timeout=6.0):
        calls.append((method, params))
        if method not in payloads:
            raise RuntimeError(f"no fake for {method}")
        return payloads[method]

    return _rpc, calls


def test_sector_items_filters_and_classifies(monkeypatch):
    payloads = {
        "get_sector_list": [
            {"Code": "881290.SH", "Name": "航海装备"},
            {"Code": "880741.SH", "Name": "代糖概念"},
            {"Code": "880081.SH", "Name": "轮动趋势"},
        ]
    }
    fake, _ = _fake_rpc(payloads)
    monkeypatch.setattr(tdx, "_rpc", fake)
    items = tdx.sector_items()
    assert [i["code"] for i in items] == ["880741.SH", "881290.SH"]
    assert {i["code"]: i["board_type"] for i in items} == {
        "881290.SH": "industry",
        "880741.SH": "concept",
    }


def test_board_quotes_merges_units_and_tolerates_partial(monkeypatch):
    payloads = {
        "get_pricevol": {
            "881290.SH": {"LastClose": "1507.53", "Now": "1541.39", "Volume": "7747567"},
            "880741.SH": {"LastClose": "1180.00", "Now": "1141.89", "Volume": "19630568"},
        },
        # AMO 公式: AMOW = 成交额(万元)
        "formula_process_mul_zb": {
            "881290.SH": {"AMOW": ["1566205.63"]},
            "880741.SH": {"AMOW": ["1329547.63"]},
        },
    }
    fake, calls = _fake_rpc(payloads)
    monkeypatch.setattr(tdx, "_rpc", fake)
    q = tdx.board_quotes(["881290.SH", "880741.SH"])
    a = q["881290.SH"]
    assert a["price"] == pytest.approx(1541.39)
    assert a["change_pct"] == pytest.approx(2.246, abs=0.01)
    assert a["amount"] == pytest.approx(1566205.63 * 1e4)  # 万元 → 元
    assert a["volume"] == pytest.approx(7747567)
    # SUPAMO 缺失(抛 no fake) → fund_net=None, 不影响其余字段
    assert a["fund_net"] is None


def test_board_quotes_fund_from_supamo(monkeypatch):
    payloads = {
        "get_pricevol": {"881290.SH": {"LastClose": "1507.53", "Now": "1541.39", "Volume": "100"}},
        "formula_process_mul_zb": {
            "881290.SH": {"AMOW": ["1566205.63"], "主力资金": ["28254.36"]},
        },
    }

    def _rpc(method, params, timeout=6.0):
        return payloads[method]

    monkeypatch.setattr(tdx, "_rpc", _rpc)
    q = tdx.board_quotes(["881290.SH"])
    assert q["881290.SH"]["fund_net"] == pytest.approx(28254.36 * 1e4)


def test_name_map_from_stock_list(monkeypatch):
    payloads = {
        "get_stock_list": [
            {"Code": "600150.SH", "Name": "中国船舶"},
            {"Code": "03877.HK", "Name": "中国船舶租赁"},
        ]
    }
    fake, _ = _fake_rpc(payloads)
    monkeypatch.setattr(tdx, "_rpc", fake)
    names = tdx.name_map()
    assert names["600150.SH"] == "中国船舶"
    assert "03877.HK" not in names  # 仅 A股(HK 由 tdx_code_to_symbol 过滤口径一致)


def test_amount_baseline_wan_to_yuan(monkeypatch):
    """日线 Amount 与 AMOW 同为万元 → 基准必须换算为元(否则量比代理虚高 1e4 倍)。"""
    payloads = {
        "get_market_data": {
            "881290.SH": {
                "Date": ["20260903", "20260904", "20260907", "20260908", "20260909", "20260910"],
                # 前 5 日: 100/200/300/400/500 万元; 当日(末位)不参与
                "Amount": ["1000000", "2000000", "3000000", "4000000", "5000000", "9999000"],
            }
        }
    }
    fake, _ = _fake_rpc(payloads)
    monkeypatch.setattr(tdx, "_rpc", fake)
    base = tdx.amount_baseline(["881290.SH"])
    assert base["881290.SH"] == pytest.approx(3000000 * 1e4)  # 均值 300万 → 元


def test_volume_ratio_proxy():
    # 今日 600万, 基准 300万×进度 1.0 → 2.0
    assert tdx.volume_ratio_proxy(6000000.0, 3000000.0, 1.0) == pytest.approx(2.0)
    # 缺基准/非正 → None(不猜)
    assert tdx.volume_ratio_proxy(6000000.0, None, 1.0) is None
    assert tdx.volume_ratio_proxy(6000000.0, 0.0, 1.0) is None
    assert tdx.volume_ratio_proxy(None, 3000000.0, 1.0) is None


# ── 接口层 ────────────────────────────────────────────────────────────────

def _wire_tdx(monkeypatch, *, fail: bool = False):
    payloads = {
        "get_sector_list": [
            {"Code": "881290.SH", "Name": "航海装备"},
            {"Code": "880741.SH", "Name": "代糖概念"},
        ],
        "get_pricevol": {
            "881290.SH": {"LastClose": "1507.53", "Now": "1541.39", "Volume": "100"},
            "880741.SH": {"LastClose": "1180.00", "Now": "1141.89", "Volume": "200"},
            "600150.SH": {"LastClose": "30.00", "Now": "31.20", "Volume": "300"},
        },
        "formula_process_mul_zb": {
            "881290.SH": {"AMOW": ["1566205.63"], "主力资金": ["28254.36"]},
            "880741.SH": {"AMOW": ["1329547.63"], "主力资金": ["-88462.71"]},
        },
        "get_stock_list_in_sector": ["600150.SH", "000001.SZ"],
        "get_stock_list": [
            {"Code": "600150.SH", "Name": "中国船舶"},
            {"Code": "000001.SZ", "Name": "平安银行"},
        ],
    }

    def _rpc(method, params, timeout=6.0):
        if fail:
            raise RuntimeError("tdx down")
        if method not in payloads:
            raise RuntimeError(f"no fake for {method}")
        return payloads[method]

    monkeypatch.setattr(tdx, "_rpc", _rpc)
    monkeypatch.setattr(tdx, "data_fresh", lambda: True)


def test_heatmap_source_tdx(monkeypatch):
    _wire_tdx(monkeypatch)
    monkeypatch.setattr(boards_api, "in_trading_window", lambda: True)
    client, Session = _client()
    _seed(Session)
    r = client.get("/api/boards/heatmap?type=industry&source=tdx&live=1")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"] == "tdx"
    assert d["live"] is True and d["count"] == 1
    it = d["items"][0]
    assert it["block_code"] == "881290.SH" and it["name"] == "航海装备"
    assert it["change_pct"] == pytest.approx(2.246, abs=0.01)
    assert it["fund_net"] == pytest.approx(28254.36 * 1e4)
    assert it["volume"] == pytest.approx(1566205.63 * 1e4)
    assert it["live"] is True


def test_heatmap_source_auto_falls_back_to_ths(monkeypatch):
    _wire_tdx(monkeypatch, fail=True)
    monkeypatch.setattr(boards_api, "in_trading_window", lambda: False)
    client, Session = _client()
    _seed(Session)
    r = client.get("/api/boards/heatmap?type=industry&source=auto")
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "ths"
    assert d["items"][0]["block_code"] == "URFI0001"


def test_detail_tdx_branch(monkeypatch):
    _wire_tdx(monkeypatch)
    client, Session = _client()
    _seed(Session)
    r = client.get("/api/boards/881290.SH")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "航海装备" and d["source"] == "tdx"
    assert d["today"]["change_pct"] == pytest.approx(2.246, abs=0.01)
    assert d["today"]["fund_net"] == pytest.approx(28254.36 * 1e4)


def test_constituents_tdx_branch_with_change(monkeypatch):
    _wire_tdx(monkeypatch)
    client, Session = _client()
    _seed(Session)
    r = client.get("/api/boards/881290.SH/constituents")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 2
    rows = {x["symbol"]: x for x in d["items"]}
    assert rows["600150"]["name"] == "中国船舶"
    assert rows["600150"]["change_pct"] == pytest.approx(4.0, abs=0.01)
    assert rows["000001"]["change_pct"] is None  # 快照缺失 → None(不猜)
