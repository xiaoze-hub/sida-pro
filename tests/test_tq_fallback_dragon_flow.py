"""dragon_tiger / market_capital_flow 的 TQ 备源 + get_financial_data_by_date (2026-10-02)。

覆盖审计 docs/TQ切换面审计_20260924.md 剩余缺口:
  · ① dragon_tiger registry 注册 TQ 备源: GP02/08/09/17/18 序列 → DragonTigerItem,
       **万元→元 ×1e4**(与东财 BILLBOARD_*_AMT 元口径对齐);
  · ② market_capital_flow registry 注册 TQ 备源: 全 881 行业板块 SUPAMO 主力资金求和
       → 大盘净额(亿); 资金类硬约束: **必须带 caliber + direction_semantics**, 方向性判定
       只走逐笔(get_main_intent), 非 tick 口径过 require_directional 必抛 / 冲突优先逐笔;
  · ③ get_financial_data_by_date 接入(网关契约: table_list[] + code + year)。

钉住的硬约束(全部离线 mock, 不触真实网关):
  · **单位**: GP02 万元 → 元 ×1e4; SUPAMO 万元 → 亿 ÷1e4(与同模块 TqBoardCapitalFlowVendor
    同一读数, 保证"大盘=板块净额之和"内部一致);
  · **口径**: TQ 资金类=L2/主力口径=契约 "ths"(非 eastmoney4), 带 caliber+direction_semantics,
    direction_semantics 与 src/core/caliber.py:DIRECTION_THS **逐字一致**;
  · **缺字段留 None/空(绝不补 0)**: TQ 龙虎榜无 reason/close/席位名; TQ 大盘只给净额,
    无流入/流出双值 → total_inflow/total_outflow=None;
  · **诚实性**: TQ 无市场级「某日全部上榜」枚举 → 无候选时返回空(不伪造); 网关/表无数据
    → 返回 [] 交给下一源, 不抛、不编造。
"""
from __future__ import annotations

import pytest

import marketdata.vendors.tq as tqmod
from marketdata.cache import TTLCache
from marketdata.defaults import InMemoryMetricsSink, StaticConfigProvider
from marketdata.engine import Engine
from marketdata.ports import SourceConfig
from marketdata.registry import PACKAGE_VENDORS_BY_TYPE, build_vendors
from marketdata.symbol import Symbol
from marketdata.types import Request
from marketdata.vendors.tq import (
    TqDragonTigerVendor,
    TqMarketFlowVendor,
)

from src.bootstrap.datasources import DATA_SOURCE_SEEDS
from src.core import caliber as C


def _cn(code: str = "002361") -> Symbol:
    return Symbol.parse(code, "CN")


def _seeds(type_: str) -> dict[str, dict]:
    return {s["provider"]: s for s in DATA_SOURCE_SEEDS if s.get("type") == type_}


# 实测形状取自 172.27.16.1:17709 (见 tests/test_tq_extras.py _RAW)。
_LHB_GP = {
    "GP02": [{"Date": "20251219", "Value": ["14881.66", "23043.70"]}],  # 买/卖 万元
    "GP09": [{"Date": "20251219", "Value": ["2.00", "6029.40"]}],       # 机构个数/买入额
    "GP17": [{"Date": "20251219", "Value": ["6452.20", "17082.80"]}],   # 营业部买/卖
    "GP18": [{"Date": "20251219", "Value": ["21234.23", "17733.06"]}],  # 沪深股通买/卖
}


# ════════════════════════════════════════════════════════════════════════
# ① dragon_tiger TQ 备源
# ════════════════════════════════════════════════════════════════════════

def test_龙虎榜_万元转元_同东财口径(monkeypatch):
    seen = {}

    def _rpc(m, p, timeout=None, **kw):
        seen["method"] = m
        seen["code"] = p.get("code")
        return _LHB_GP

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    rows = TqDragonTigerVendor().fetch([], {"date": "2025-12-19", "symbols": ["002361"]})

    assert seen["method"] == "get_gpjy_value"
    assert seen["code"] == "002361.SZ"          # 裸码归一到带后缀(TQ 要带后缀)
    assert len(rows) == 1
    r = rows[0]
    assert r.trade_date == "2025-12-19"          # 与东财 TRADE_DATE[:10] 同格式
    assert r.symbol == "002361"
    # 万元 → 元 ×1e4(与东财 BILLBOARD_BUY_AMT 同口径; 漏换算会错 1e4 且不报错)
    assert r.buy_amt == pytest.approx(14881.66 * 1e4)
    assert r.sell_amt == pytest.approx(23043.70 * 1e4)
    assert r.net_buy == pytest.approx((14881.66 - 23043.70) * 1e4)
    # TQ 没有的字段一律留空(不编造)
    assert r.name == ""
    assert r.reason is None and r.close is None and r.change_pct is None
    assert r.turnover_pct is None and r.free_market_cap is None
    assert r.top_buyers is None and r.top_sellers is None


