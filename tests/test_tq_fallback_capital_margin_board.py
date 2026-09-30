"""A-2/A-3/A-5 TQ 备源: board_capital_flow / margin / capital_flow (2026-09-30)。

三条均为**备源**(不动主源):
  · A-2 board_capital_flow: 同花顺单点 → TQ SUPAMO 板块主力资金(万元)→ 净额(亿)/1e4;
  · A-3 margin: 东财/FTShare → TQ GP03/GP11/GP12 (万元→元 ×1e4);
  · A-5 capital_flow: 东财/新浪 → TQ get_more_info.Zjl_HB (万元→元 ×1e4)。

钉住的硬约束(全部离线 mock, 不触真实网关):
  ① **单位**: TQ 金额是万元 → 元 ×1e4 / 亿 ÷1e4(漏换算会错 1e4 倍且不报错);
  ② **口径**: TQ 资金类=L2/主力口径=契约 "ths"(非 eastmoney4), 带 caliber + direction_semantics,
     direction_semantics 与 src/core/caliber.py:DIRECTION_THS **逐字一致**(复用契约, 不自创);
  ③ **缺字段留 None(绝不补 0)**: 四档拆分/融券余额/流入流出等 TQ 无 → None;
  ④ **回退**: 网关/表无数据 → 返回 [] 交给下一源, 不抛、不伪造。
"""
from __future__ import annotations

import pytest

import marketdata.vendors.tq as tqmod
from marketdata.symbol import Symbol
from marketdata.vendors.tq import (
    TqBoardCapitalFlowVendor,
    TqCapitalFlowVendor,
    TqMarginVendor,
)

from src.core import caliber as C
from src.bootstrap.datasources import DATA_SOURCE_SEEDS


def _cn(code: str = "002361") -> Symbol:
    return Symbol.parse(code, "CN")


def _seeds(type_: str) -> dict[str, dict]:
    return {s["provider"]: s for s in DATA_SOURCE_SEEDS if s.get("type") == type_}


# --------------------------------------------------------------------------- #
# A-2 board_capital_flow TQ 备源
# --------------------------------------------------------------------------- #


_BOARDS = [
    {"Code": "881290.SH", "Name": "通用设备"},
    {"Code": "881155.SH", "Name": "出版业"},
    {"Code": "880301.SH", "Name": "机器人概念"},
]


def test_board_行业过滤_万元转亿_降序排位(monkeypatch):
    monkeypatch.setattr(tqmod, "sector_list", lambda list_type=1: list(_BOARDS))
    seen_codes: list[list[str]] = []

    def _fm(name, codes, **kw):
        assert name == "SUPAMO"
        seen_codes.append(list(codes))
        return {
            "881290.SH": {"主力资金": "30000.00"},   # 3.0 亿
            "881155.SH": {"主力资金": "-10000.00"},  # -1.0 亿
            "880301.SH": {"主力资金": "99999.00"},   # 概念 → 行业请求里不应出现
        }

    monkeypatch.setattr(tqmod, "formula_mul", _fm)
    rows = TqBoardCapitalFlowVendor().fetch([], {"board_type": "industry"})

    assert [r.board_name for r in rows] == ["通用设备", "出版业"]   # 按净额降序
    assert [r.net_inflow for r in rows] == [3.0, -1.0]             # 万元/1e4 = 亿
    assert [r.rank for r in rows] == [1, 2]
    # 概念板块(880xxx)不该进行业请求
    assert all("880301.SH" not in batch for batch in seen_codes)
    # TQ 只给净额: 流入/流出留空(不补 0)
    assert all(r.inflow is None and r.outflow is None for r in rows)
    assert all(r.source == "tq" for r in rows)


def test_board_口径标签符合契约(monkeypatch):
    monkeypatch.setattr(tqmod, "sector_list", lambda list_type=1: list(_BOARDS))
    monkeypatch.setattr(tqmod, "formula_mul", lambda *a, **k: {"881290.SH": {"主力资金": "30000"}})
    r = TqBoardCapitalFlowVendor().fetch([], {"board_type": "industry"})[0]
    assert r.caliber == "ths"                                   # L2/主力口径, 不是 eastmoney4
    assert r.direction_semantics == C.DIRECTION_THS             # 逐字复用契约常量
    C.require_directional is not None                           # 契约出口可复用(非 tick 会抛)


