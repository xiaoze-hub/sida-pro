# -*- coding: utf-8 -*-
"""数据日期与涨停口径阻断项(2026-11-09 复审后的红→绿钉子)。

审计线索(逐条在代码里核实过):
  ① **ST 涨停被漏**: `limit_up_calc.limit_up_ratio` 只看代码前缀 → 主板 ST 真实
     涨停是 5%, 按 10% 判 ⇒ TQ 主源把主板 ST 涨停股整批漏掉(且自算连板数塌成 0);
  ② **日期格式不归一**: `_window_bars` 拿 `YYYY-MM-DD` 去比 `YYYYMMDD` 永远不相等
     → 显式日期请求直接空池/降级; `get_limit_up_pool("2026-11-06")` 的探测回退也
     按 `%Y%m%d` 解析失败 ⇒ 悄悄用别的日期;
  ③ **连板窗口差一根**: `_TQ_BARS = 12` 只够数出 11 连板(12 连板需 13 根收盘价),
     市场最高板正好卡在边界就被少算一板;
  ④ **取数不全却当完整池**: TQ 日线分片失败只记 logger, 池子照样当"全市场涨停家数"
     返回; 无覆盖率信息时也没有任何标注 —— 缺来源被读成覆盖完整;
  ⑤ **非当日池 + 当前概念归属伪装同日**: 盘前跑的是上一交易日 K 线, 概念/行业却是
     今天反查的成员关系, 响应只有一个 `as_of`, 消费方会把"今天的归属"当成"那天的归属";
  ⑥ **无可信日期按今日持久化**: `_upsert_daily_snapshot` 缺 as_of 时回退
     `date.today()`; `market_phase.sync` 拿到别的交易日的池子照样按 target 落库。

本文件钉住修复后的契约; 全部禁真网络。
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest


# ════════════════ ① ST 涨停口径(真实名称 + 真实规则日期) ════════════════
# 规则事实(2026-11-09 核对): 沪深北交易所 2026-04 公告, **2026-07-06** 起主板风险警示股
# 涨跌幅由 ±5% 调整为 ±10%; 创业板/科创板 ST 仍 ±20%, 北交所 ST 仍 ±30%。
# 所以"ST 一律 5%"和"ST 一律 10%"都错: 必须按交易日期分档。
def test_st_main_board_5pct_only_before_2026_07_06():
    from src.core.limit_up_calc import limit_up_ratio

    assert limit_up_ratio("600001.SH", "ST易购", as_of="2026-06-30") == 0.05
    assert limit_up_ratio("000001.SZ", "*ST海航", as_of="20260630") == 0.05
    assert limit_up_ratio("002024.SZ", "ST 易购", as_of="2026-06-30") == 0.05  # 名称带空格
    assert limit_up_ratio("600001.SH", "s*st样本", as_of="2026-06-30") == 0.05  # 大小写不敏感
    assert limit_up_ratio("600001.SH", "SST样本", as_of="2026-06-30") == 0.05

    # 新规生效日及之后: 主板 ST 与普通主板同为 10%
    assert limit_up_ratio("600001.SH", "ST易购", as_of="20260706") == 0.10
    assert limit_up_ratio("600001.SH", "ST易购", as_of="2026-11-09") == 0.10
    # 日期未知 → 现行规则(10%), 不假设 ST 降档
    assert limit_up_ratio("600001.SH", "ST易购") == 0.10


def test_st_on_20pct_boards_keeps_20pct():
    """创业板/科创板 ST 始终 20%、北交所 ST 始终 30%, 不随主板新规变。"""
    from src.core.limit_up_calc import limit_up_ratio

    for day in ("2026-06-30", "2026-07-06", "2026-11-09"):
        assert limit_up_ratio("300001.SZ", "ST宝德", as_of=day) == 0.20
        assert limit_up_ratio("301059.SZ", "*ST样本", as_of=day) == 0.20
        assert limit_up_ratio("688001.SH", "ST样本", as_of=day) == 0.20
        assert limit_up_ratio("430047.BJ", "ST样本", as_of=day) == 0.30


def test_non_st_name_keeps_board_ratio():
    from src.core.limit_up_calc import limit_up_ratio

    assert limit_up_ratio("600001.SH", "正常股份", as_of="2026-06-30") == 0.10
    assert limit_up_ratio("600001.SH", "", as_of="2026-06-30") == 0.10
    # 名字里含 ST 但不是前缀(如 "海ST重工") 不能误判
    assert limit_up_ratio("600001.SH", "海ST重工", as_of="2026-06-30") == 0.10


def test_st_limit_price_rounds_to_cent():
    """ST 涨停价 = 前收 ×1.05 四舍五入到分(1.87 → 1.96), 不是 1.9635。"""
    from src.core.limit_up_calc import limit_up_price

    assert limit_up_price(1.87, 0.05) == 1.96
    assert limit_up_price(10.0, 0.10) == 11.0
    assert limit_up_price(3.87, 0.10) == 4.26
    assert limit_up_price(10.0, 0.20) == 12.0


def test_st_five_percent_run_counts_only_before_cutover():
    """主板 ST 连续 +5%: 2026-07-06 前是连板, 之后不是(需 10%)。"""
    from src.core.limit_up_calc import consecutive_boards

    seq = [10.0, 10.5, 11.03]  # +5.0% / +5.05%
    assert consecutive_boards(seq, "600001.SH", name="ST易购", as_of="2026-06-30") == 2
    assert consecutive_boards(seq, "600001.SH", name="ST易购", as_of="2026-11-09") == 0
    assert consecutive_boards([10.0, 11.0], "600001.SH", name="ST易购", as_of="2026-11-09") == 1
    # 名称缺失(不知是 ST)时 5% 不算涨停 —— 证明名称确实参与判停
    assert consecutive_boards(seq, "600001.SH", as_of="2026-06-30") == 0


def test_board_window_across_rule_cutover_uses_per_bar_rule():
    """跨 2026-07-06 的连板窗口要逐段取规则: 5%(旧) + 10%(新) 各算一板。"""
    from src.core.limit_up_calc import consecutive_boards

    closes = [10.0, 10.5, 11.55]  # +5.0% (旧规) / +10.0% (新规)
    dates = ["20260702", "20260703", "20260706"]
    assert consecutive_boards(closes, "600001.SH", name="ST易购", dates=dates) == 2
    # 同一序列若整段按新规(10%)判, 第一段 5% 就不算 —— 证明 dates 生效
    assert consecutive_boards(closes, "600001.SH", name="ST易购", as_of="20260706") == 1


def test_chinext_st_still_needs_20pct():
    from src.core.limit_up_calc import consecutive_boards

    for day in ("2026-06-30", "2026-11-09"):
        assert consecutive_boards([10.0, 10.5, 11.03], "300001.SZ", name="ST宝德", as_of=day) == 0
        assert consecutive_boards([10.0, 12.0], "300001.SZ", name="ST宝德", as_of=day) == 1


def test_build_pool_items_uses_names_for_st():
    from src.core.limit_up_calc import build_pool_items

    # 2026-06-30(新规前): ST +5% 是涨停
    old = {
        "600001.SH": {
            "Close": ["10.0", "10.5"], "Amount": ["100", "100"],
            "Date": ["20260627", "20260630"],
        }
    }
    pool = build_pool_items(old, names={"600001.SH": "ST易购"})
    assert len(pool) == 1
    assert pool[0]["name"] == "ST易购"
    assert pool[0]["days"] == 1
    assert pool[0]["limit_pct"] == 5.0
    # 不带名称(不知是 ST) → 5% 不是涨停, 不入池
    assert build_pool_items(old) == []

    # 同一涨幅放到新规后(2026-11-09): 需要 10%, 不入池
    new = {
        "600001.SH": {
            "Close": ["10.0", "10.5"], "Amount": ["100", "100"],
            "Date": ["20261106", "20261109"],
        }
    }
    assert build_pool_items(new, names={"600001.SH": "ST易购"}) == []


# ════════════════ ② 日期格式归一 ════════════════
def test_window_bars_accepts_iso_request_date():
    from src.collectors.market_sentiment_collector import _window_bars

    bars = {
        "000001.SZ": {"Date": ["20261106", "20261109"], "Close": [10, 11], "Amount": [1, 1]},
        "000002.SZ": {"Date": ["20261105", "20261106"], "Close": [10, 11], "Amount": [1, 1]},
    }
    window, covered = _window_bars(bars, "2026-11-09")
    assert covered is True
    assert set(window) == {"000001.SZ"}
    assert window["000001.SZ"]["Date"] == ["20261106", "20261109"]


def test_window_bars_normalizes_dashed_and_int_dates():
    from src.collectors.market_sentiment_collector import _window_bars

    bars = {"000001.SZ": {"Date": ["2026-11-06", "2026-11-09"], "Close": [10, 11]}}
    window, covered = _window_bars(bars, "20261109")
    assert covered is True
    assert window["000001.SZ"]["Close"] == [10, 11]

    bars_int = {"000001.SZ": {"Date": [20261106, 20261109], "Close": [10, 11]}}
    window2, covered2 = _window_bars(bars_int, "2026-11-09")
    assert covered2 is True
    assert window2["000001.SZ"]["Close"] == [10, 11]


def test_ymd_to_iso_accepts_both_formats():
    from src.collectors.market_sentiment_collector import _ymd_to_iso

    assert _ymd_to_iso("20261109") == "2026-11-09"
    assert _ymd_to_iso("2026-11-09") == "2026-11-09"
    assert _ymd_to_iso(" 2026/11/09 ") == "2026-11-09"
    assert _ymd_to_iso(None) is None
    assert _ymd_to_iso("") is None


def test_get_limit_up_pool_normalizes_date_to_ymd(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    seen: list = []

    def fake_tq(self, date=None):
        seen.append(date)
        return [{"code": "1", "name": "A", "days": 1, "as_of": "2026-11-06", "source": "tq"}]

    monkeypatch.setattr(MarketSentimentCollector, "_limit_up_pool_tq", fake_tq, raising=True)
    pool = MarketSentimentCollector().get_limit_up_pool("2026-11-06")
    assert seen == ["20261106"]
    assert pool and pool[0]["as_of"] == "2026-11-06"


def test_get_limit_up_pool_refuses_unparseable_date(monkeypatch):
    """日期解析不了 → 空池(不猜成今天), 且不触发任何源。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    called: list = []
    monkeypatch.setattr(
        MarketSentimentCollector, "_limit_up_pool_tq",
        lambda self, date=None: called.append(date) or [],
        raising=True,
    )
    assert MarketSentimentCollector().get_limit_up_pool("昨天") == []
    assert called == []


