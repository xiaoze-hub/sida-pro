"""国家队 ETF 看板：配置/解析/聚合/API 全链路单测（TDX 口径）。

三个必须钉死的口径（写错了会静默出错，不会报异常）：

1. **Amount 单位是万元**。库内 `amount_wan` 存原始值，出口才 ÷10000 转亿元。
   写反了图上看不出，但"今日 12 亿"会被算成"120 亿"。
2. **净申购只能到市场级**。单只 ETF 份额在通达信是当前值回填，所以 API 必须
   显式 `single_share_supported=False`，不能让前端以为能拆到单只。
3. **基线是同日同期**。今天 10:30 的累计要和过去 20 个交易日的 10:30 累计比，
   不是和过去 20 日的**全天**比。这里用构造数据把两种算法区分开：
   若误比全天，ratio 会小一个量级，state 会从 `active` 掉到 `quiet`。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from src.collectors import tq_etf_watch as ew
from src.web.api import etf_watch as api
from src.web.database import engine


@pytest.fixture()
def clean_flow():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM etf_minute_flow"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM etf_minute_flow"))


@pytest.fixture()
def isolated_flow():
    """快照 + 还原 `etf_flow_daily`（本模块自己的表，仍要还原以免影响其它用例的托底日样本）。"""
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            text("SELECT * FROM etf_flow_daily")
        ).mappings()]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM etf_flow_daily"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM etf_flow_daily"))
        if rows:
            conn.execute(
                text(
                    "INSERT INTO etf_flow_daily (trade_date, scale_yi, net_sub_yi, updated_at) "
                    "VALUES (:trade_date, :scale_yi, :net_sub_yi, :updated_at)"
                ),
                rows,
            )


def _insert(day: str, minute: str, code: str, amount_wan: float, source: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO etf_minute_flow (trade_date, minute, etf_code, amount_wan, volume, source) "
                "VALUES (:d, :m, :c, :a, 0, :s)"
            ),
            {"d": day, "m": minute, "c": code, "a": amount_wan, "s": source},
        )


# ── 配置 ─────────────────────────────────────────────────────────────────

def test_篮子是20只且分12类():
    assert len(ew.ETF_BASKET) == 20
    assert len(ew.categories()) == 12
    codes = [c for c, _n, _cat in ew.ETF_BASKET]
    assert len(set(codes)) == 20, "不能有重复标的，否则合计成交额会重复计"
    for code in codes:
        assert code.endswith((".SH", ".SZ")), code


def test_名称非空且不是代码本身():
    for code, name, _cat in ew.ETF_BASKET:
        assert name and name != code, f"{code} 名称缺失"


# ── 时间字段归一化 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expect",
    [("093000", "0930"), (93000, "0930"), ("145800", "1458"), ("930", "0930"), ("0930", "0930")],
)
def test_hhmm三种写法归一(raw, expect):
    assert ew._hhmm(raw) == expect


def test_hhmm脏值返回None():
    assert ew._hhmm(None) is None
    assert ew._hhmm("abc") is None


# ── 取数解析（打桩 _rpc） ─────────────────────────────────────────────────

def test_fetch_minute按日期过滤并解析(monkeypatch):
    payload = {
        "510300.SH": {
            "Date": ["20261105", "20261105", "20261104"],
            "Time": ["093000", "093100", "150000"],
            "Amount": ["100.5", "200", "999"],
            "Volume": ["1000", "2000", "3000"],
        }
    }
    monkeypatch.setattr(ew, "_rpc_full", lambda *a, **k: {"Value": payload, "has_more": False})
    got = ew.fetch_minute(["510300.SH"], "20261105")
    assert list(got) == ["510300.SH"]
    assert got["510300.SH"] == [("0930", 100.5, 1000), ("0931", 200.0, 2000)]


def test_fetch_minute按标的分页必须翻到最后一页(monkeypatch):
    """带日期窗口的批量取数是**按标的分页**的：一次只回 1 只 + has_more。

    上线首版没翻页，实测只入库了第 1 只 ETF（510300），其余 19 只静默丢失 ——
    页面看起来正常（有图有线），但"20 只合计"其实只是 1 只，属于最危险的那种静默错误。
    `stock_page_size` 传 20/50/100 都无效（实测仍只回 1 只）。
    """
    pages = {
        0: {"Value": {"510300.SH": {"Date": ["20261105"], "Time": ["093000"], "Amount": ["1"], "Volume": ["1"]}},
            "has_more": True},
        1: {"Value": {"510050.SH": {"Date": ["20261105"], "Time": ["093000"], "Amount": ["2"], "Volume": ["1"]}},
            "has_more": True},
        2: {"Value": {"159919.SZ": {"Date": ["20261105"], "Time": ["093000"], "Amount": ["3"], "Volume": ["1"]}},
            "has_more": False},
    }
    calls: list[int] = []

    def fake_full(method, params, timeout=20.0):
        assert method == "get_market_data"
        idx = params.get("stock_page_index", 0)
        calls.append(idx)
        return pages.get(idx, {"Value": {}, "has_more": False})

    monkeypatch.setattr(ew, "_rpc_full", fake_full)
    got = ew.fetch_minute(["510300.SH", "510050.SH", "159919.SZ"], "20261105")
    assert set(got) == {"510300.SH", "510050.SH", "159919.SZ"}, got
    assert len(calls) == 3, f"应翻满 3 页，实际 {calls}"


def test_fetch_minute翻页不前进时不死循环(monkeypatch):
    """网关若一直回同一页（has_more 恒 true），必须能自己停下来。"""
    same = {"Value": {"510300.SH": {"Date": ["20261105"], "Time": ["093000"], "Amount": ["1"], "Volume": ["1"]}},
            "has_more": True}
    n = {"c": 0}

    def fake_full(method, params, timeout=20.0):
        n["c"] += 1
        return same

    monkeypatch.setattr(ew, "_rpc_full", fake_full)
    got = ew.fetch_minute(["510300.SH"], "20261105")
    assert list(got) == ["510300.SH"]
    assert n["c"] <= 2, f"重复页应立即停，实际请求 {n['c']} 次"


def test_fetch_minute空响应返回空dict(monkeypatch):
    monkeypatch.setattr(ew, "_rpc_full", lambda *a, **k: {})
    assert ew.fetch_minute(["510300.SH"], "20261105") == {}


def test_fetch_sc8解析两列(monkeypatch):
    payload = {"SC8": [
        {"Date": "20261104", "Value": ["32238.13", "193.43"]},
        {"Date": "20261105", "Value": ["32393.07", "154.94"]},
    ]}
    monkeypatch.setattr(ew, "_rpc", lambda *a, **k: payload)
    assert ew.fetch_sc8("20261101", "20261105") == [
        ("20261104", 32238.13, 193.43),
        ("20261105", 32393.07, 154.94),
    ]


def test_since只写该分钟之后的行(clean_flow):
    """盘中增量：只回看最近几分钟，不重写全天（全天 4800 行实测 1.5~1.8s/次）。"""
    data = {"510300.SH": [("0930", 1.0, 10), ("1430", 2.0, 20), ("1431", 3.0, 30)]}
    assert ew.write_minute("20261105", data, since="1430") == 2
    with engine.connect() as conn:
        got = [r[0] for r in conn.execute(text(
            "SELECT minute FROM etf_minute_flow WHERE trade_date='20261105' ORDER BY minute"
        ))]
    assert got == ["1430", "1431"], got


def test_minus_minutes跨小时():
    assert ew._minus_minutes("1432", 5) == "1427"
    assert ew._minus_minutes("1002", 5) == "0957"
    assert ew._minus_minutes("0003", 5) == "0000"


def test_盘中对账不因重复数据而判失败(clean_flow, monkeypatch):
    """盘中对账全天数据，已有行不变化也不是失败。"""
    from datetime import datetime as _dt

    monkeypatch.setattr(ew, "_now", lambda: _dt(2026, 11, 5, 14, 32))
    monkeypatch.setattr(ew, "fetch_minute", lambda *a, **k: {
        "510300.SH": [("0930", 1.0, 10), ("1431", 2.0, 20)]
    })
    out = ew.run_intraday()
    assert out["ok"] is True, out
    assert out["rows"] == 2, out  # 返回对账处理条数，而非数据库实际更新条数
    assert ew.run_intraday()["ok"] is True


def test_写入幂等_upsert不重复(clean_flow):
    data = {"510300.SH": [("0930", 100.0, 1000)]}
    assert ew.write_minute("20261105", data) == 1
    assert ew.write_minute("20261105", data) == 1
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM etf_minute_flow")).scalar()
    assert n == 1, "重复采集同一分钟不能写成两行"


# ── 盘中聚合 ─────────────────────────────────────────────────────────────

def test_intraday拿同日同期做基线_不是全天对比(clean_flow):
    """构造：今天 10:30 前累计 10 亿；过去 2 日同分钟累计各 8 亿 → ratio≈1.25 → active。

    若误用「全天累计」当基线（80 亿），ratio 会是 0.125 → quiet，断言会失败。
    """
    today = "20261105"
    for code in [c for c, _n, _cat in ew.ETF_BASKET]:
        _insert(today, "0930", code, 50_000.0)   # 5 亿/只? 不, 20 只 × 5 万 = 100 万 → 见下
    # 直接按「万元」写：每分钟每只 5,000 万元 = 0.5 亿，20 只 = 10 亿
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM etf_minute_flow"))
    for code in [c for c, _n, _cat in ew.ETF_BASKET]:
        _insert(today, "0930", code, 2_500.0)     # 20 只 × 2500 万 = 5 亿
        _insert(today, "0931", code, 2_500.0)     # 累计 10 亿
        for d in ("20261103", "20261104"):
            _insert(d, "0930", code, 2_000.0)     # 4 亿
            _insert(d, "0931", code, 2_000.0)     # 累计 8 亿

    body = api.get_intraday(date_str=today)
    assert body["state"] in ("active", "surge"), body.get("state")
    assert body["totals"]["cum_yi"] == pytest.approx(10.0, abs=0.05)
    assert body["totals"]["baseline_avg_yi"] == pytest.approx(8.0, abs=0.05)
    assert body["totals"]["ratio_vs_baseline"] == pytest.approx(1.25, abs=0.02)
    assert body["totals"]["baseline_days"] == 2


def test_intraday空缺ETF时判数据不全(clean_flow):
    """只采到 3 只 ETF（覆盖率 15%）→ 不允许出活跃度结论。"""
    for code in [c for c, _n, _cat in ew.ETF_BASKET][:3]:
        _insert("20261105", "0930", code, 100.0)
    body = api.get_intraday(date_str="20261105")
    assert body["state"] == "insufficient"


def test_今日无数据时回退最近交易日并显式标注(clean_flow, monkeypatch):
    """当日分钟线未就绪 → 回退到最近交易日，但必须带 fallback_from，不能伪装成今天。"""
    from datetime import datetime as _dt

    for code in [c for c, _n, _cat in ew.ETF_BASKET]:
        _insert("20261104", "0930", code, 1_000.0)
        _insert("20261104", "0931", code, 1_000.0)

    monkeypatch.setattr(api, "_now_cn", lambda: _dt(2026, 11, 6, 10, 0))
    body = api.get_intraday(None)
    assert body["date"] == "20261104"
    assert body["fallback_from"] == "20261106"
    assert body["totals"]["cum_yi"] == pytest.approx(4.0, abs=0.05)


def test_intraday无数据显式null不编造(clean_flow):
    body = api.get_intraday(date_str="20261105")
    assert body["state"] == "no_data"
    assert body["series"] == [] and body["totals"] is None


def test_intraday非法日期返回错误():
    body = api.get_intraday(date_str="2026-11-05")
    assert "error" in body


# ── 市场级 SC8 份额（单位推断） ──────────────────────────────────────────

def test_support_reports_share_inference_without_unfounded_threshold(clean_flow, isolated_flow, monkeypatch):
    with engine.begin() as conn:
        rows = [
            ("20261103", 32000.0, 50.0),
            ("20261104", 32238.13, 193.43),
            ("20261105", 32393.07, 154.94),
        ]
        for d, s, n in rows:
            conn.execute(
                text(
                    "INSERT INTO etf_flow_daily (trade_date, scale_yi, net_sub_yi) "
                    "VALUES (:d, :s, :n)"
                ),
                {"d": d, "s": s, "n": n},
            )
    monkeypatch.setattr(ew, "single_share_source", None)
    # 固定时钟：本用例的数据止于 20261105，用 11-06 盘前看才是「T+1 新鲜」。
    monkeypatch.setattr(
        api, "_now_cn",
        lambda: datetime(2026, 11, 6, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    body = api.get_support()
    assert body["state"] == "ok"
    assert body["stale"] is False
    assert body["as_of"] == "20261105"
    assert body["freshness"]["lag_trading_days"] == 0
    # SC8 是**推断的份额口径**：出口带单位与置信度，且不许写成金额。
    assert body["unit"] == "亿份" and body["net_sub_unit"] == "亿份"
    assert body["unit_confidence"] == "inferred"
    assert "推断" in body["note"] and "不是金额" in body["note"]
    assert "亿元" not in body["note"]
    # 旧 100 亿元阈值被当成「100 亿份」托底阈值的整套推断必须消失（含误导性兼容字段）。
    for gone in (
        "threshold_share_yi",
        "threshold_yi",
        "support_days",
        "support_count",
        "latest_percentile_in_support_days",
    ):
        assert gone not in body, gone
    assert body["sample_count"] == 3
    # 分位只在本读源样本内算：154.94 低于 193.43 → 1/3。
    assert body["latest_percentile_in_samples"] == pytest.approx(33.3, abs=0.1)
    assert body["single_share_supported"] is False
    assert "单只" in body["single_share_note"]


def test_support停更时标stale且保留真实日期(clean_flow, isolated_flow, monkeypatch):
    """接口层也要挡住「停更日冒充今天」：日期照实返回，滞后按交易日算。"""
    with engine.begin() as conn:
        for d, s_, n in (("20261015", 32772.84, 227.10), ("20261016", 32886.32, 113.48)):
            conn.execute(
                text(
                    "INSERT INTO etf_flow_daily (trade_date, scale_yi, net_sub_yi) "
                    "VALUES (:d, :s, :n)"
                ),
                {"d": d, "s": s_, "n": n},
            )
    monkeypatch.setattr(
        api, "_now_cn",
        lambda: datetime(2026, 11, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    body = api.get_support()
    assert body["state"] == "stale"
    assert body["stale"] is True
    assert body["as_of"] == "20261016"
    assert body["latest"]["date"] == "20261016"
    assert body["freshness"]["lag_trading_days"] == 16
    assert "16 个交易日" in body["note"]
    assert "不是当前读数" in body["note"]


def test_support无数据时不伪造(clean_flow, isolated_flow):
    body = api.get_support()
    assert body["state"] == "no_data"
    assert "series" not in body


# ── 日度全景 ─────────────────────────────────────────────────────────────

def test_daily按分类聚合并带市场级净申购(clean_flow, isolated_flow):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO etf_flow_daily (trade_date, scale_yi, net_sub_yi) "
                "VALUES ('20261105', 32393.07, 154.94)"
            )
        )
    for code in [c for c, _n, _cat in ew.ETF_BASKET]:
        _insert("20261105", "0930", code, 1_000.0)
    body = api.get_daily(days=5)
    assert body["state"] == "ok"
    assert body["dates"] == ["20261105"]
    # 20 只 × 1000 万 = 2 亿元
    assert body["total_amount_yi"][0] == pytest.approx(2.0, abs=0.01)
    assert body["net_sub_yi"][0] == pytest.approx(154.94, abs=0.01)
    cats = {x["category"] for x in body["by_category"]}
    assert "沪深300" in cats and "行业主题" in cats


# ── 真实 HTTP：分钟来源必须从 etf_minute_flow.source 读 ──────────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api.router, prefix="/api/etf-watch")
    return TestClient(app)


def test_http_intraday_reports_real_em_source(clean_flow, monkeypatch):
    """兜底日（em_trends2）必须显示东财，而不是写死的通达信 TQ。"""
    monkeypatch.setattr(api, "_index_points", lambda target: [])
    for code in [c for c, _n, _cat in ew.ETF_BASKET]:
        _insert("20261105", "0930", code, 1_000.0, source="em_trends2")
        _insert("20261105", "0931", code, 1_000.0, source="em_trends2")
    resp = _client().get("/api/etf-watch/intraday", params={"date": "20261105"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "em_trends2"
    assert body["sources"] == ["em_trends2"]
    assert body["source_labels"] == ["东财 trends2"]
    assert "东财 trends2" in body["source_note"]
    assert "通达信" not in body["source_note"]


def test_http_intraday_flags_mixed_sources(clean_flow, monkeypatch):
    """同一天出现两种来源 → mixed + 逐日明细，不能挑一个当成单一来源。"""
    monkeypatch.setattr(api, "_index_points", lambda target: [])
    for code in [c for c, _n, _cat in ew.ETF_BASKET]:
        _insert("20261105", "0930", code, 1_000.0, source="tdx_tq")
        _insert("20261105", "0931", code, 1_000.0, source="em_trends2")
    body = _client().get("/api/etf-watch/intraday", params={"date": "20261105"}).json()
    assert body["source"] == "mixed"
    assert set(body["source_labels"]) == {"通达信 TQ", "东财 trends2"}
    assert body["source_by_date"]["20261105"] == ["东财 trends2", "通达信 TQ"]
    assert "来源混合" in body["source_note"]


def test_http_intraday_null_source_is_unknown_not_tq(clean_flow, monkeypatch):
    """历史行 source 为 NULL（未标注）→ unknown，不许默认认领成通达信 TQ。"""
    monkeypatch.setattr(api, "_index_points", lambda target: [])
    for code in [c for c, _n, _cat in ew.ETF_BASKET]:
        _insert("20261105", "0930", code, 1_000.0)   # source 不写 → NULL
    body = _client().get("/api/etf-watch/intraday", params={"date": "20261105"}).json()
    assert body["source"] == "unknown"
    assert body["source"] != "tdx_tq"
    assert body["source_labels"] == ["来源未知"]
    assert "未知" in body["source_note"]
    assert "通达信" not in body["source_note"].replace("不默认当成通达信 TQ", "")


def test_http_daily_reports_source_per_date(clean_flow):
    """日度窗口跨来源 → mixed，且逐日来源照实列出。"""
    for code in [c for c, _n, _cat in ew.ETF_BASKET]:
        _insert("20261103", "0930", code, 1_000.0, source="tdx_tq")
        _insert("20261104", "0930", code, 1_000.0, source="em_trends2")
    body = _client().get("/api/etf-watch/daily", params={"days": 5}).json()
    assert body["state"] == "ok"
    assert body["source"] == "mixed"
    assert body["source_by_date"] == {
        "20261103": ["通达信 TQ"],
        "20261104": ["东财 trends2"],
    }


# ── basket 接口声明 ──────────────────────────────────────────────────────

def test_basket声明单只份额不可用(monkeypatch):
    monkeypatch.setattr(ew, "single_share_source", None)
    body = api.get_basket()
    assert len(body["items"]) == 20
    assert body["single_share_supported"] is False


# ── 调度守卫 ─────────────────────────────────────────────────────────────

def test_非交易日不采集(monkeypatch):
    from datetime import datetime as _dt

    monkeypatch.setattr(ew, "_now", lambda: _dt(2026, 11, 7, 10, 0))  # 周六
    out = ew.run_intraday()
    assert out.get("skipped") == "non-trading-day"


def test_交易时段外不做无谓采集(monkeypatch):
    from datetime import datetime as _dt

    monkeypatch.setattr(ew, "_now", lambda: _dt(2026, 11, 5, 22, 0))  # 交易日深夜
    out = ew.run_intraday()
    assert out.get("skipped") == "out-of-session"
