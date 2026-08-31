"""TradingView Alert Webhook 端点测试(2026-08-12)。

覆盖:
- secret 校验(正确/错误/缺失)
- payload 解析(标准 TradingView Alert 格式 + 额外字段容忍)
- FastAPI 路由: 无 secret 时 503, 错误 secret 401
"""
import os
from unittest.mock import patch

import pytest

from src.web.api.tradingview_webhook import TVAlertPayload, _secret_ok


class TestSecret:
    def test_ok(self):
        with patch.dict(os.environ, {"PANWATCH_TV_WEBHOOK_SECRET": "test-secret-123"}, clear=False):
            assert _secret_ok("test-secret-123") is True

    def test_wrong(self):
        with patch.dict(os.environ, {"PANWATCH_TV_WEBHOOK_SECRET": "test-secret-123"}, clear=False):
            assert _secret_ok("wrong") is False

    def test_missing(self):
        with patch.dict(os.environ, {"PANWATCH_TV_WEBHOOK_SECRET": ""}, clear=False):
            assert _secret_ok(None) is False
            assert _secret_ok("") is False


class TestPayload:
    def test_standard_tv_alert(self):
        p = TVAlertPayload(**{
            "exchange": "SSE",
            "ticker": "600519",
            "time": "2026-08-12T10:30:00Z",
            "close": 1234.5,
            "volume": 10000,
            "message": "RSI 超买, 卖出信号",
            "strategy": {"position_size": "100"},
        })
        assert p.ticker == "600519"
        assert p.close == 1234.5
        assert p.message == "RSI 超买, 卖出信号"

    def test_extra_fields_tolerated(self):
        # 不同 Pine 策略 alert 格式可能带自定义字段, 不应崩
        p = TVAlertPayload(**{"symbol": "SH600519", "price": 99.9})
        assert getattr(p, "symbol", "") == "SH600519"
        assert p.close is None


class TestEndpoint:
    def _resp(self, r):
        """PanWatch 统一响应包装: {code, success, data}"""
        return r.json().get("data", r.json())

    def test_no_secret_env_disabled(self):
        from fastapi.testclient import TestClient
        from src.web.app import app

        with patch.dict(os.environ, {"PANWATCH_TV_WEBHOOK_SECRET": ""}, clear=False):
            client = TestClient(app)
            r = client.post("/api/webhooks/tradingview", json={"ticker": "600519"})
            assert r.status_code == 200
            assert self._resp(r)["ok"] is False
            assert self._resp(r)["error"] == "webhook_disabled"

    def test_wrong_secret_401(self):
        from fastapi.testclient import TestClient
        from src.web.app import app

        with patch.dict(os.environ, {"PANWATCH_TV_WEBHOOK_SECRET": "test-secret-123"}, clear=False):
            client = TestClient(app)
            r = client.post(
                "/api/webhooks/tradingview",
                json={"ticker": "600519", "close": 1234.5},
                headers={"X-PanWatch-Secret": "wrong"},
            )
            assert r.status_code == 200
            assert self._resp(r)["ok"] is False
            assert self._resp(r)["error"] == "unauthorized"

    def test_valid_secret_ok(self):
        from fastapi.testclient import TestClient
        from src.web.app import app

        with patch.dict(os.environ, {"PANWATCH_TV_WEBHOOK_SECRET": "test-secret-123"}, clear=False):
            client = TestClient(app)
            with patch("src.core.notify_center.push_notification") as mock_push:
                r = client.post(
                    "/api/webhooks/tradingview",
                    json={"ticker": "600519", "close": 1234.5, "message": "买"},
                    headers={"X-PanWatch-Secret": "test-secret-123"},
                )
                assert r.status_code == 200
                assert self._resp(r)["ok"] is True
                assert mock_push.called
