"""数据集能力矩阵测试(2026-09-12, 借鉴 TSP 能力路由视图)。

两条必须钉住: ①"未测量"不许并入"正常"(对齐 0.3 降级不伪装);
② /capabilities 必须注册在 /{source_id} 之前, 否则会被当成 id 解析成 422。
"""
from __future__ import annotations


import src.core.data_capabilities as dc


def _row(t, p, enabled=True, prio=1):
    return {"type": t, "provider": p, "name": p, "enabled": enabled, "priority": prio}


def _h(sr, n=50):
    return {"success_rate": sr, "count": n, "ewma_latency_ms": 120, "last_error": ""}


def _src(p, enabled=True, prio=1, sr=None, n=0):
    """classify 的入参是 build_capabilities 内部产出的"源视图", 不是 DB 行。"""
    return {"provider": p, "name": p, "enabled": enabled, "priority": prio,
            "success_rate": sr, "samples": n, "ewma_latency_ms": None, "last_error": ""}


def test_classify_four_statuses_never_merges_unknown_into_ok():
    assert dc.classify([_src("tq", enabled=False)]) == ("unavailable", "没有启用的数据源")
    # 有启用源但没样本 → unknown(不能算正常)
    st, reason = dc.classify([_src("tq")])
    assert st == "unknown" and "样本" in reason
    # 样本足且成功率达标 → ok
    assert dc.classify([_src("tq", sr=0.95, n=40)])[0] == "ok"
    # 全部低于阈值 → degraded, reason 里带各源数字
    st, reason = dc.classify([_src("a", sr=0.4, n=30), _src("b", sr=0.6, n=20)])
    assert st == "degraded" and "a 40%" in reason
    # 样本太少不足以判降级 → unknown(冷启动不误判)
    assert dc.classify([_src("a", sr=0.1, n=3)])[0] == "unknown"


def test_build_capabilities_groups_and_summarizes():
    rows = [_row("kline", "tq", prio=4), _row("kline", "tencent", prio=1),
            _row("quote", "sina", enabled=False), _row("dragon_tiger", "eastmoney")]
    health = {"tq": _h(0.99), "tencent": _h(0.5), "eastmoney": _h(0.2)}
    out = dc.build_capabilities(rows, health, {"klines": "2026-09-11 15:00:00+08:00"}, today="2026-09-12")
    by = {i["type"]: i for i in out["items"]}
    assert by["kline"]["status"] == "ok"                       # 有一个好源就够
    assert [s["provider"] for s in by["kline"]["sources"]][:1] == ["tencent"]  # 按 priority 排
    assert by["quote"]["status"] == "unavailable"               # 唯一源被禁用
    assert by["dragon_tiger"]["status"] == "degraded"
    assert by["kline"]["latest_date"].startswith("2026-09-11")  # 只对落库类型探新鲜度
    assert by["kline"]["age_days"] == 1
    assert by["dragon_tiger"]["latest_date"] is None            # 未探测 → None, 不编造
    s = out["summary"]
    assert (s["ok"], s["degraded"], s["unavailable"], s["total"]) == (1, 1, 1, 3)
    assert s["unavailable_labels"] == ["实时行情"] and s["degraded_labels"] == ["龙虎榜"]


def test_age_days_handles_both_date_formats():
    assert dc._age_days("20260911", "2026-09-12") == 1
    assert dc._age_days("2026-09-11 15:00:00+08:00", "2026-09-12") == 1
    assert dc._age_days(None, "2026-09-12") is None
    assert dc._age_days("garbage", "2026-09-12") is None


def test_capabilities_route_registered_before_source_id_route():
    """回归护栏: /capabilities 必须排在 /{source_id} 之前, 否则会被当成 id 解析(422)。"""
    import src.web.api.datasources as ds_api

    paths = [getattr(r, "path", "") for r in ds_api.router.routes]
    assert "/capabilities" in paths
    assert paths.index("/capabilities") < paths.index("/{source_id}")
