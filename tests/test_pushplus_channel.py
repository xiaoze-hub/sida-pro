import asyncio

import pytest

from src.core.notifier import NotifierManager
from src.core.notify_center import _PUSH_LEVELS
from src.web.api.channels import _channel_test_content


def test_pushplus_requires_token():
    with pytest.raises(ValueError, match="token"):
        NotifierManager().add_channel("pushplus", {})


def test_unknown_channel_is_rejected():
    with pytest.raises(ValueError, match="不支持"):
        NotifierManager().add_channel("unknown", {})


def test_info_notifications_are_forwarded():
    assert "info" in _PUSH_LEVELS


def test_channel_test_content_is_unique():
    first = _channel_test_content()
    second = _channel_test_content()

    assert first != second
    assert "测试时间：" in first
    assert "测试编号：" in first


def test_pushplus_accepts_string_success_code_and_returns_receipt(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": "200", "msg": "ok", "data": "message-123"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("src.core.notifier.httpx.AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr("src.core.notifier.get_global_proxy", lambda: "")

    receipt = asyncio.run(
        NotifierManager()._send_pushplus(
            {"token": "configured-token"},
            "test",
            "content",
        )
    )

    assert receipt == {"accepted": True, "message_id": "message-123"}