def test_龙虎榜_裸码与带后缀都归一(monkeypatch):
    codes = []

    def _rpc(m, p, timeout=None, **kw):
        codes.append(p.get("code"))
        return _LHB_GP

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    TqDragonTigerVendor().fetch([], {"date": "20251219", "symbols": ["600519", "002361.SZ"]})
    assert codes == ["600519.SH", "002361.SZ"]


def test_龙虎榜_只取目标日_窗口外剔除(monkeypatch):
    def _rpc(m, p, timeout=None, **kw):
        return {"GP02": [
            {"Date": "20251218", "Value": ["1", "2"]},
            {"Date": "20251219", "Value": ["3", "4"]},
        ]}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    rows = TqDragonTigerVendor().fetch([], {"date": "20251219", "symbols": ["002361"]})
    assert len(rows) == 1 and rows[0].trade_date == "2025-12-19"


def test_龙虎榜_无候选返回空不伪造(monkeypatch):
    called = {"n": 0}

    def _rpc(m, p, timeout=None, **kw):
        called["n"] += 1
        return _LHB_GP

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    # 无 config.symbols、无显式 symbols → TQ 无市场级枚举, 不伪造整日榜单
    assert TqDragonTigerVendor().fetch([], {"date": "20251219"}) == []
    assert called["n"] == 0


def test_龙虎榜_日期缺失返回空(monkeypatch):
    monkeypatch.setattr(tqmod, "_rpc", lambda *a, **k: _LHB_GP)
    assert TqDragonTigerVendor().fetch([], {"symbols": ["002361"]}) == []