def test_board_无数据不补0_返回空交给下一源(monkeypatch):
    monkeypatch.setattr(tqmod, "sector_list", lambda list_type=1: list(_BOARDS))
    monkeypatch.setattr(tqmod, "formula_mul", lambda *a, **k: {})  # 网关无数据
    assert TqBoardCapitalFlowVendor().fetch([], {"board_type": "industry"}) == []


def test_board_公式异常回退到空(monkeypatch):
    monkeypatch.setattr(tqmod, "sector_list", lambda list_type=1: list(_BOARDS))

    def _boom(*a, **k):
        raise RuntimeError("TQ 隧道断开")

    monkeypatch.setattr(tqmod, "formula_mul", _boom)
    assert TqBoardCapitalFlowVendor().fetch([], {"board_type": "industry"}) == []


def test_board_板块目录为空回退到空(monkeypatch):
    monkeypatch.setattr(tqmod, "sector_list", lambda list_type=1: [])
    monkeypatch.setattr(tqmod, "formula_mul", lambda *a, **k: {"881290.SH": {"主力资金": "1"}})
    assert TqBoardCapitalFlowVendor().fetch([], {"board_type": "industry"}) == []


# --------------------------------------------------------------------------- #
# A-3 margin TQ 备源
# --------------------------------------------------------------------------- #


def _fake_gp_margin(tables, code, *, start_time="", end_time=""):
    assert tables == ["GP03", "GP11", "GP12"]
    return {
        "GP03": [{"Date": "20260925", "Value": ["12345.67", "50000"]}],   # 融资余额万元/融券余量股
        "GP11": [{"Date": "20260925", "Value": ["200.50", "180.25"]}],    # 融资买入/偿还 万元
        "GP12": [{"Date": "20260925", "Value": ["3000", "2500"]}],        # 融券卖出/偿还 股
    }


def test_margin_万元转元_融券余额留空(monkeypatch):
    monkeypatch.setattr(tqmod, "gp_series", _fake_gp_margin)
    rows = TqMarginVendor().fetch([_cn()], {})
    assert len(rows) == 1
    r = rows[0]
    assert r.date == "2026-09-25"
    assert r.symbol == "002361"
    assert r.rz_balance == pytest.approx(12345.67 * 1e4)   # 万元 → 元
    assert r.rz_buy == pytest.approx(200.50 * 1e4)
    assert r.rz_repay == pytest.approx(180.25 * 1e4)
    # TQ 只给融券余量(股), 不给融券余额(元) → None(禁补 0); 据此 total_balance 不臆造
    assert r.rq_balance is None
    assert r.total_balance is None
    assert r.rq_sell_vol == 3000.0 and r.rq_repay_vol == 2500.0  # 股, 不换算
    assert r.source == "tq"


def test_margin_取最新一期(monkeypatch):
    def _gp(tables, code, *, start_time="", end_time=""):
        return {
            "GP03": [
                {"Date": "20260924", "Value": ["1.00"]},
                {"Date": "20260925", "Value": ["22222.00"]},
            ]
        }

    monkeypatch.setattr(tqmod, "gp_series", _gp)
    r = TqMarginVendor().fetch([_cn()], {})[0]
    assert r.date == "2026-09-25"
    assert r.rz_balance == pytest.approx(22222.0 * 1e4)


def test_margin_全表空时回退且不补0(monkeypatch):
    monkeypatch.setattr(tqmod, "gp_series", lambda *a, **k: {})
    assert TqMarginVendor().fetch([_cn()], {}) == []


