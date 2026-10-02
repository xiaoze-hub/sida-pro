"""TQ get_financial_data_by_date 接入 (2026-10-02)。

覆盖审计 docs/TQ切换面审计_20260924.md 覆盖清单第 ③ 项: `get_financial_data_by_date`
(与已有 `get_financial_data` 配套, 按**年度**取专业财务数据, 精确回补历史财务)。

网关契约(来自 tq-capability-audit references/api-contracts 实测标定):
  **`table_list[]` + `code` + `year`** —— ⚠️ key 是 `year`, 不是 start_time/end_time。

钉住的硬约束(全离线 mock):
  · 参数缺一不用(表/代码/年度任一为空 → {} 且**不发起 RPC**, 不猜当前年);
  · 元数据键(ErrorId/Error/run_id)剔除, 空表过滤 —— 不做任何"补 0";
  · 网关故障**原样抛**(与同模块 financial_data 一致): "{}" 是"无数据", 故障是故障,
    两者不得混淆(AGENTS 要求缺数据/故障显式区分)。
"""
from __future__ import annotations

import pytest

import marketdata.vendors.tq as tqmod
from marketdata.vendors.tq import financial_data_by_date


def test_按年度取财务_契约参数(monkeypatch):
    seen = {}

    def _rpc(m, p, timeout=None, **kw):
        seen["method"] = m
        seen["params"] = dict(p)
        return {"Fn193": [{"Date": "20251231", "Value": ["1.23"]}], "ErrorId": "0", "run_id": "0"}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    out = financial_data_by_date(["Fn193"], "002361.SZ", 2025)

    assert seen["method"] == "get_financial_data_by_date"
    # 网关契约: table_list[] + code + year(⚠️ 不是 start_time/end_time)
    assert seen["params"]["table_list"] == ["Fn193"]
    assert seen["params"]["code"] == "002361.SZ"
    assert seen["params"]["year"] == "2025"          # 归一成字符串
    # 元数据键被剔除, 空表被过滤
    assert set(out) == {"Fn193"}


def test_按年度取财务_缺参不调用(monkeypatch):
    called = {"n": 0}

    def _rpc(m, p, timeout=None, **kw):
        called["n"] += 1
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    assert financial_data_by_date([], "002361.SZ", 2025) == {}          # 无表
    assert financial_data_by_date(["Fn193"], "", 2025) == {}            # 无代码
    assert financial_data_by_date(["Fn193"], "002361.SZ", None) == {}   # year 必填, 不猜当前年
    assert financial_data_by_date(["Fn193"], "002361.SZ", "") == {}
    assert called["n"] == 0


def test_按年度取财务_故障向上抛不与无数据混淆(monkeypatch):
    """与同模块 financial_data 一致: 网关异常**原样抛**(由 Engine/调用方决定降级)。

    不在此吞成 {}: "{}" 是"该表无数据", 与"网关故障"语义不同 —— 混淆会让上游把故障
    当成"无数据"静默跳过(AGENTS 要求缺数据/故障显式区分)。
    """

    def _boom(*a, **k):
        raise RuntimeError("TQ 隧道断开")

    monkeypatch.setattr(tqmod, "_rpc", _boom)
    with pytest.raises(RuntimeError):
        financial_data_by_date(["Fn193"], "002361.SZ", 2025)


def test_按年度取财务_非dict响应回空(monkeypatch):
    monkeypatch.setattr(tqmod, "_rpc", lambda *a, **k: [1, 2, 3])
    assert financial_data_by_date(["Fn193"], "002361.SZ", 2025) == {}


def test_按年度取财务_空表过滤(monkeypatch):
    """Value 为 null 的表(该年无该类数据)不进结果 —— 不当 0, 也不占位。"""

    def _rpc(m, p, timeout=None, **kw):
        return {"Fn193": [{"Date": "20251231", "Value": ["1.23"]}], "Fn194": None}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    assert set(financial_data_by_date(["Fn193", "Fn194"], "002361.SZ", 2025)) == {"Fn193"}