# ════════════════ ③ 连板窗口(12 连板边界) ════════════════
def test_tq_bar_window_covers_twelve_boards():
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    # 数出 12 连板需要 13 根收盘价(12 段涨幅)
    assert MarketSentimentCollector._TQ_BARS >= 13


def test_collector_reports_twelve_consecutive_boards(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    closes = [10.0]
    for _ in range(12):
        closes.append(round(closes[-1] * 1.10, 2))
    dates = [f"2026{m:02d}{d:02d}" for m, d in zip([9] * 12 + [10], range(1, 14))]
    codes = ["600001.SH"]
    seen_counts: list[int] = []

    def fake_rpc(method, params, timeout=None):
        if method == "get_stock_list":
            return [{"Code": c, "Name": "样本"} for c in codes]
        if method == "get_market_data":
            n = int(params.get("count") or 0)
            seen_counts.append(n)
            return {c: {"Close": closes[-n:], "Amount": [1] * n, "Date": dates[-n:]} for c in codes}
        return {}

    import marketdata.vendors.tq as tqmod

    monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)
    monkeypatch.setattr("src.core.tdx_boards.stock_blocks", lambda sym: [], raising=False)

    pool = MarketSentimentCollector()._limit_up_pool_tq()
    assert max(seen_counts) >= 13
    assert pool and pool[0]["days"] == 12


