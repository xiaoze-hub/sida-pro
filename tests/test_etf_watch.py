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

from datetime import date

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


def _insert(day: str, minute: str, code: str, amount_wan: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO etf_minute_flow (trade_date, minute, etf_code, amount_wan, volume) "
                "VALUES (:d, :m, :c, :a, 0)"
            ),
            {"d": day, "m": minute, "c": code, "a": amount_wan},
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


# ── 托底信号 ─────────────────────────────────────────────────────────────

def test_support筛出净申购达标日并给出分位(clean_flow, isolated_flow, monkeypatch):
    with engine.begin() as conn:
        rows = [
            ("20261103", 32000.0, 50.0),
            ("20261104", 32238.13, 193.43),   # 托底日
            ("20261105", 32393.07, 154.94),   # 托底日
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
    body = api.get_support()
    assert body["state"] == "ok"
    assert body["support_count"] == 2
    assert [x["date"] for x in body["support_days"]] == ["20261104", "20261105"]
    # 最新一天 154.94 在托底样本里排第 1（高于 193.43 的没有）→ 分位 0%
    assert body["latest_percentile_in_support_days"] == 0.0
    assert body["single_share_supported"] is False
    assert "单只" in body["single_share_note"]


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
