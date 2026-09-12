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


def _src(p, enabled=True, prio=1, sr=None, n=0, basis="ewma"):
    """classify 的入参是 build_capabilities 内部产出的"源视图", 不是 DB 行。"""
    return {"provider": p, "name": p, "enabled": enabled, "priority": prio,
            "success_rate": sr, "samples": n, "basis": basis,
            "ewma_latency_ms": None, "last_error": ""}


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
    # basis 未知 → 不冒充任何依据; 明确 db → 标注"累计统计"
    assert "(" not in dc.classify([_src("a", sr=0.95, n=40, basis="none")])[1]
    assert "累计统计)" in dc.classify([_src("a", sr=0.2, n=40, basis="db")])[1]


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


def test_verdict_source_matches_the_source_classify_used():
    """KI-050: 状态由谁定, 数就显示谁 —— 不能按 priority 猜。"""
    srcs = [_src("low", sr=0.5, n=40), _src("high", sr=0.95, n=40, basis="db", prio=9)]
    v = dc.verdict_source(srcs)
    assert v["provider"] == "high" and v["basis"] == "db"
    reason = dc.classify(srcs)[1]
    assert "high" in reason and "95%" in reason
    # 判不出来 → None(UI 因此显示样本进度, 不显示百分比)
    assert dc.verdict_source([_src("a", sr=0.9, n=3)]) is None
    assert dc.verdict_source([_src("a", sr=0.9, n=40, enabled=False)]) is None


def test_build_capabilities_exposes_verdict_and_min_samples():
    rows = [_row("kline", "tq"), _row("kline", "tencent", prio=9), _row("news", "sina")]
    health = {"tq": _h(0.5), "tencent": _h(0.95), "sina": _h(0.99, 3)}
    out = dc.build_capabilities(rows, health, {}, today="2026-09-12")
    assert out["min_samples"] == dc.MIN_SAMPLES
    by = {i["type"]: i for i in out["items"]}
    # 结论由 tencent 定(样本足里最高), 即使 tq 优先级更高
    assert by["kline"]["verdict"]["provider"] == "tencent"
    assert by["kline"]["status"] == "ok"
    # 样本不足 → 无 verdict, 状态 unknown
    assert by["news"]["verdict"] is None and by["news"]["status"] == "unknown"


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


def test_db_counts_take_over_when_ewma_cold():
    """EWMA 是进程内存的, 重启后归零 —— 必须退回 DB 累计计数, 否则整张矩阵全灰。"""
    row = {"type": "kline", "provider": "tq", "name": "TQ", "enabled": True, "priority": 4,
           "success_count": 900, "error_count": 6, "last_error": ""}
    # 冷启动: EWMA 里没这个 vendor
    out = dc.build_capabilities([row], {}, {}, today="2026-09-12")
    it = out["items"][0]
    assert it["status"] == "ok" and "累计统计" in it["reason"]
    assert it["sources"][0]["samples"] == 906 and it["sources"][0]["basis"] == "db"
    # EWMA 有足够样本时优先用它(更能反映"此刻"质量), 且 reason 标出依据
    hot = {"tq": {"success_rate": 0.3, "count": 50, "ewma_latency_ms": 900, "last_error": "timeout"}}
    out2 = dc.build_capabilities([row], hot, {}, today="2026-09-12")
    it2 = out2["items"][0]
    assert it2["status"] == "degraded" and "滚动EWMA" in it2["reason"]
    assert it2["sources"][0]["samples"] == 50
    # 两边都不足 → 仍然 unknown, 不猜
    cold = dict(row, success_count=2, error_count=0)
    assert dc.build_capabilities([cold], {}, {}, today="2026-09-12")["items"][0]["status"] == "unknown"