# ════════════════ ④ 覆盖率: 取数不全不得当完整池 ════════════════
def _patch_partial_tq(monkeypatch):
    """两批代码, 第二批抛异常 → 只取到一半; 池子仍应返回但标 partial。"""
    import marketdata.vendors.tq as tqmod

    codes = [f"6000{i:02d}.SH" for i in range(60)]

    def fake_rpc(method, params, timeout=None):
        if method == "get_stock_list":
            return [{"Code": c, "Name": f"S{c}"} for c in codes]
        if method == "get_market_data":
            part = params.get("stock_list") or []
            if any(c.startswith("600050") for c in part):
                raise RuntimeError("boom")
            return {
                c: {"Close": ["10.0", "11.0"], "Amount": ["1", "1"], "Date": ["20261106", "20261109"]}
                for c in part
            }
        return {}

    monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)


def test_tq_partial_fetch_marks_coverage_partial(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector
    from src.core.market_mainline import aggregate_mainline

    _patch_partial_tq(monkeypatch)
    monkeypatch.setattr("src.core.tdx_boards.stock_blocks", lambda sym: [], raising=False)

    pool = MarketSentimentCollector()._limit_up_pool_tq()
    assert pool, "取到的一半仍要给出(不能因为不全就返回空)"
    cov = pool[0]["pool_coverage"]
    assert cov["status"] == "partial"
    assert cov["fetched_codes"] < cov["requested_codes"]

    r = aggregate_mainline(pool)
    meta = r["data_meta"]
    assert meta["pool_coverage"]["status"] == "partial"
    assert "下限" in meta["coverage_note"]
    assert meta["pool_coverage"]["fetched_codes"] < meta["pool_coverage"]["requested_codes"]


def test_tq_full_fetch_marks_coverage_complete(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector
    from src.core.market_mainline import aggregate_mainline

    import marketdata.vendors.tq as tqmod

    codes = ["600001.SH", "600002.SH"]

    def fake_rpc(method, params, timeout=None):
        if method == "get_stock_list":
            return [{"Code": c, "Name": "样本"} for c in codes]
        if method == "get_market_data":
            part = params.get("stock_list") or []
            return {
                c: {"Close": ["10.0", "11.0"], "Amount": ["1", "1"], "Date": ["20261106", "20261109"]}
                for c in part
            }
        return {}

    monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)
    monkeypatch.setattr("src.core.tdx_boards.stock_blocks", lambda sym: [], raising=False)

    pool = MarketSentimentCollector()._limit_up_pool_tq()
    assert pool[0]["pool_coverage"]["status"] == "complete"
    r = aggregate_mainline(pool)
    assert r["data_meta"]["pool_coverage"]["status"] == "complete"
    assert r["data_meta"]["coverage_note"] == ""
    assert r["data_meta"]["as_of_basis"] == "kline"
    assert r["data_meta"]["as_of_verified"] is True


def test_request_date_basis_is_not_marked_verified():
    """降级源按请求日返回的 as_of 不可当 K线证实 —— 必须 basis=source_request。"""
    from src.core.market_mainline import aggregate_mainline

    pool = [
        _z(
            str(i), f"S{i}", 2, source="eastmoney",
            as_of="2026-11-06", as_of_basis="source_request",
        )
        for i in range(3)
    ]
    meta = aggregate_mainline(pool)["data_meta"]
    assert meta["as_of"] == "2026-11-06"
    assert meta["as_of_basis"] == "source_request"
    assert meta["as_of_verified"] is False


def test_missing_as_of_basis_is_unknown():
    from src.core.market_mainline import aggregate_mainline

    pool = [_z(str(i), f"S{i}", 2, as_of="2026-11-06") for i in range(3)]
    meta = aggregate_mainline(pool)["data_meta"]
    assert meta["as_of_basis"] == "unknown"
    assert meta["as_of_verified"] is False


def test_missing_coverage_meta_is_unknown_not_complete():
    """池子没带覆盖率信息 → unknown, 不许当成"全市场完整"。"""
    from src.core.market_mainline import aggregate_mainline

    pool = [{"code": str(i), "name": f"S{i}", "days": 2, "sector": "X", "amount": 1e8, "source": "tq"} for i in range(3)]
    r = aggregate_mainline(pool)
    meta = r["data_meta"]
    assert meta["pool_coverage"]["status"] == "unknown"
    assert "未提供" in meta["coverage_note"]
    assert "完整" in meta["coverage_note"]


# ════════════════ ⑤ 非当日池 + 当前概念归属不得伪装同日 ════════════════
def _z(code, name, days, sector="X", concepts=None, source="tq", **kw):
    row = {
        "code": code,
        "name": name,
        "days": days,
        "sector": sector,
        "theme": "",
        "concepts": list(concepts or []),
        "amount": 1e8,
        "source": source,
    }
    row.update(kw)
    return row


def test_concept_as_of_differs_from_pool_as_of_is_stated():
    from src.core.market_mainline import aggregate_mainline

    pool = [
        _z(str(i), f"S{i}", 2, concepts=["商业航天"], as_of="2026-11-06", block_as_of="2026-11-09")
        for i in range(3)
    ]
    r = aggregate_mainline(pool)
    meta = r["data_meta"]
    assert meta["as_of"] == "2026-11-06"
    assert meta["concept_as_of"] == "2026-11-09"
    assert "不同日" in meta["concept_as_of_note"]
    assert "2026-11-06" in meta["concept_as_of_note"]
    assert "2026-11-09" in meta["concept_as_of_note"]


def test_concept_as_of_same_day_has_no_warning():
    from src.core.market_mainline import aggregate_mainline

    pool = [
        _z(str(i), f"S{i}", 2, concepts=["商业航天"], as_of="2026-11-09", block_as_of="2026-11-09")
        for i in range(3)
    ]
    r = aggregate_mainline(pool)
    assert r["data_meta"]["concept_as_of"] == "2026-11-09"
    assert r["data_meta"]["concept_as_of_note"] == ""


def test_concept_as_of_unknown_is_not_guessed():
    from src.core.market_mainline import aggregate_mainline

    pool = [_z(str(i), f"S{i}", 2, concepts=["商业航天"], as_of="2026-11-06") for i in range(3)]
    r = aggregate_mainline(pool)
    assert r["data_meta"]["concept_as_of"] is None
    assert "未知" in r["data_meta"]["concept_as_of_note"] or "未提供" in r["data_meta"]["concept_as_of_note"]


def test_concept_source_inference_is_not_marked_verified():
    """没有来源标记时按 TQ 族推定 —— 必须标 source_verified=False(缺来源不当真凭据)。"""
    from src.core.market_mainline import aggregate_mainline

    rows = [_z(str(i), f"S{i}", 2, concepts=["商业航天"], as_of="2026-11-09") for i in range(3)]
    meta = aggregate_mainline(rows)["data_meta"]
    assert meta["concept_source"] == "tq:get_relation"
    assert meta["concept_source_verified"] is False

    tagged = [
        _z(str(i), f"S{i}", 2, concepts=["商业航天"], as_of="2026-11-09", block_source="tq:get_relation")
        for i in range(3)
    ]
    assert aggregate_mainline(tagged)["data_meta"]["concept_source_verified"] is True


def test_enrich_pool_blocks_stamps_block_as_of(monkeypatch):
    """概念归属是"当前"成员关系 → 必须带反查日期, 供上游与池子数据日比对。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    monkeypatch.setattr(
        "src.core.tdx_boards.stock_blocks",
        lambda sym: [{"code": "880301.SH", "name": "商业航天", "type": "gn"}],
        raising=False,
    )
    rows = [{"code": "1", "name": "A", "days": 1, "source": "eastmoney"}]
    out = MarketSentimentCollector()._enrich_pool_blocks(rows)
    assert out[0]["concepts"] == ["商业航天"]
    assert out[0]["block_as_of"] == date.today().isoformat()


def test_tq_pool_without_date_marks_concept_day_separately(monkeypatch):
    """盘前(不传日期)拿到上一交易日池 + 今天反查的概念 → 两个日期必须分别标注。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    import marketdata.vendors.tq as tqmod

    def fake_rpc(method, params, timeout=None):
        if method == "get_stock_list":
            return [{"Code": "600001.SH", "Name": "样本"}]
        if method == "get_market_data":
            return {"600001.SH": {"Close": ["10.0", "11.0"], "Amount": ["1", "1"], "Date": ["20261030", "20261106"]}}
        return {}

    monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)
    monkeypatch.setattr(
        "src.core.tdx_boards.stock_blocks",
        lambda sym: [{"code": "880301.SH", "name": "商业航天", "type": "gn"}],
        raising=False,
    )

    pool = MarketSentimentCollector()._limit_up_pool_tq()
    assert pool[0]["as_of"] == "2026-11-06"
    assert pool[0]["block_as_of"] == date.today().isoformat()

    from src.core.market_mainline import aggregate_mainline

    meta = aggregate_mainline(pool)["data_meta"]
    if date.today().isoformat() != "2026-11-06":
        assert "不同日" in meta["concept_as_of_note"]