def test_margin_网关异常回退到空(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("TQ 隧道断开")

    monkeypatch.setattr(tqmod, "gp_series", _boom)
    assert TqMarginVendor().fetch([_cn()], {}) == []


def test_margin_非CN市场不处理(monkeypatch):
    monkeypatch.setattr(tqmod, "gp_series", _fake_gp_margin)
    assert TqMarginVendor().fetch([Symbol.parse("AAPL")], {}) == []


# --------------------------------------------------------------------------- #
# A-5 capital_flow TQ 备源
# --------------------------------------------------------------------------- #


def test_capital_flow_主力净额万元转元_四档留空(monkeypatch):
    def _rpc(method, params, timeout=None, **kw):
        assert method == "get_more_info"
        assert params["stock_code"] == "002361.SZ"
        return {"Name": "神剑股份", "Zjl_HB": "3992.67", "HqDate": "20260925"}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    rows = TqCapitalFlowVendor().fetch([_cn()], {})
    assert len(rows) == 1
    r = rows[0]
    assert r.main_net_inflow == pytest.approx(3992.67 * 1e4)   # 万元 → 元
    assert r.date == "2026-09-25"
    # TQ 无四档拆分 → 超大/大/中/小单留空(绝不补 0)
    assert r.super_net_inflow is None
    assert r.big_net_inflow is None
    assert r.mid_net_inflow is None
    assert r.small_net_inflow is None
    assert r.main_net_inflow_pct is None and r.main_net_5d is None
    assert r.source == "tq"


def test_capital_flow_口径标签符合契约(monkeypatch):
    monkeypatch.setattr(
        tqmod, "_rpc", lambda *a, **k: {"Zjl_HB": "100.00", "HqDate": "20260925"}
    )
    r = TqCapitalFlowVendor().fetch([_cn()], {})[0]
    assert r.caliber == "ths"                          # L2/主力口径, 不是 eastmoney4
    assert r.direction_semantics == C.DIRECTION_THS    # 逐字复用契约常量
    # 非 tick 口径不得用于主力意图判定 → require_directional 必须抛
    with pytest.raises(C.CaliberViolationError):
        C.require_directional(
            C.CaliberTag(r.caliber, r.direction_semantics, r.source), "主力意图判定"
        )


def test_capital_flow_缺主力净额不补0返回空(monkeypatch):
    monkeypatch.setattr(tqmod, "_rpc", lambda *a, **k: {"Name": "X", "HqDate": "20260925"})
    assert TqCapitalFlowVendor().fetch([_cn()], {}) == []


def test_capital_flow_网关异常回退到空(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("TQ 隧道断开")

    monkeypatch.setattr(tqmod, "_rpc", _boom)
    assert TqCapitalFlowVendor().fetch([_cn()], {}) == []


def test_capital_flow_非dict响应返回空(monkeypatch):
    monkeypatch.setattr(tqmod, "_rpc", lambda *a, **k: ["Zjl_HB", "1"])
    assert TqCapitalFlowVendor().fetch([_cn()], {}) == []


# --------------------------------------------------------------------------- #
# 接线: registry 注册 + seed 行(备源, 不抢主源)
# --------------------------------------------------------------------------- #


class TestWiring:
    def test_registry_注册tq三个能力(self):
        from marketdata.registry import PACKAGE_VENDORS_BY_TYPE

        assert "tq" in PACKAGE_VENDORS_BY_TYPE["capital_flow"]
        assert "tq" in PACKAGE_VENDORS_BY_TYPE["board_capital_flow"]
        assert "tq" in PACKAGE_VENDORS_BY_TYPE["margin"]

    def test_seed_tq行存在且enabled(self):
        for t in ("capital_flow", "board_capital_flow", "margin"):
            seeds = _seeds(t)
            assert "tq" in seeds, f"{t} 缺 TQ 备源 seed 行"
            assert seeds["tq"]["enabled"] is True

    def test_主源不是TQ(self):
        assert min(_seeds("capital_flow"), key=lambda p: _seeds("capital_flow")[p]["priority"]) == "eastmoney"
        assert min(_seeds("board_capital_flow"), key=lambda p: _seeds("board_capital_flow")[p]["priority"]) == "ths_flow"
        assert min(_seeds("margin"), key=lambda p: _seeds("margin")[p]["priority"]) == "eastmoney"

    def test_优先级TQ在主源之后(self):
        assert _seeds("capital_flow")["eastmoney"]["priority"] < _seeds("capital_flow")["tq"]["priority"]
        assert _seeds("board_capital_flow")["ths_flow"]["priority"] < _seeds("board_capital_flow")["tq"]["priority"]
        assert _seeds("margin")["eastmoney"]["priority"] < _seeds("margin")["tq"]["priority"]
