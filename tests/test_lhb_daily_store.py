"""龙虎榜按日落库(2026-10-14 资金类落库收口, 迁移 m190)。

审计定性: `/market-data/fundamentals-detail` 经 `_lhb_from_tq` **每次请求现拉** TQ GP
长序列(个股整段历史), `/dragon-tiger/{date}` 亦逐次现拉东财+ftshare。本文件锁定收口后
契约:

  ① upsert 幂等: 同 (trade_date, symbol, market) 重拉只留一行, 值覆盖为最新;
  ② 读回契约: 库行 → 前端行, 键/单位/席位列表与现拉路径一致;
  ③ 读库命中零回源: 库里有该 symbol 且最新日 >= 今天 → **不碰 vendor**;
  ④ 过旧**当日增量**回源: 最新日 < 今天 → 只回源 [最新+1, 今天](start_time 收窄);
  ⑤ 冷启动整段回源并落库; 第二次调用走库零回源;
  ⑥ `/dragon-tiger/{date}` 已落库 → 读库返回(历史日不回源);
  ⑦ `_compute_lhb_range` 逐日先读库, 已落库日不回源。

全部禁真实网络(vendor / 网关 / TQ 均打桩, 库用内存 SQLite)。
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.web.api.market_data as mdm
from src.web.database import engine as _real_engine  # noqa: F401  (仅为语义清晰)
from src.web.migrations import _m192_dragon_tiger_daily_table


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m192_dragon_tiger_daily_table(conn)
    return eng


def _use_engine(monkeypatch, eng):
    import src.web.database as database

    monkeypatch.setattr(database, "engine", eng)


def _today() -> str:
    return date.today().strftime("%Y%m%d")


def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y%m%d")


def _row(d: str, sym: str = "002361", **kw) -> dict:
    base = {
        "trade_date": d, "symbol": sym, "name": "神剑股份", "reason": "日涨幅偏离值达7%",
        "close": 10.42, "change_pct": 9.98, "net_buy": -81620400.0,
        "buy_amt": 148816600.0, "sell_amt": 230437000.0, "turnover_pct": 12.3,
        "top_buyers": [{"name": "某营业部"}], "top_sellers": [],
        "inst_buy_wan": 6029.40, "inst_sell_wan": None, "yyb_buy_wan": 6452.20,
        "yyb_sell_wan": 17082.80, "hsgt_buy_wan": None, "hsgt_sell_wan": None,
        "suspicious": False,
    }
    base.update(kw)
    return base


def _count(eng, sym="002361") -> int:
    with eng.connect() as conn:
        return int(conn.execute(
            text("SELECT COUNT(*) FROM dragon_tiger_daily WHERE symbol = :s"), {"s": sym}
        ).scalar() or 0)


# ─────────────────────────── ① upsert 幂等 ───────────────────────────

def test_按日upsert幂等_同日同股只留一行(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    rows = [_row("20251219"), _row("20251218")]

    assert mdm._lhb_upsert_rows(rows, market="CN", source="tdx_tq") == 2
    assert _count(eng) == 2
    # 重拉(同键) → 仍 2 行, 值被覆盖为最新
    again = [_row("20251219", close=11.11), _row("20251218")]
    mdm._lhb_upsert_rows(again, market="CN", source="tdx_tq")
    assert _count(eng) == 2
    with eng.connect() as conn:
        close = conn.execute(
            text("SELECT close FROM dragon_tiger_daily WHERE trade_date='20251219'")
        ).scalar()
    assert close == pytest.approx(11.11)


def test_键不全的行不入库不编造(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    # 非 6 位代码 / 非 8 位日期 → 丢弃
    assert mdm._lhb_upsert_rows([{"trade_date": "2025-12-19", "symbol": "ABC"}]) == 0
    assert _count(eng, "ABC") == 0


# ─────────────────────────── ② 读回契约 ───────────────────────────

def test_读回与现拉路径同契约(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    mdm._lhb_upsert_rows([_row("20251219")], market="CN", source="tdx_tq")

    out = mdm._lhb_read_symbol("002361.SZ")  # 带后缀也能读
    assert len(out) == 1
    r = out[0]
    assert r["trade_date"] == "20251219" and r["symbol"] == "002361"
    assert r["name"] == "神剑股份" and r["reason"] == "日涨幅偏离值达7%"
    assert r["buy_amt"] == pytest.approx(148816600.0)      # 元, 与现拉同口径
    assert r["top_buyers"] == [{"name": "某营业部"}]        # JSON 列表还原
    assert r["top_sellers"] == []
    assert r["inst_buy_wan"] == pytest.approx(6029.40)     # TQ 独有拆解仍在
    assert r["hsgt_buy_wan"] is None and r["suspicious"] is False
    # 空席位不写 "[]" 占位
    assert r["top_sellers"] == []


# ─────────────────────── ③ 库命中零回源 ───────────────────────

def test_库最新日为今天_零回源(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    mdm._lhb_upsert_rows([_row(_today()), _row("20250101")], market="CN", source="tdx_tq")

    def _no_vendor(*a, **k):
        raise AssertionError("库里有今日行时不应回源 TQ")

    monkeypatch.setattr("marketdata.vendors.tq.lhb_series", _no_vendor)
    out = mdm._lhb_symbol_rows("002361", "CN")
    assert [r["trade_date"] for r in out] == sorted([_today(), "20250101"], reverse=True)


# ─────────────────── ④ 过旧 → 当日增量回源 ───────────────────

def test_最新日早于今天_仅回源增量(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    mdm._lhb_upsert_rows([_row("20250101")], market="CN", source="tdx_tq")

    calls: list[str] = []

    def _series(code, start_time=""):
        calls.append(start_time)
        return [{"date": _today(), "buy": 100.0, "sell": 50.0, "inst_buy_amount": None,
                 "inst_sell_amount": None, "yyb_buy": None, "yyb_sell": None,
                 "hsgt_buy": None, "hsgt_sell": None, "suspicious": False}]

    monkeypatch.setattr("marketdata.vendors.tq.lhb_series", _series)
    out = mdm._lhb_symbol_rows("002361", "CN")

    # 只抓增量: start_time = 20250102(最新+1), 不是整段
    assert calls == ["20250102"]
    assert [r["trade_date"] for r in out] == [_today(), "20250101"]
    # 增量行已落库
    assert _count(eng) == 2


def test_库最新日为昨天_增量起点为今天(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    mdm._lhb_upsert_rows([_row(_yesterday())], market="CN", source="tdx_tq")

    seen: list[str] = []
    monkeypatch.setattr("marketdata.vendors.tq.lhb_series",
                        lambda code, start_time="": seen.append(start_time) or [])
    mdm._lhb_symbol_rows("002361", "CN")
    assert seen == [_today()]


# ─────────────────── ⑤ 冷启动整段回源 + 落库 ───────────────────

def test_冷启动整段回源并落库_二次走库(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)

    calls = {"n": 0}

    def _series(code, start_time=""):
        calls["n"] += 1
        return [{"date": "20250101", "buy": 10.0, "sell": 5.0, "inst_buy_amount": None,
                 "inst_sell_amount": None, "yyb_buy": None, "yyb_sell": None,
                 "hsgt_buy": None, "hsgt_sell": None, "suspicious": False}]

    monkeypatch.setattr("marketdata.vendors.tq.lhb_series", _series)
    first = mdm._lhb_symbol_rows("002361", "CN")
    assert calls["n"] == 1 and len(first) == 1
    assert _count(eng) == 1                      # 拉到即落

    # 第二次: 库已落(最新日 20250101 < 今天) → 只回源增量, 不再整段
    mdm._lhb_symbol_rows("002361", "CN")
    assert calls["n"] == 2
    # 第三次若把最新日推进到今天 → 零回源
    mdm._lhb_upsert_rows([_row(_today())], market="CN", source="tdx_tq")
    monkeypatch.setattr("marketdata.vendors.tq.lhb_series",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应回源")))
    out = mdm._lhb_symbol_rows("002361", "CN")
    assert out[0]["trade_date"] == _today()


# ─────────────────────── ⑥ /dragon-tiger/{date} 读库 ───────────────────────

class _FT:
    def __init__(self, sym):
        self.symbol = sym
        self.top_buyers = [{"name": "席位A"}]
        self.top_sellers = []


class _Item:
    def __init__(self, sym, d):
        self.symbol = sym
        self.trade_date = d
        self.name = "某票"
        self.reason = "x"
        self.close = 1.0
        self.change_pct = 1.0
        self.net_buy = 1.0
        self.buy_amt = 2.0
        self.sell_amt = 1.0
        self.turnover_pct = 1.0


def test_按日端点_库已落则读库不回源(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    store: dict = {}
    monkeypatch.setattr(mdm.biz_cache, "set_json", lambda k, v, ttl=None: store.__setitem__(k, v))
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))
    mdm._lhb_upsert_rows([_row("20251219")], market="CN", source="em_ftshare")

    class _MD:
        def dragon_tiger(self, **k):
            raise AssertionError("该日已落库 → 不应回源")

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    out = asyncio.run(mdm.dragon_tiger_proxy("20251219", market="CN", wait=0))
    assert out["source"] == "db" and out["count"] == 1
    assert out["items"][0]["symbol"] == "002361"
    assert out["pending"] is False


def test_按日端点_库空则回源并落库(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    store: dict = {}
    monkeypatch.setattr(mdm.biz_cache, "set_json", lambda k, v, ttl=None: store.__setitem__(k, v))
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))

    class _MD:
        def dragon_tiger(self, **k):
            return [_Item("002361", "20251219")]

    class _FTVendor:
        def fetch(self, *a, **k):
            return [_FT("002361")]

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    monkeypatch.setattr("marketdata.vendors.ftshare.FtshareDragonTigerVendor", _FTVendor)
    out = asyncio.run(mdm.dragon_tiger_proxy("20251219", market="CN", wait=5))
    assert out["count"] == 1 and out["source"] == "em_ftshare"
    # 拉到即落
    assert _count(eng) == 1
    with eng.connect() as conn:
        src = conn.execute(text(
            "SELECT source FROM dragon_tiger_daily WHERE trade_date='20251219'")).scalar()
    assert src == "em_ftshare"


# ─────────────────── ⑦ range 逐日先读库 ───────────────────

def test_range逐日先读库_已落库日不回源(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    today = _today()
    mdm._lhb_upsert_rows([_row(today)], market="CN", source="em_ftshare")

    seen: list[str] = []

    class _MD:
        def dragon_tiger(self, *, date=None, market="CN"):
            seen.append(date)
            return []

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    out = mdm._compute_lhb_range("CN", 3)

    assert today in out["by_date"]
    assert out["by_date"][today][0]["symbol"] == "002361"
    assert today not in seen                       # 已落库日零回源
    # 其余回看日(库里没有)才回源
    assert all(d != today for d in seen)
