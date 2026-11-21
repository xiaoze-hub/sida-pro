"""探针端到端：用本机回环假服务真实跑一遍分层 smoke（2026-11-21）。

不是 mock 掉 `run_probe`，而是让它真的发 HTTP：公开资源 → 登录身份 → UI 数据契约
→ 提交 run → 轮询 → 取会话终稿。假服务只模拟**协议形状**（真实产品服务不在单测里
启动，也不连外网）。

三条红线在这里落地：
  · 只有进度、没有终稿 → 失败；
  · run ok 但正文是半截/占位 → 失败；
  · 时间线只报观测值, ttft_available 恒为 False。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from scripts import copilot_smoke as probe

QUESTION_ANSWER = "今日主线在算力与半导体，成交额较昨日放大，情绪温度回到中性偏暖。" * 3


class _State:
    def __init__(self):
        self.polls = 0
        self.mode = "ok"          # ok | short | hang
        self.answer = QUESTION_ANSWER
        self.token_seen = ""
        self.cancel_calls: list[str] = []
        self.cancelled = False


class _Handler(BaseHTTPRequestHandler):
    state: _State

    def log_message(self, *_args):  # 静默
        pass

    def _send(self, code, payload, content_type="application/json"):
        if 200 <= code < 300:
            if isinstance(payload, dict) and "run" in payload:
                payload["run"]["run_id"] = "r-probe"
            payload = {"code": 0, "success": True, "data": payload, "message": ""}
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        auth = self.headers.get("Authorization") or ""
        if auth:
            self.state.token_seen = auth

        if path == "/api/health":
            return self._send(200, {"status": "ok", "version": "test"})
        if path == "/":
            return self._html("<html><body><div id=app></div></body></html>")
        if path == "/ai-copilot":
            return self._html("<html><body><div id=app></div></body></html>")
        if path == "/api/auth/me":
            if not auth.startswith("Bearer "):
                return self._send(401, {"detail": "unauthorized"})
            return self._send(200, {"user": {"id": "u1", "username": "probe"}})
        if path == "/api/copilot/presets":
            return self._send(200, [{"id": "full_dossier"}])
        if path.startswith("/api/copilot/runs/"):
            self.state.polls += 1
            if self.state.cancelled:
                return self._send(200, {"run": {"status": "cancelled", "conversation_id": 7},
                                        "steps": []})
            if self.state.mode == "hang":
                return self._send(200, {"run": {"status": "running", "conversation_id": 7},
                                        "steps": [{"seq": 1, "tool": "get_quote", "ok": True}]})
            if self.state.mode == "degraded" and self.state.polls >= 2:
                return self._send(200, {
                    "run": {"status": "degraded", "conversation_id": 7, "tool_calls": 2,
                            "elapsed_ms": 1400},
                    "steps": [
                        {"seq": 1, "tool": "get_quote", "ok": True, "elapsed_ms": 10},
                        {"seq": 2, "tool": "get_kline", "ok": False, "elapsed_ms": 5},
                    ],
                })
            if self.state.polls < 2:
                return self._send(200, {"run": {"status": "running", "conversation_id": 7},
                                        "steps": [{"seq": 1, "tool": "get_quote", "ok": True}]})
            return self._send(200, {
                "run": {"status": "ok", "conversation_id": 7, "tool_calls": 1,
                        "elapsed_ms": 1200},
                "steps": [{"seq": 1, "tool": "get_quote", "ok": True, "elapsed_ms": 10}],
            })
        if path.startswith("/api/chat/conversations/"):
            content = self.state.answer if self.state.mode != "short" else "好的"
            return self._send(200, {
                "conversation": {"id": 7},
                "messages": [
                    {"role": "user", "content": "今天主线是什么"},
                    {"role": "assistant", "content": content,
                     "run_id": "other-run" if self.state.mode == "unlinked" else "r-probe"},
                ],
            })
        return self._send(404, {"detail": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if path == "/api/copilot/runs":
            payload = json.loads(raw or b"{}")
            assert payload.get("question")
            return self._send(200, {"run_id": "r-probe", "status": "queued"})
        if path.startswith("/api/copilot/runs/") and path.endswith("/cancel"):
            run_id = path.rsplit("/", 2)[-2]
            self.state.cancel_calls.append(run_id)
            self.state.cancelled = True
            return self._send(200, {"run_id": run_id, "status": "cancelled"})
        return self._send(404, {"detail": "not found"})


@pytest.fixture()
def server():
    state = _State()
    handler = type("H", (_Handler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    finally:
        httpd.shutdown()
        httpd.server_close()


def _probe(server, **kw):
    base, state = server
    return probe.run_probe(
        base=base, allow_production=False, question="今天主线是什么？",
        min_chars=probe.MIN_ANSWER_CHARS, timeout_s=kw.pop("timeout_s", 3.0),
        poll_s=kw.pop("poll_s", 0.05), env={"SIDA_PROBE_TOKEN": "tok-abc"},
        **kw,
    )


def test_probe_end_to_end_success(server):
    report = _probe(server)
    base, state = server

    assert report["ok"] is True, report
    assert report["environment"] == "local"
    assert [k for k in report["layers"]] == ["public", "auth", "ui", "qa"]
    assert all(layer["ok"] for layer in report["layers"].values())

    tl = report["timeline"]
    assert tl["queue_ms"] is not None
    assert tl["fetch_to_first_step_ms"] is not None
    assert tl["total_ms"] is not None
    assert tl["ttft_available"] is False
    assert "ttft_ms" not in tl

    # 凭据不进报告
    assert "tok-abc" not in json.dumps(report)
    assert state.token_seen == "Bearer tok-abc"


def test_probe_end_to_end_fails_on_short_answer(server):
    _, state = server
    state.mode = "short"
    report = _probe(server)

    assert report["ok"] is False
    qa = report["layers"]["qa"]
    assert any("too_short" in r for r in qa["reasons"])


def test_probe_rejects_a_good_looking_reply_for_another_run(server):
    _, state = server
    state.mode = "unlinked"
    report = _probe(server)
    assert report["ok"] is False
    assert "final_answer_missing_or_too_short" in report["layers"]["qa"]["reasons"]


def test_probe_end_to_end_progress_without_terminal_is_failure(server):
    _, state = server
    state.mode = "hang"
    report = _probe(server, timeout_s=0.4)

    assert report["ok"] is False
    qa = report["layers"]["qa"]
    assert any("timeout_waiting_terminal" in r for r in qa["reasons"])
    # 进度/步骤都被看到过, 但仍不算成功
    assert report["timeline"]["fetch_to_first_step_ms"] is not None
    assert report["timeline"]["total_ms"] is None


def test_probe_reports_failed_tool_as_degradation_cause(server):
    _, state = server
    state.mode = "degraded"
    report = _probe(server)

    assert report["ok"] is False
    qa = report["layers"]["qa"]
    assert any("degraded" in r for r in qa["reasons"])
    assert qa["failed_tools"] == ["get_kline"]
    assert any("failed_tools=get_kline" in r for r in qa["reasons"])


# ── 超时收尾: 只取消自己刚提交的同一个 run (2026-11-21 复审) ───────────────
def test_probe_does_not_cancel_by_default(server):
    """默认不写：探针失败不等于可以替用户改 run 状态。"""
    _, state = server
    state.mode = "hang"
    report = _probe(server, timeout_s=0.4)

    assert report["ok"] is False
    assert state.cancel_calls == []
    assert report["layers"]["qa"]["cancel"] == {
        "attempted": False, "reason": "disabled",
    }


def test_probe_cancel_on_timeout_only_touches_its_own_run(server):
    _, state = server
    state.mode = "hang"
    report = _probe(server, timeout_s=0.4, cancel_on_timeout=True)

    assert report["ok"] is False, "取消成功也不代表这轮问答成功"
    assert state.cancel_calls == ["r-probe"], "只允许取消本次提交返回的同一个 run_id"
    cancel = report["layers"]["qa"]["cancel"]
    assert cancel["attempted"] is True
    assert cancel["ok"] is True
    assert cancel["http"] == 200
