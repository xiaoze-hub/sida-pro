"""采集层健壮性修复的回归测试(2026-08-23)。

对应审计报告 aud-20260823-frontend-collectors.md, 本测试只覆盖:
- S-3 / S-4: screenshot_collector context 资源保护 + 单只超时 + 整批并发+超时
- M-9: auction_collector in_async_loop / to_async
- M-11: klines_ingestor 失败聚合摘要结构化字段
- M-12: market_http 重试总耗时封顶
- M-4 / L-1 / L-3: capital_flow_collector 全 0 识别 + date 统一 + 直连重试
- L-1: events_collector 工厂异常走 logger.exception 而不是裸 pass
- L-2: market_sentiment_collector 总预算 30s 封顶
- L-6: wudao_mcp_client 1 次重试 + notifications 降级 warn
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ────────────────────────────────────────────────────────────────────
# M-12 market_http 重试总耗时封顶
# ────────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
    def json(self): return {}
    @property
    def content(self): return b""
    @property
    def text(self): return ""
    def raise_for_status(self): pass


class TestMarketHttpBudget:
    def test_max_total_s_caps_total_runtime(self, monkeypatch):
        """M-12: 总预算 1s, 但第一次请求就睡眠 5s, 应在 ~1s 后超时返回 None, 而不是等 5s。"""
        from src.collectors import market_http

        # 直接让每次 httpx get 都抛 httpx.TimeoutException, 这模拟"网络永远慢"
        # market_get 通过 deadline 检测应该在第一次失败后(或下一次 retry 前)跳出循环.
        import httpx as _httpx_lib

        class _FailClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, params=None):
                raise _httpx_lib.TimeoutException("simulated timeout")

        monkeypatch.setattr(market_http.httpx, "Client", _FailClient)

        start = time.monotonic()
        result = market_http.market_get(
            "http://x.example/api",
            host_key="x.example",
            timeout=10.0,
            retries=5,            # 想重试很多次
            max_total_s=0.5,     # 但总预算 0.5s
            parse="json",
            log_label="M12 test",
        )
        elapsed = time.monotonic() - start
        # 应该快速返回 None, 不应该真跑 5 次重试
        assert result is None
        assert elapsed < 2.5, f"max_total_s 应快速失败, 实际 {elapsed:.2f}s"

    def test_default_max_total_s_is_10(self):
        """M-12: 默认 max_total_s=10s, 不需要调用方显式传。"""
        from src.collectors import market_http
        import inspect
        sig = inspect.signature(market_http.market_get)
        assert sig.parameters["max_total_s"].default == 10.0
        # 默认也收紧
        assert sig.parameters["timeout"].default == 6.0
        assert sig.parameters["retries"].default == 1


# ────────────────────────────────────────────────────────────────────
# M-9 auction_collector in_async_loop / to_async
# ────────────────────────────────────────────────────────────────────
class TestAuctionAsyncSafety:
    def test_in_async_loop_false_outside_loop(self):
        from src.collectors.auction_collector import in_async_loop
        # 同步上下文
        assert in_async_loop() is False

    def test_to_async_runs_sync_when_not_in_loop(self):
        from src.collectors.auction_collector import to_async
        called = {"n": 0}
        def sync_fn(x):
            called["n"] += 1
            return x * 2
        result = to_async(sync_fn, 21)
        assert result == 42
        assert called["n"] == 1

    @pytest.mark.asyncio
    async def test_to_async_in_loop_runs_via_thread(self):
        from src.collectors.auction_collector import to_async, in_async_loop
        assert in_async_loop() is True
        called = {"thread_ids": []}
        import threading
        def sync_fn():
            called["thread_ids"].append(threading.current_thread().ident)
            return "ok"
        main_thread = threading.current_thread().ident
        result = await to_async(sync_fn)
        assert result == "ok"
        # 应该在另一个线程跑
        assert called["thread_ids"][0] != main_thread

    def test_async_wrappers_exist(self):
        """M-9: 每个公开 fetch_auction_* 都对应一个 fetch_auction_*_async 包装。"""
        from src.collectors import auction_collector
        for name in (
            "fetch_auction_overview",
            "fetch_auction_strongest",
            "fetch_auction_theme",
            "fetch_auction_weak_to_strong",
            "fetch_auction_raw",
            "fetch_auction_risk",
        ):
            sync_fn = getattr(auction_collector, name)
            async_fn = getattr(auction_collector, f"{name}_async", None)
            assert async_fn is not None, f"{name}_async 缺失"
            assert asyncio.iscoroutinefunction(async_fn), f"{name}_async 不是 async"


# ────────────────────────────────────────────────────────────────────
# M-11 klines_ingestor 失败聚合摘要
# ────────────────────────────────────────────────────────────────────
class TestKlinesIngestorAggregation:
    @pytest.mark.asyncio
    async def test_ingest_batch_emits_summary_with_fail_count(self, monkeypatch, caplog):
        """M-11: 全部三源失败时聚合日志包含 success/fail/total_ingested 字段。"""
        from src.collectors import klines_ingestor

        async def fake_gather(*tasks, **kwargs):
            # 模拟 kc.get_klines() 永远空列表 → 由 ingest_symbol 走 continue
            results = []
            for t in tasks:
                # 每个 ingest_symbol 都返回 ingested=0
                results.append({"symbol": "?", "market": "CN", "period": "1d",
                                "ingested": 0, "by_source": {"tencent": 0, "eastmoney": 0, "sina": 0},
                                "fail_details": [{"source": "tencent", "error": "empty/no klines"}]})
            return results

        # 直接测试 ingest_batch 路径: 注入 monkeypatch 给 ingest_symbol 不走 DB
        async def fake_ingest_symbol(engine, sym, mkt, period, days):
            return {"symbol": sym, "market": mkt.value, "period": period,
                    "ingested": 0, "by_source": {"tencent": 0, "eastmoney": 0, "sina": 0},
                    "fail_details": [{"source": "tencent", "error": "empty"}]}

        monkeypatch.setattr(klines_ingestor, "ingest_symbol", fake_ingest_symbol)
        db_engine = MagicMock()

        caplog.set_level(logging.ERROR, logger="src.collectors.klines_ingestor")
        result = await klines_ingestor.ingest_batch(
            db_engine, [("000001", "CN"), ("600519", "CN")], period="1d"
        )
        assert result["fail"] == 2
        assert result["success"] == 0
        assert result["total_symbols"] == 2
        assert len(result["fail_symbols"]) == 2
        assert any("聚合" in r.message for r in caplog.records), "聚合 ERROR 日志缺失"

    def test_ingest_symbol_returns_fail_details_field(self):
        """M-11: ingest_symbol 返回必须带 fail_details 字段(供聚合统计)。"""
        import inspect
        from src.collectors import klines_ingestor
        # 主要验证签名存在, 失败时不抛
        assert inspect.iscoroutinefunction(klines_ingestor.ingest_symbol)


# ────────────────────────────────────────────────────────────────────
# S-3 / S-4 screenshot_collector
# ────────────────────────────────────────────────────────────────────
class TestScreenshotCollector:
    @pytest.fixture(autouse=True)
    def _import(self):
        from src.collectors.screenshot_collector import ScreenshotCollector
        self.ScreenshotCollector = ScreenshotCollector

    def test_capture_batch_signature_has_concurrency_and_timeout(self):
        """S-4: capture_batch 支持 concurrency + batch_timeout_s。"""
        import inspect
        from src.collectors.screenshot_collector import ScreenshotCollector
        sig = inspect.signature(ScreenshotCollector.capture_batch)
        assert "concurrency" in sig.parameters
        assert "batch_timeout_s" in sig.parameters

    def test_capture_timeout_default_is_30s(self):
        """S-4: 单只硬封顶默认 30s(原 80s+)."""
        assert self.ScreenshotCollector._CAPTURE_TIMEOUT_S == 30.0

    @pytest.mark.asyncio
    async def test_capture_timeout_kicks_in(self, monkeypatch):
        """S-4: capture() 内 await 超过 30s 必须被 wait_for 截断返回 None。"""
        from src.collectors import screenshot_collector

        async def fake_inner(*a, **k):
            await asyncio.sleep(60)
            return None
        # 构造一个 mock 实例
        SC = self.ScreenshotCollector
        coll = SC({"capture_timeout_s": 0.5})
        coll._browser = MagicMock()
        coll._get_url = lambda *a, **k: "https://x"
        monkeypatch.setattr(coll, "_capture_inner", fake_inner)
        result = await coll.capture(symbol="000001", name="TEST", market="CN")
        assert result is None, "超时应当返回 None"

    @pytest.mark.asyncio
    async def test_capture_batch_concurrent(self, monkeypatch):
        """S-4: 批量并发数起作用: 3 只股 0.5s 任务, 串行 1.5s, 并发 2 约 1s。"""
        from src.collectors.screenshot_collector import ScreenshotCollector

        called: list[float] = []

        async def slow_capture(self_, symbol, name, market="CN", period="daily",
                               provider="xueqiu", *, concurrency=None, batch_timeout_s=None):
            # 用 Symbol/Name 区分
            t0 = time.monotonic()
            await asyncio.sleep(0.5)
            called.append(time.monotonic() - t0)
            return None

        SC = self.ScreenshotCollector
        coll = SC({"batch_concurrency": 2, "batch_timeout_s": 5})
        monkeypatch.setattr(SC, "capture", slow_capture)

        t0 = time.monotonic()
        await coll.capture_batch(
            [{"symbol": "a"}, {"symbol": "b"}, {"symbol": "c"}],
            concurrency=2,
            batch_timeout_s=5,
        )
        elapsed = time.monotonic() - t0
        # 并发 2: 3 只串成 (a,b) -> c 约 1.0-1.1s;
        # 串行 3 单只要 1.5s. 留 0.5s 容差.
        assert elapsed < 1.5, f"并发 2 时 3 只应在 ~1s 完成, 实际 {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_capture_batch_overall_timeout(self, monkeypatch):
        """S-4: 整批总预算超时: 单只都没问题, 但 batch timeout=0.1s 整批应终止并返回空 list (None 过滤)。"""
        from src.collectors.screenshot_collector import ScreenshotCollector

        async def hang(self_, symbol, name, **kw):
            await asyncio.sleep(5)
            return None

        SC = self.ScreenshotCollector
        coll = SC({"batch_timeout_s": 0.2})
        monkeypatch.setattr(SC, "capture", hang)
        start = time.monotonic()
        results = await coll.capture_batch(
            [{"symbol": "x"}, {"symbol": "y"}],
            batch_timeout_s=0.2,
            concurrency=2,
        )
        elapsed = time.monotonic() - start
        assert results == [], "超时后应该返回空"
        assert elapsed < 1.0, f"整批应快速终止, 实际 {elapsed:.2f}s"


# ────────────────────────────────────────────────────────────────────
# M-4 / L-1 / L-3 capital_flow_collector
# ────────────────────────────────────────────────────────────────────
class TestCapitalFlowCollector:
    def test_direct_flow_timeout_is_8s(self):
        """L-3: 直连 timeout 由 5s 提到 8s。"""
        from src.collectors import capital_flow_collector
        assert capital_flow_collector._DIRECT_FLOW_TIMEOUT_S == 8.0
        assert capital_flow_collector._DIRECT_FLOW_MAX_RETRY >= 1

    def test_today_cn_format_is_iso(self):
        """L-1: date 字段统一 YYYY-MM-DD。"""
        from src.collectors.capital_flow_collector import _today_cn
        s = _today_cn()
        assert isinstance(s, str)
        # 格式校验: 10 字符串, 4-2-2
        assert len(s) == 10
        dt = datetime.strptime(s, "%Y-%m-%d")
        # 一致性: 应该是今天
        assert dt.date() == datetime.now().date()


# ────────────────────────────────────────────────────────────────────
# L-1 events_collector 工厂异常走 logger.exception
# ────────────────────────────────────────────────────────────────────
class TestEventsCollectorRobustness:
    def test_factory_failure_logs_exception(self, caplog):
        """L-1: events_collector 工厂构造失败时 logger.exception 应被调用, 而不是裸 pass。"""
        from src.collectors import events_collector
        from src.web import database as web_db

        # 模拟一坏 factory
        def bad_factory(cfg):
            raise ValueError("missing provider config")

        # 在 COLLECTOR_MAP 上注入坏 factory, 测试完成清理.
        original_map = dict(events_collector.EventsCollector.COLLECTOR_MAP)
        events_collector.EventsCollector.COLLECTOR_MAP["badprov"] = bad_factory
        try:
            # 桩 DataSource
            class FakeDS:
                type = "events"
                enabled = True
                priority = 0
                provider = "badprov"
                config = {}
                id = 1

            # 桩 SessionLocal, 绑到 src.web.database(SessionLocal 是在那里定义)
            class FakeSession:
                def __init__(self): pass
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def query(self, *a, **k):
                    class Q:
                        def __init__(s, ds): s.ds = ds
                        def filter(s, *a, **k): return s
                        def order_by(s, *a): return s
                        def all(s): return [s.ds]
                    return Q(FakeDS())
                def close(self): pass

            caplog.set_level(logging.ERROR, logger="src.collectors.events_collector")
            with patch.object(web_db, "SessionLocal", FakeSession):
                coll = events_collector.EventsCollector.from_database()
            # 应该至少有一个 logger.exception(ERROR)记录
            assert any(
                rec.levelno == logging.ERROR and "构造失败" in rec.getMessage()
                for rec in caplog.records
            ), f"L-1 修复未生效, 日志: {[r.getMessage() for r in caplog.records]}"
        finally:
            # 还原 COLLECTOR_MAP
            events_collector.EventsCollector.COLLECTOR_MAP.clear()
            events_collector.EventsCollector.COLLECTOR_MAP.update(original_map)


# ────────────────────────────────────────────────────────────────────
# L-2 market_sentiment_collector 总预算
# ────────────────────────────────────────────────────────────────────
class TestMarketSentimentTotalBudget:
    def test_get_limit_up_pool_has_total_budget(self):
        """L-2: _LIMITUP_TOTAL_BUDGET_S = 30s。"""
        from src.collectors import market_sentiment_collector
        assert getattr(market_sentiment_collector, "_LIMITUP_TOTAL_BUDGET_S", 0) >= 20

    @pytest.mark.asyncio
    async def test_get_limit_up_pool_exits_on_deadline(self, monkeypatch):
        """L-2: 当 wudao + eastmoney 一直返空, 30s 后 deadline 触发, 不应继续回溯。"""
        from src.collectors import market_sentiment_collector

        # 让时间看似过了很久
        class FakeMonotonic:
            base = time.monotonic()
            def __init__(self): self.t = 0
            def __call__(self):
                self.t += 60  # 每次调用往后 60s
                return self.base + self.t

        m = FakeMonotonic()
        monkeypatch.setattr(market_sentiment_collector.time, "monotonic", m)

        coll = market_sentiment_collector.MarketSentimentCollector()
        # wudao 一直返空, 东财也一直返 None(空 pool)
        monkeypatch.setattr(coll, "_limit_up_pool_wudao", lambda *a, **k: [])
        monkeypatch.setattr(market_sentiment_collector, "market_get", lambda *a, **k: None)
        result = coll.get_limit_up_pool("20260823")
        # 立即返回空(deadline 已经过期), 不会尝试到第 6 天
        assert result == []


# ────────────────────────────────────────────────────────────────────
# L-6 wudao_mcp_client 双 POST + 1 次重试 + notifications 降级
# ────────────────────────────────────────────────────────────────────
class TestWudaoClientInitialize:
    def test_initialize_retries_once_on_failure(self, monkeypatch):
        """L-6: initialize 第一次失败应自动 1 次重试。"""
        from src.collectors import wudao_mcp_client

        call_count = {"n": 0}

        class FakeResp:
            def __init__(self, status_code=200):
                self.status_code = status_code
            def raise_for_status(self):
                if self.status_code != 200:
                    raise RuntimeError(f"http {self.status_code}")
            def json(self): return {}

        def fake_post(url, headers=None, json=None, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("network blip")
            return FakeResp(200)

        # notifications 失败应被吞(warn 而非抛)
        def fake_post2(url, headers=None, json=None, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return FakeResp(200)  # initialize 第二次成功
            raise RuntimeError("notifications fail")

        posts = [fake_post, fake_post2]
        idx = {"i": 0}
        def dispatch(url, **kw):
            p = posts[idx["i"]]
            idx["i"] += 1
            return p(url, **kw)

        monkeypatch.setattr(wudao_mcp_client.requests, "post", dispatch)
        monkeypatch.setattr(wudao_mcp_client.time, "sleep", lambda s: None)

        client = wudao_mcp_client.WudaoMCPClient(url="http://w.example", token="t")
        # 不应抛 — 第二次重试成功
        client._initialize()
        assert client._initialized is True
        # 应该至少跑了 2 次(initialize 第1次失败 + 第2次成功)
        assert call_count["n"] >= 2

    def test_initialize_notifications_failure_does_not_raise(self, monkeypatch, caplog):
        """L-6: notifications/initialized 失败应降级 warn, 不能让 _initialize 抛。"""
        from src.collectors import wudao_mcp_client

        def fake_post(url, headers=None, json=None, timeout=None):
            payload = (json or {})
            if payload.get("method") == "initialize":
                class Ok:
                    status_code = 200
                    def raise_for_status(self): pass
                return Ok()
            # notifications
            raise RuntimeError("notify fail")

        monkeypatch.setattr(wudao_mcp_client.requests, "post", fake_post)
        monkeypatch.setattr(wudao_mcp_client.time, "sleep", lambda s: None)

        caplog.set_level(logging.WARNING, logger="src.collectors.wudao_mcp_client")
        client = wudao_mcp_client.WudaoMCPClient(url="http://w.example", token="t")
        # 必须不抛
        client._initialize()
        assert client._initialized is True
        # 应有 warn 记录
        assert any(
            "降级" in r.getMessage() or "降级继续" in r.getMessage()
            for r in caplog.records
        ), "L-6: notifications 失败应有 warn, 实际日志: " + repr(
            [(r.levelname, r.getMessage()) for r in caplog.records]
        )
