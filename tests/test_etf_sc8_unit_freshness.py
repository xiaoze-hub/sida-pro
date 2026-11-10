# -*- coding: utf-8 -*-
"""SC8 市场级 ETF 序列：单位口径 + 新鲜度闸门（2026-11-10 安全修复）。

## 为什么要钉这两条

1. **单位**：`get_scjy_value(SC8)` 两列经独立重建对拍后确认是 **份额（亿份）**，不是金额（亿元）：
   - 2026-10-16 上交所 924 只（TOT_VOL 万份）+ 深交所 761 只（份）合计 **32863.16 亿份**，
     SC8 列0 = 32886.32 → 差 **-0.07%**（-23.16 亿份，缺口正是沪市货币 ETF 不在交易所份额接口）；
   - 同口径「Σ份额 × 当日净值」= **46548 亿元**（东财 clist f20 逐只对拍，overlap 1594 只比值 0.9997）；
   - 2024-09-13→10-08 沪深300 涨 25%+，SC8 列0 只涨 4.6%（份额口径才会这样，市值口径不可能）。
   - 列1 ≈ Δ列0（914 天中 557 天精确相等）⇒ 两列同源，不能互证。
   所以出口**不许**把这两列写成「亿元 / 净申购金额 / 规模×净值」。

2. **新鲜度**：TQ SC8 全历史止于 **2026-10-16**（915 行，上游「专业数据下载」停更）。
   `support_payload` 原来直接取 `series[-1]` 当最新，页面上 10-16 会长期冒充今天。
   现在按交易日历算滞后天数，超阈值 `state="stale"` + `as_of` 原样返回真实日期，
   前端与参谋都必须带日期与滞后提示，**不许**改日期、改库、改历史行。

本文件只读：全部用回放 runner，不连库。
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.chat import tools_catalog as tc  # noqa: E402
from src.core import etf_watch_feed as feed  # noqa: E402

_CST = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 11, 10, 9, 0, tzinfo=_CST)   # 报告核查日：周二，交易日


def _runner(rows: list[dict]):
    def run(key, params=None, fmt=None):
        return list(rows) if key == "etf_support" else []

    return run


def _support(rows, now=_NOW):
    return feed.support_payload(_runner(rows), 750, now=now)


def _rows(*dates: str) -> list[dict]:
    """给定交易日，构造 scale/net 单调上升的 SC8 行（数值不重要，形状重要）。"""
    return [
        {"trade_date": d, "etf_scale": 32772.84 + i, "etf_net_sub": 113.48 - i}
        for i, d in enumerate(dates)
    ]


# ── 1. 单位：亿份，不是亿元 ────────────────────────────────────────────────

def test_support_declares_share_unit_as_inference_and_never_yuan():
    sup = _support(_rows("20261015", "20261016"))
    # 显式单位元数据，供前端/参谋渲染，不靠字段名猜
    assert sup["unit"] == "亿份"
    assert sup["scale_unit"] == "亿份"
    assert sup["net_sub_unit"] == "亿份"
    # 证据只能支持「更像份额」，没有官方字段定义 ⇒ 必须标为推断，不能写成已确认
    assert sup["unit_confidence"] == "inferred"
    assert "推断" in sup["note"]
    assert "不是金额" in sup["note"]
    assert "× 净值" not in sup["note"]
    assert "反证" in sup["unit_evidence"] and "1.42" in sup["unit_evidence"]
    # 旧 100 亿元阈值被当成 100 亿份托底阈值的整套推断 + 兼容字段必须消失
    for gone in (
        "threshold_share_yi",
        "threshold_yi",
        "support_days",
        "support_count",
        "latest_percentile_in_support_days",
    ):
        assert gone not in sup, gone
    assert "100" not in sup["note"], "读侧不许再出现任何按 100 筛选的托底阈值"
    # 历史数据原样：日期与数值不许被改
    assert sup["series"][-1] == {
        "date": "20261016", "scale_yi": 32773.84, "net_sub_yi": 112.48,
    }


def test_daily_payload_states_per_series_units():
    rows = [{"trade_date": "20261016", "etf_scale": 32886.32, "etf_net_sub": 113.48}]

    def run(key, params=None, fmt=None):
        if key == "etf_daily_dates":
            return [{"trade_date": "20261016"}]
        if key == "etf_daily_net":
            return list(rows)
        return []

    day = feed.daily_payload(run, 5)
    assert day["units"] == {"total_amount": "亿元", "net_sub": "亿份", "scale": "亿份"}
    assert "市场级 SC8" in day["note"]
    assert "是份额不是金额" in day["note"]
    assert "亿份" in day["note"]


# ── 2. 新鲜度：滞后按交易日算，超阈值不能冒充当前 ──────────────────────────

def test_stale_sc8_reports_real_date_and_lag():
    """报告实测场景：库内最新 20261016，核查日 2026-11-10 → 滞后 16 个交易日。"""
    sup = _support(_rows("20261015", "20261016"))
    assert sup["state"] == "stale"
    assert sup["stale"] is True
    assert sup["as_of"] == "20261016"                  # 真实日期，不改写
    assert sup["latest"]["date"] == "20261016"
    assert sup["freshness"]["lag_trading_days"] == 16   # (10-16, 11-10) 开区间交易日数
    assert sup["freshness"]["max_lag_trading_days"] == feed.STALE_MAX_LAG_TRADING_DAYS
    assert sup["freshness"]["verified"] is True
    assert "20261016" in sup["note"]
    assert "16 个交易日" in sup["note"]
    assert "亿元" not in sup["note"]
    # 样本本身不许被裁剪：滞后不等于删数据
    assert sup["sample_count"] == 2
    assert sup["series"][0]["date"] == "20261015"


def test_fresh_sc8_is_ok_when_latest_is_previous_trading_day():
    """T+1 序列：交易日当天的最新日就是上一交易日，滞后 0。"""
    sup = _support(_rows("20261106", "20261109"))
    assert sup["freshness"]["lag_trading_days"] == 0
    assert sup["stale"] is False
    assert sup["state"] == "ok"
    assert "16 个交易日" not in sup["note"]


def test_one_missed_publish_day_is_tolerated_but_labelled():
    """滞后 1 个交易日（11-06 → 11-10 中间只有 11-09）仍在容差内，但口径说明照旧。"""
    sup = _support(_rows("20261105", "20261106"))
    assert sup["freshness"]["lag_trading_days"] == 1
    assert sup["stale"] is False
    assert sup["state"] == "ok"


def test_unknown_calendar_is_not_treated_as_fresh():
    """交易日历没覆盖的年份必须给「无法确认」，不能默认当前。"""
    sup = _support(_rows("20240102"))   # 静态表覆盖 2025-2028，2024 会抛 TradingCalendarError
    assert sup["freshness"]["verified"] is False
    assert sup["freshness"]["lag_trading_days"] is None
    assert sup["stale"] is True
    assert sup["state"] == "stale"


def test_future_dated_row_is_not_fresh():
    """未来日期（时钟错乱/脏行）不许算成滞后 0 的「最新」。"""
    sup = _support(_rows("20261111"))
    assert sup["freshness"]["reason"] == "future_date"
    assert sup["stale"] is True
    assert "晚于今日" in sup["note"]


def test_no_data_still_no_data():
    sup = _support([])
    assert sup["state"] == "no_data"
    assert "尚无落库数据" in sup["note"]
    assert "unit" not in sup or sup.get("unit") is None


# ── 3. 参谋出口：日期 + 亿份 + 滞后提示，不许说「净申购 X 亿元」 ──────────

def test_render_etf_flow_marks_stale_sc8_and_uses_shares():
    sup = _support(_rows("20261015", "20261016"))
    out = tc.render_etf_flow(
        as_of="20261016", source="x", intraday={"state": "no_data"}, support=sup,
    )
    assert "20261016" in out
    assert "亿份" in out
    assert "16 个交易日" in out
    assert "不是当前" in out or "不可视为当前" in out
    # 旧文案：金额口径 + 规模
    assert "净申购 112.48 亿元" not in out
    assert "规模 32773.84 亿元" not in out
    # 参谋输出里 SC8 段不许出现「× 净值」这种金额换算
    sc8_line = next(ln for ln in out.splitlines() if "SC8" in ln and "亿份" in ln)
    assert "元" not in sc8_line.replace("亿份", "")


def test_render_etf_flow_fresh_sc8_has_no_stale_warning():
    sup = _support(_rows("20261106", "20261109"))
    out = tc.render_etf_flow(
        as_of="20261109", source="x", intraday={"state": "no_data"}, support=sup,
    )
    assert "亿份" in out
    assert "滞后" not in out and "停更" not in out


#: 已废弃的金额口径文案（逐条来自修复前的实现）：出现任何一条都说明口径回退。
_LEGACY_YUAN_COPY = (
    "净申购（亿元）",
    "ETF 规模（亿元）",
    "份额变动 × 净值",
    "净申购 = 当日 ETF 份额变动",
    "规模/净申购（SC8）",
    "SC8 市场级口径，亿元",
)


def test_no_surface_repeats_legacy_yuan_copy():
    for rel in (
        "src/core/etf_watch_feed.py",
        "src/web/api/etf_watch.py",
        "src/agents/chat/tools_catalog.py",
        "frontend/src/pages/EtfWatch.tsx",
        "frontend/src/components/market-watch/MarketWatchHub.tsx",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        hits = [p for p in _LEGACY_YUAN_COPY if p in src]
        assert not hits, f"{rel} 仍在用金额口径文案: {hits}"