def test_龙虎榜_网关异常回退到空(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("TQ 隧道断开")

    monkeypatch.setattr(tqmod, "_rpc", _boom)
    assert TqDragonTigerVendor().fetch([], {"date": "20251219", "symbols": ["002361"]}) == []


def test_龙虎榜_无数据返回空(monkeypatch):
    monkeypatch.setattr(tqmod, "_rpc", lambda *a, **k: {})
    assert TqDragonTigerVendor().fetch([], {"date": "20251219", "symbols": ["002361"]}) == []


def test_龙虎榜_显式symbols也可作候选(monkeypatch):
    seen = {}

    def _rpc(m, p, timeout=None, **kw):
        seen["code"] = p.get("code")
        return _LHB_GP

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    rows = TqDragonTigerVendor().fetch([_cn("002361")], {"date": "20251219"})
    assert seen["code"] == "002361.SZ" and len(rows) == 1


# ════════════════════════════════════════════════════════════════════════
# ② market_capital_flow TQ 备源
# ════════════════════════════════════════════════════════════════════════

_BOARDS = [
    {"Code": "881290.SH", "Name": "通用设备"},   # 行业
    {"Code": "881155.SH", "Name": "出版业"},     # 行业
    {"Code": "880301.SH", "Name": "机器人概念"},  # 概念 → 行业求和不含
]


def _patch_boards(monkeypatch, formula):
    monkeypatch.setattr(tqmod, "sector_list", lambda list_type=1: list(_BOARDS))
    monkeypatch.setattr(tqmod, "formula_mul", formula)


def test_大盘资金_全行业求和_万元转亿(monkeypatch):
    seen = []

    def _fm(name, codes, **kw):
        assert name == "SUPAMO"
        seen.append(list(codes))
        return {
            "881290.SH": {"主力资金": "30000.00"},    # 3.0 亿
            "881155.SH": {"主力资金": "-10000.00"},   # -1.0 亿
            "880301.SH": {"主力资金": "99999.00"},     # 概念 → 不该出现在行业请求里
        }

    _patch_boards(monkeypatch, _fm)
    rows = TqMarketFlowVendor().fetch([], {})

    assert len(rows) == 1
    r = rows[0]
    assert r.net_inflow == pytest.approx(2.0)      # (30000 - 10000) 万元 / 1e4 = 2.0 亿
    assert r.board_count == 2
    assert r.source == "tq"
    # TQ 只给净额 → 流入/流出留空(不补 0)
    assert r.total_inflow is None and r.total_outflow is None
    assert all("880301.SH" not in batch for batch in seen)


def test_大盘资金_口径标签符合契约(monkeypatch):
    _patch_boards(monkeypatch, lambda *a, **k: {"881290.SH": {"主力资金": "30000"}})
    r = TqMarketFlowVendor().fetch([], {})[0]
    assert r.caliber == "ths"                          # L2/主力口径, 不是 eastmoney4
    assert r.direction_semantics == C.DIRECTION_THS    # 逐字复用契约常量
    # 非 tick 口径不得用于主力意图判定 → require_directional 必抛
    with pytest.raises(C.CaliberViolationError):
        C.require_directional(C.CaliberTag(r.caliber, r.direction_semantics, r.source), "主力意图判定")


def test_大盘资金_口径冲突优先逐笔(monkeypatch):
    """两口径方向冲突时: 逐笔为唯一可信方向, 冲突文案显式说明差异。"""
    _patch_boards(monkeypatch, lambda *a, **k: {"881290.SH": {"主力资金": "30000"}})
    r = TqMarketFlowVendor().fetch([], {})[0]
    tick = C.CaliberTag("tick", C.DIRECTION_TICK, "get_main_intent")
    ths = C.CaliberTag(r.caliber, r.direction_semantics, r.source)
    # 逐笔判流入(+), TQ/同花顺口径判流出(-) → 冲突
    verdict = C.reconcile_direction(tick, 1e7, ths, -r.net_inflow)
    assert verdict["agree"] is False
    assert "以逐笔为准" in verdict["statement"]


def test_大盘资金_无数据不补0返回空(monkeypatch):
    _patch_boards(monkeypatch, lambda *a, **k: {})   # 网关无数据
    assert TqMarketFlowVendor().fetch([], {}) == []


def test_大盘资金_公式异常回退到空(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("TQ 隧道断开")

    _patch_boards(monkeypatch, _boom)
    assert TqMarketFlowVendor().fetch([], {}) == []


def test_大盘资金_板块目录为空回退到空(monkeypatch):
    monkeypatch.setattr(tqmod, "sector_list", lambda list_type=1: [])
    monkeypatch.setattr(tqmod, "formula_mul", lambda *a, **k: {"881290.SH": {"主力资金": "1"}})
    assert TqMarketFlowVendor().fetch([], {}) == []


# ════════════════════════════════════════════════════════════════════════
# 接线: registry 注册 + seed 行(备源, 不抢主源)
# ════════════════════════════════════════════════════════════════════════

class TestWiring:
    def test_registry_注册tq两个能力(self):
        assert "tq" in PACKAGE_VENDORS_BY_TYPE["dragon_tiger"]
        assert "tq" in PACKAGE_VENDORS_BY_TYPE["market_capital_flow"]

    def test_build_vendors_含tq实例(self):
        assert "tq" in build_vendors("dragon_tiger")
        assert "tq" in build_vendors("market_capital_flow")

    def test_seed_tq行存在且enabled(self):
        for t in ("dragon_tiger", "market_capital_flow"):
            seeds = _seeds(t)
            assert "tq" in seeds, f"{t} 缺 TQ 备源 seed 行"
            assert seeds["tq"]["enabled"] is True

    def test_主源不是TQ(self):
        assert min(_seeds("dragon_tiger"), key=lambda p: _seeds("dragon_tiger")[p]["priority"]) == "eastmoney"
        assert min(_seeds("market_capital_flow"),
                   key=lambda p: _seeds("market_capital_flow")[p]["priority"]) == "ths_market_flow"

    def test_优先级TQ在主源之后(self):
        assert _seeds("dragon_tiger")["eastmoney"]["priority"] < _seeds("dragon_tiger")["tq"]["priority"]
        assert _seeds("dragon_tiger")["ftshare"]["priority"] < _seeds("dragon_tiger")["tq"]["priority"]
        assert (_seeds("market_capital_flow")["ths_market_flow"]["priority"]
                < _seeds("market_capital_flow")["tq"]["priority"])


# ── fallback 端到端(Engine 链路, 全离线) ──────────────────────────────────

def _engine(datatype: str, srcs: list[SourceConfig]) -> Engine:
    return Engine(
        datatype=datatype,
        vendors=build_vendors(datatype),
        config=StaticConfigProvider({datatype: srcs}),
        metrics=InMemoryMetricsSink(),
        cache=TTLCache(300.0),
        default_ttl=300.0,
    )


def test_fallback_龙虎榜_engine链路到tq(monkeypatch):
    monkeypatch.setattr(tqmod, "_rpc", lambda *a, **k: _LHB_GP)
    eng = _engine("dragon_tiger", [
        SourceConfig(vendor="tq", priority=8, config={"symbols": ["002361"]}),
    ])
    resp = eng.fetch(Request(symbols=(), market="CN", extra=(("date", "20251219"),)))
    assert resp.ok and resp.vendor == "tq"
    assert len(resp.data) == 1 and resp.data[0].buy_amt == pytest.approx(14881.66 * 1e4)


def test_fallback_大盘资金_engine链路到tq(monkeypatch):
    _patch_boards(monkeypatch, lambda *a, **k: {"881290.SH": {"主力资金": "30000"}})
    eng = _engine("market_capital_flow", [SourceConfig(vendor="tq", priority=2)])
    resp = eng.fetch(Request(symbols=(), market="CN"))
    assert resp.ok and resp.vendor == "tq"
    assert resp.data[0].net_inflow == pytest.approx(3.0)
    assert resp.data[0].caliber == "ths"