def test_zdt_backup_uses_real_names_for_st_boards(monkeypatch):
    """快照备源也要拿真实名称判 ST(主板 5%), 否则备源把 ST 连板算成 0。"""
    import marketdata.vendors.tq as tqmod
    from src.core import limit_pool_zdt as zdt

    def fake_rpc(method, params, timeout=None):
        if method == "get_stock_list":
            return [{"Code": "600001.SH", "Name": "ST样本"}]
        if method == "get_market_data":
            return {"600001.SH": {"Close": ["10.0", "10.5"], "Amount": ["1", "1"]}}
        return {}

    monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)
    monkeypatch.setattr(
        tqmod,
        "zdt_snapshot",
        lambda part: {"600001.SH": {"OpenTimesZT": 0, "FDVolMaxZT": 100, "FirstTimeZT": 93000}},
        raising=True,
    )
    pool = zdt.zdt_fallback_pool()
    assert len(pool) == 1
    assert pool[0]["name"] == "ST样本"
    assert pool[0]["days"] == 1


# ════════════════ ⑥ 无可信日期不得按今日持久化 ════════════════
class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, stmt, params=None):
        self.sink.append(dict(params or {}))
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeEngine:
    def __init__(self, sink):
        self.sink = sink

    def connect(self):
        return _FakeConn(self.sink)

    def begin(self):
        return _FakeConn(self.sink)


