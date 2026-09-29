"""A-7(2026-09-29): 大盘资金端点优先读已落库快照。

审计 docs/TQ切换面审计_20260924.md 第五节第7条 —— `market_capital_flow` 端点改读已
落库的 SC 序列。此前资金字段**只依赖实时外呼**(国内网关→东财), 上游不可用时整块缺失,
只能降级成「仅涨跌家数」。而应用本就在持续落库大盘资金快照(market_flow_snapshots)。

覆盖:
  ① 库里有新鲜快照 → 走库且来源标注正确(snapshot_ts/age/source);
  ② 库里快照过旧 → 回退实时链路;
  ③ 库为空 → 回退实时链路;
  ④ 实时链路失败 → 仍按现有降级(不因本次改动而变);
  ⑤ 库里资金字段缺失 → 显式无数据/降级, **不得补 0**;
  ⑥ 非交易时段 → 即便库里有新鲜快照也走实时链路。
全部禁真实网络(网关与库均打桩)。

主回归一: 库读分支**绝不**把陈旧值冒充实时值 —— 响应必须带 source="db_snapshot" +
快照时间戳; 过旧行一律不采纳。
主回归二: 资金字段缺失(网关故障期间采样器落的「仅家数」行)不得补 0, 必须显式 degraded。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.core.marketdata_client as md_client
import src.core.quote_snapshots as qs
import src.web.api.market_data as mdm
from src.web.migrations import _m138_market_flow_snapshots_table


# ─────────────────────────── 夹具: 双方言兼容的内存库 ───────────────────────────

def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m138_market_flow_snapshots_table(conn)
    return eng


def _insert(eng, *, age_minutes: float = 0.0, funds=True, up=100, down=200, flat=5):
    """落一条快照。age_minutes>0 时用 SQLite datetime('now', '-N minutes') 造旧行。"""
    ts_expr = "CURRENT_TIMESTAMP" if age_minutes == 0 else f"datetime('now', '-{age_minutes} minutes')"
    vals = {
        "flow": -123.4 if funds else None,
        "sh": -60.0 if funds else None,
        "sz": -63.4 if funds else None,
    }
    with eng.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO market_flow_snapshots "
                f"(ts, total_main_flow, up_count, down_count, flat_count, sh_flow, sz_flow) "
                f"VALUES ({ts_expr}, :flow, :up, :down, :flat, :sh, :sz)"
            ),
            {**vals, "up": up, "down": down, "flat": flat},
        )
    return eng


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _MD:
    def __init__(self, boards=None):
        self._boards = boards or []

    def board_capital_flow(self, *, board_type="industry"):
        return self._boards


def _gateway_ok():
    return {
        "total_main_flow": -100.0,
        "sh": {"main_flow": -50.0, "point": 390000, "change_pct": -30},
        "sz": {"main_flow": -50.0},
        "cyb": {"main_flow": -10.0},
        "total_amount": 16000.0,
        "up_count": 900, "down_count": 4200, "flat_count": 100,
    }


@pytest.fixture
def _no_net(monkeypatch):
    """实时链路打桩: 网关返回正常报文, 其余辅助(备份/TQ)置空。禁真实网络。"""
    store: dict = {}
    monkeypatch.setattr(mdm.biz_cache, "set_json", lambda k, v, ttl=None: store.__setitem__(k, v))
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(_gateway_ok()))
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD())
    monkeypatch.setattr(mdm, "_try_write_snapshot_async", lambda payload: None)
    monkeypatch.setattr(mdm, "_tq_breadth", lambda: None)
    return store


def _patch_trading(monkeypatch, flag: bool = True):
    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: flag)


def _use_engine(monkeypatch, eng):
    import src.web.database as database

    monkeypatch.setattr(database, "engine", eng)


# ─────────────────────────── ① 新鲜快照 → 走库 ───────────────────────────

def test_fresh_snapshot_served_from_db(monkeypatch, _no_net):
    eng = _insert(_mk_engine(), funds=True)
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)

    def _no_realtime(*a, **k):
        raise AssertionError("库里有新鲜快照时不应实时外呼")

    monkeypatch.setattr("requests.get", _no_realtime)

    out = asyncio.run(mdm.market_capital_flow_proxy())

    assert out["source"] == "db_snapshot"
    assert out["data_source"] == "db_snapshot"
    assert out["data_origin"] == "market_flow_snapshots"
    assert out["breadth_source"] == "db_snapshot"
    # 资金/家数字段取自库里那一行
    assert out["total_main_flow"] == -123.4
    assert out["sh_flow"] == -60.0 and out["sz_flow"] == -63.4
    assert (out["up_count"], out["down_count"], out["flat_count"]) == (100, 200, 5)
    # 来源标注: 带原快照时间戳与行龄(不冒充实时)
    assert out["snapshot_ts"] and out["timestamp"] == out["snapshot_ts"]
    assert out["snapshot_age_sec"] is not None and out["snapshot_age_sec"] <= 5
    # 口径标签齐备
    assert out["caliber"] == "eastmoney4"
    assert out["caliber_source"] == "db_snapshot_inherited"
    assert "禁止用于主力意图判定" in out["direction_semantics"]
    assert "direction_semantics" in out
    # 快照表未存的字段: 显式置空, 不编造
    assert out["cyb_flow"] is None and out["total_amount"] is None
    assert out["sh"] is None and out["sz"] is None and out["cyb"] is None
    assert out["inflow_boards"] == [] and out["outflow_boards"] == []
    assert "degraded" not in out


def test_db_snapshot_matches_realtime_shape(monkeypatch, _no_net):
    """库读分支的字段名/结构必须与实时分支一致(前端不改)。"""
    eng = _insert(_mk_engine(), funds=True)
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)
    db_out = asyncio.run(mdm.market_capital_flow_proxy())

    # 实时分支
    monkeypatch.setattr(mdm, "_read_fresh_db_snapshot", lambda: None)
    rt_out = asyncio.run(mdm.market_capital_flow_proxy())

    for key in ("total_main_flow", "sh_flow", "sz_flow", "cyb_flow", "total_amount",
                "up_count", "down_count", "flat_count", "sh", "sz", "cyb",
                "inflow_boards", "outflow_boards", "source", "breadth_source",
                "caliber", "caliber_label"):
        assert key in db_out, f"库读分支缺字段 {key}"
        assert key in rt_out, f"实时分支缺字段 {key}"


# ─────────────────────────── ② 过旧 → 回退实时 ───────────────────────────

def test_stale_snapshot_falls_back_to_realtime(monkeypatch, _no_net):
    eng = _insert(_mk_engine(), age_minutes=10, funds=True)  # 10min 前 > 默认 5min 阈值
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)

    out = asyncio.run(mdm.market_capital_flow_proxy())

    assert out["source"] == "eastmoney_push2delay_cn"       # 走实时, 不采纳旧行
    assert out["total_main_flow"] == -100.0
    assert out.get("snapshot_ts") is None                    # 不冒充快照来源


# ─────────────────────────── ③ 库空 → 回退实时 ───────────────────────────

def test_empty_db_falls_back_to_realtime(monkeypatch, _no_net):
    _use_engine(monkeypatch, _mk_engine())                  # 建表但无行
    _patch_trading(monkeypatch, True)

    out = asyncio.run(mdm.market_capital_flow_proxy())

    assert out["source"] == "eastmoney_push2delay_cn"
    assert out["total_main_flow"] == -100.0


def test_db_unreachable_falls_back_to_realtime(monkeypatch, _no_net):
    """库不可达(读抛异常) → 静默回退实时链路, 不影响主流程。"""
    class _Boom:
        def connect(self):
            raise RuntimeError("db down")

    _use_engine(monkeypatch, _Boom())
    _patch_trading(monkeypatch, True)

    out = asyncio.run(mdm.market_capital_flow_proxy())
    assert out["source"] == "eastmoney_push2delay_cn"


# ─────────────────────── ⑥ 非交易时段 → 即便新鲜也回退实时 ───────────────────────

def test_outside_trading_window_ignores_fresh_snapshot(monkeypatch, _no_net):
    eng = _insert(_mk_engine(), funds=True)
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, False)                      # 非交易时段

    out = asyncio.run(mdm.market_capital_flow_proxy())

    assert out["source"] == "eastmoney_push2delay_cn"
    assert out["total_main_flow"] == -100.0


# ─────────────────── ⑤ 资金字段缺失 → 显式降级, 不得补 0 ───────────────────

def test_fresh_breadth_only_snapshot_degraded_not_zero(monkeypatch, _no_net):
    """网关故障期间采样器落的「仅家数」行: 新鲜但无资金字段 → 显式 degraded, 不补 0。"""
    eng = _insert(_mk_engine(), funds=False, up=1891, down=3564, flat=121)
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)

    def _no_realtime(*a, **k):
        raise AssertionError("库里有新鲜快照(即便仅家数)即应走库")

    monkeypatch.setattr("requests.get", _no_realtime)

    out = asyncio.run(mdm.market_capital_flow_proxy())

    assert out["source"] == "db_snapshot"
    assert out["degraded"] is True
    # 关键: 资金字段为 None(显式无数据), **绝不补 0**
    assert out["total_main_flow"] is None
    assert out["sh_flow"] is None and out["sz_flow"] is None
    assert (out["up_count"], out["down_count"], out["flat_count"]) == (1891, 3564, 121)
    assert "缺失" in out["note"] and "未编造 0" in out["note"]


# ─────────────────── ④ 实时链路失败 → 现有降级语义不变 ───────────────────

def test_realtime_gateway_error_no_backup_unchanged(monkeypatch):
    _use_engine(monkeypatch, _mk_engine())
    _patch_trading(monkeypatch, True)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp({"error": "502 网关挂了"}))
    monkeypatch.setattr(mdm, "_stale_take", lambda kind: None)
    monkeypatch.setattr(mdm, "_tq_breadth", lambda: None)

    out = asyncio.run(mdm.market_capital_flow_proxy())
    assert out == {"error": "502 网关挂了"}


def test_realtime_failure_without_backup_still_502(monkeypatch):
    _use_engine(monkeypatch, _mk_engine())
    _patch_trading(monkeypatch, True)
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD())

    def _boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr("requests.get", _boom)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mdm.market_capital_flow_proxy())
    assert ei.value.status_code == 502


def test_realtime_error_serves_stale_backup_unchanged(monkeypatch):
    """源故障 + 有备份 → 仍回退旧快照并带 stale 标注(本次改动不改此语义)。"""
    import time as _t

    _use_engine(monkeypatch, _mk_engine())
    _patch_trading(monkeypatch, True)
    store = {
        "stale:market-flow": {
            "saved_at": _t.time() - 30,
            "payload": {"total_main_flow": -88.0, "inflow_boards": [], "outflow_boards": []},
        }
    }
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD())
    monkeypatch.setattr(mdm, "_tq_breadth", lambda: None)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp({"error": "502"}))

    out = asyncio.run(mdm.market_capital_flow_proxy())
    assert out["stale"] is True and out["total_main_flow"] == -88.0


# ─────────────────────────── 阈值可配置 ───────────────────────────

def test_fresh_sec_env_override(monkeypatch):
    monkeypatch.setenv("MARKET_FLOW_SNAPSHOT_FRESH_SEC", "60")
    assert mdm._snapshot_fresh_sec() == 60
    monkeypatch.setenv("MARKET_FLOW_SNAPSHOT_FRESH_SEC", "abc")   # 非法 → 默认
    assert mdm._snapshot_fresh_sec() == mdm._SNAPSHOT_FRESH_SEC_DEFAULT
    monkeypatch.delenv("MARKET_FLOW_SNAPSHOT_FRESH_SEC", raising=False)
    assert mdm._snapshot_fresh_sec() == mdm._SNAPSHOT_FRESH_SEC_DEFAULT


def test_fresh_sec_threshold_takes_effect(monkeypatch, _no_net):
    """阈值调大后, 10min 旧的行也算新鲜 → 走库(证明阈值真的生效)。"""
    monkeypatch.setenv("MARKET_FLOW_SNAPSHOT_FRESH_SEC", "900")
    eng = _insert(_mk_engine(), age_minutes=10, funds=True)
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)

    out = asyncio.run(mdm.market_capital_flow_proxy())
    assert out["source"] == "db_snapshot"
    assert out["snapshot_age_sec"] >= 590
