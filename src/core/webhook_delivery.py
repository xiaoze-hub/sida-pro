"""Webhook 投递（P2-2.2, 2026-10-25）。

外部 Agent 提交任务后要能收到结果。**这里只做投递，不做业务**：

- **SSRF 防护**：内网地址（127./10./172.16-31./192.168./localhost/169.254.）默认拒绝。
  这不是洁癖：回调 URL 是用户填的，允许它指向内网等于给外部一个探测内网的工具。
- **有限重试**：3 次指数退避；失败要记录原因，**不许静默丢失**（用户以为会收到、其实没有，
  比直接报错更糟）。
- **载荷不含密钥/持仓原文**：只回 run_id / 状态 / 摘要长度 / 耗时。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_S = (0.2, 0.5)      # 测试可 monkeypatch 成 0；生产 3 次约 0.7s
TIMEOUT_S = 8

_PRIVATE_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


class WebhookRejected(ValueError):
    """回调地址不可用（协议/内网）。文案直接给调用方看。"""


def validate_callback_url(url: str, *, allow_private: bool = False) -> str:
    """校验回调地址，返回规范化后的 URL。不合法抛 `WebhookRejected`。"""
    raw = (url or "").strip()
    if not raw:
        raise WebhookRejected("callback_url 为空")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise WebhookRejected(f"只支持 http/https，收到 {parsed.scheme or '空'}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise WebhookRejected("callback_url 缺少主机名")
    if allow_private:
        return raw
    if host in _PRIVATE_HOSTS:
        raise WebhookRejected(f"拒绝内网回调地址：{host}")
    # 域名解析后再判一次：`evil.com` 可以解析到 127.0.0.1
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise WebhookRejected(f"回调地址无法解析：{host}（{exc}）") from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise WebhookRejected(f"拒绝内网回调地址：{host} → {addr}")
    return raw


def build_payload(*, run_id: str, status: str, question_len: int = 0,
                  elapsed_ms: int | None = None, error: str | None = None) -> dict:
    """回调载荷。**只放定位事实**，不放问题原文/持仓/密钥。"""
    return {
        "run_id": run_id,
        "status": status,
        "question_chars": int(question_len or 0),
        "elapsed_ms": elapsed_ms,
        "error": (error or "")[:300] or None,
    }


def deliver(url: str, payload: dict, *, attempts: int = MAX_ATTEMPTS,
            backoff: tuple[float, ...] = BACKOFF_S) -> dict:
    """POST 到回调地址，有限重试。**不抛异常**，把结果如实返回。

    返回 `{delivered, attempts, status_code, error}`：
      - `delivered=True` 表示对端 2xx；
      - 非 2xx / 网络错误都算失败并记录最后一次原因。
    """
    body = json.dumps(payload, ensure_ascii=False).encode()
    last_error = ""
    status_code: int | None = None
    for i in range(1, max(1, attempts) + 1):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "sida-webhook/1"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                status_code = int(getattr(r, "status", 200) or 200)
            if 200 <= status_code < 300:
                return {"delivered": True, "attempts": i, "status_code": status_code, "error": ""}
            last_error = f"HTTP {status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"[:200]
        if i < attempts:
            time.sleep(backoff[min(i - 1, len(backoff) - 1)] if backoff else 0)
    logger.warning("webhook 投递失败 url=%s attempts=%s err=%s", url, attempts, last_error)
    return {"delivered": False, "attempts": max(1, attempts),
            "status_code": status_code, "error": last_error}


def deliver_for_run(db, run_id: str) -> list[dict]:
    """把某个 run 的所有待发回调发出去，并把结果写回记录（尽力而为）。"""
    from src.db.models import WebhookSubscription

    rows = (
        db.query(WebhookSubscription)
        .filter(WebhookSubscription.run_id == run_id,
                WebhookSubscription.status == "pending")
        .all()
    )
    out: list[dict] = []
    for row in rows:
        payload = build_payload(run_id=run_id, status=row.status_snapshot or "ok")
        res = deliver(row.url, payload)
        row.attempts = res["attempts"]
        row.status = "delivered" if res["delivered"] else "failed"
        row.last_error = res["error"] or None
        out.append({"url": row.url, **res})
    if rows:
        db.commit()
    return out