def _patch_engine(monkeypatch, sink):
    import src.web.database as db

    monkeypatch.setattr(db, "engine", _FakeEngine(sink), raising=True)


def test_daily_snapshot_skipped_without_trusted_date(monkeypatch):
    from src.web.api import market_mainline as api

    sink: list = []
    _patch_engine(monkeypatch, sink)
    payload = {"ranked_groups": [{"name": "AI算力", "rank": 1}], "data_meta": {}}
    assert api._upsert_daily_snapshot(payload) is False
    assert sink == [], "无可信日期不得落今日快照"


def test_daily_snapshot_uses_payload_as_of(monkeypatch):
    from src.web.api import market_mainline as api

    sink: list = []
    _patch_engine(monkeypatch, sink)
    payload = {"ranked_groups": [], "data_meta": {"as_of": "2026-11-06"}}
    assert api._upsert_daily_snapshot(payload) is True
    assert sink and sink[0]["date"] == "2026-11-06"


def test_data_date_accepts_compact_ymd():
    from src.web.api import market_mainline as api

    assert api._data_date("20261106") == "2026-11-06"
    assert api._data_date("2026-11-06") == "2026-11-06"
    assert api._data_date("") is None
    assert api._data_date(None) is None
    assert api._data_date("昨天") is None
    assert api._data_date("2026-13-40") is None


# ──────────── market_phase sync: 数据日不符不得按 target 落库 ────────────
@pytest.fixture
def in_mem_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.web import models, database as db
    from src.web.api import market_phase as mpm

    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sm)
    import src.db.dialect as _dialect

    monkeypatch.setattr(_dialect, "is_postgres", lambda: False)
    monkeypatch.setattr(mpm, "SessionLocal", sm)
    from src.web.cache.biz_cache import biz_cache

    biz_cache.clear()
    return models, sm


def test_sync_refuses_pool_from_another_trading_day(in_mem_db, monkeypatch):
    from src.web.api import market_phase as api
    from src.web.models import MarketPhaseDaily

    models, sm = in_mem_db
    target = date.today()
    other = (target - timedelta(days=3)).isoformat()

    class MockCol:
        def get_limit_up_pool(self, date_str=None):
            return [
                {"code": "000001", "days": 1, "as_of": other, "source": "tq"},
                {"code": "000002", "days": 2, "as_of": other, "source": "tq"},
            ]

        def get_index_snapshot(self):
            return [{"name": "上证指数", "pct": 0.5}]

    monkeypatch.setattr(api, "MarketSentimentCollector", MockCol)

    resp = api.sync_phase()
    assert resp["synced"] is False
    assert resp.get("data_date") == other
    assert "数据日" in resp["error"] or "不同" in resp["error"]

    db = sm()
    try:
        assert db.query(MarketPhaseDaily).count() == 0
    finally:
        db.close()


def test_sync_persists_when_pool_date_matches_target(in_mem_db, monkeypatch):
    from src.web.api import market_phase as api

    models, sm = in_mem_db
    today = date.today().isoformat()

    class MockCol:
        def get_limit_up_pool(self, date_str=None):
            return [
                {"code": f"{i:06d}", "days": 1, "as_of": today, "source": "tq"} for i in range(5)
            ]

        def get_index_snapshot(self):
            return [{"name": "上证指数", "pct": 0.5}]

    monkeypatch.setattr(api, "MarketSentimentCollector", MockCol)

    resp = api.sync_phase()
    assert resp["synced"] is True, resp
    assert resp["data_date"] == today
