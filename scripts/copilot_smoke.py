#!/usr/bin/env python3
"""数智参谋端到端 smoke 探针 + 7/30 天运行基线（2026-11-21）。

**这个脚本评判什么**（不是「接口 200」）：
  L1 public  公开资源：健康检查 + SPA 壳（未登录可达）
  L2 auth    登录后身份可用（token 通过 env 注入，绝不打印）
  L3 ui      登录后 UI 数据契约：/ai-copilot 页面可达 + 参谋 presets 可取
  L4 qa      真问题 → **会话里最后一条 assistant 正文**；run 必须 ok 终态，
             正文必须过最小长度且不是错误占位。SSE 的 status/progress/ping
             只证明「还在跑」，**不算成功**；半截进度文本也不算终稿。

**时间口径**（只报观测到的，没观测到写 null）：
  queue_ms            提交 → 首次看到非 queued
  fetch_to_first_step_ms  首次非 queued → 首次看到工具步骤（取数开始）
  first_observed_body_ms  提交 → 首次看到 assistant 正文
  total_ms            提交 → 终态
  ttft_available      恒为 False（本入口没有 token 级流；不拿结束时间冒充首字）

**安全边界**：
  - 默认只允许 loopback / 显式 staging；生产域名必须 `--allow-production`；
  - 每个问题都是通用市场问题，不带任何真实用户持仓/自选；
  - token 只从 env/文件读，输出与日志里不出现 token、密码、带凭据 URL。

用法：:

    # 本地/预发（token 先 export SIDA_PROBE_TOKEN=...）
    python scripts/copilot_smoke.py --base http://127.0.0.1:8000 --json

    # 生产必须显式开启（会产生一条真实问答记录）
    python scripts/copilot_smoke.py --base https://sida.hengsheng-elec.com --allow-production

    # 只读基线（7/30 天）
    python scripts/copilot_smoke.py --base http://127.0.0.1:8000 --baseline --json

退出码：0 全绿 / 1 有失败 / 2 配置或环境错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

DEFAULT_BASE = os.environ.get("SIDA_PROBE_BASE", "http://127.0.0.1:8000")
DEFAULT_QUESTION = "今天 A 股市场的主线题材是什么？"
MIN_ANSWER_CHARS = 80

#: 明确的生产域名（判不准的远端主机也按生产处理，默认拒绝）。
PRODUCTION_HOSTS = frozenset({"sida.hengsheng-elec.com", "www.sida.hengsheng-elec.com"})
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"})

#: 系统占位文案：拿到它等于没有终稿。
FAILURE_MARKERS = (
    "任务未能完成，请查看本轮过程。",
    "任务未能完成",
)

TERMINAL_STATUSES = frozenset({"ok", "degraded", "error", "failed", "cancelled", "succeeded"})


class ProbeConfigError(RuntimeError):
    """配置/环境错误（退出码 2），不是产品或接口失败。"""


# ── 判据 ──────────────────────────────────────────────────────────────────
def final_answer_text(messages, *, run_id: str | None = None) -> str:
    """取会话里**最后一条**非空 assistant 正文；没有返回空串。

    只认 assistant —— user 回显、工具进度条、system 提示都不是终稿。
    """
    for msg in reversed(list(messages or [])):
        if (msg or {}).get("role") != "assistant":
            continue
        if run_id is not None and msg.get("run_id") != run_id:
            continue
        content = msg.get("content") or ""
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def validate_min_chars(value: int) -> int:
    """最小终稿长度必须 ≥ 1；把门槛设成 0 等于让空回答过验收。"""
    value = int(value)
    if value < 1:
        raise ProbeConfigError(f"min_answer_chars 必须 ≥ 1，收到 {value}")
    return value


def evaluate_probe(
    *,
    terminal_status: str | None,
    final_content: str | None,
    min_chars: int = MIN_ANSWER_CHARS,
) -> dict:
    """端到端成功判据：**终态 ok + 真实终稿**，两者缺一不可。"""
    reasons: list[str] = []
    if terminal_status != "ok":
        reasons.append(f"terminal_status={terminal_status}")
    content = (final_content or "").strip()
    if len(content) < min_chars:
        reasons.append("final_answer_missing_or_too_short")
    elif any(marker in content for marker in FAILURE_MARKERS):
        reasons.append("final_answer_is_error_placeholder")
    return {"ok": not reasons, "reasons": reasons, "final_chars": len(content)}


# ── 目标与凭据 ────────────────────────────────────────────────────────────
def classify_endpoint(base: str) -> str:
    """local / staging / production。

    只有 loopback 与「主机名含 staging 且非生产域名」算低风险；其余远端一律
    按 production 处理 —— 判不准时默认拒绝，而不是猜。
    """
    host = (urlsplit(base).hostname or "").lower()
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return "local"
    if host in PRODUCTION_HOSTS:
        return "production"
    if "staging" in host or host.endswith(".test"):
        return "staging"
    return "production"


def ensure_allowed(base: str, *, allow_production: bool) -> str:
    """生产必须显式开启；返回环境名。"""
    target = urlsplit(base)
    if (not target.hostname or target.username is not None or target.password is not None
            or target.path not in ("", "/") or target.query or target.fragment):
        raise ProbeConfigError("目标必须是不含凭据、路径或查询参数的 HTTP(S) origin")
    if target.scheme != "https" and not (
        target.scheme == "http" and target.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ProbeConfigError("只有本地回环可使用 HTTP，远端必须使用 HTTPS")
    env = classify_endpoint(base)
    if env == "production" and not allow_production:
        raise ProbeConfigError(
            f"{urlsplit(base).hostname} 按生产处理：显式加 --allow-production 才允许发问"
            "（会写入一条真实问答记录，请用专用账号）"
        )
    return env


_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_URL_CRED_RE = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^:/@\s]+:)([^@\s]+)(@)")


def safe_text(text: str) -> str:
    """抹掉 Bearer token（输出/日志统一走这里）。"""
    return _BEARER_RE.sub(r"\1***", str(text))


def redact_url(url: str) -> str:
    """把 `scheme://user:password@host` 的密码段替换成 ***。"""
    return _URL_CRED_RE.sub(r"\1***\3", str(url))


def mask_secret(secret: str | None, *, keep: int = 4) -> str:
    """只留前缀用于确认「是哪把钥匙」，长度不足时全遮。"""
    if not secret:
        return ""
    if len(secret) <= keep:
        return "***"
    return f"{secret[:keep]}***"


# ── 时间线 ────────────────────────────────────────────────────────────────
@dataclass
class Timeline:
    """只记录**观测到**的时刻；缺失即 None，不用 0 或推断值填充。"""

    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str, t: float | None = None) -> None:
        self.marks.setdefault(name, time.monotonic() if t is None else float(t))

    def has(self, name: str) -> bool:
        return name in self.marks

    @staticmethod
    def _delta_ms(start: str, end: str, marks: dict) -> int | None:
        if start not in marks or end not in marks:
            return None
        return max(0, int((marks[end] - marks[start]) * 1000))

    def as_dict(self) -> dict:
        m = self.marks
        return {
            "queue_ms": self._delta_ms("submit", "dequeued", m),
            "fetch_to_first_step_ms": self._delta_ms("dequeued", "first_step", m),
            "first_observed_body_ms": self._delta_ms("submit", "first_body", m),
            "total_ms": self._delta_ms("submit", "final", m),
            # 没有 token 级流事实源：永不自称 TTFT
            "ttft_available": False,
            "first_body_is_final": bool(m.get("first_body_is_final")),
        }


def make_report(*, base: str, environment: str, layers: dict, timeline: Timeline | None = None) -> dict:
    """报告只含可复述事实：层结果 + 时间线（已脱敏的 base）。"""
    ok = all(bool(v.get("ok")) for v in layers.values())
    return {
        "ok": ok,
        "base": safe_text(base),
        "environment": environment,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "layers": layers,
        "timeline": (timeline or Timeline()).as_dict(),
    }


# ── HTTP（极薄，不带第三方依赖）───────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不把凭证转送到其他地址，也不把重定向登录页当成参谋页。
        return None


_HTTP = urllib.request.build_opener(_NoRedirect())


def _request(base: str, path: str, *, method: str = "GET", token: str | None = None,
             payload: dict | None = None, timeout: float = 30.0) -> tuple[int, str, str]:
    """返回 (status, body, content_type)。**异常不外抛**: 网络错误折成 status=0。"""
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json, text/html"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _HTTP.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, e.headers.get("Content-Type", "") if e.headers else ""
    except Exception as e:  # noqa: BLE001 - 连接失败也要落到报告里
        return 0, f"{type(e).__name__}", ""


def _json(body: str):
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None


def _api_data(body: str):
    envelope = _json(body)
    if isinstance(envelope, dict) and envelope.get("success") is True and envelope.get("code") == 0:
        return envelope.get("data")
    return None


def _api_object(body: str) -> dict:
    payload = _api_data(body)
    return payload if isinstance(payload, dict) else {}


def resolve_token(base: str, env: dict | None = None) -> tuple[str, str]:
    """token 来源：SIDA_PROBE_TOKEN > SIDA_PROBE_TOKEN_FILE > 用户名密码登录。

    返回 (token, source)。登录失败只报 HTTP 状态，**不回显响应体**（可能含细节）。
    """
    env = os.environ if env is None else env
    token = (env.get("SIDA_PROBE_TOKEN") or "").strip()
    if token:
        return token, "token:env"
    token_file = (env.get("SIDA_PROBE_TOKEN_FILE") or "").strip()
    if token_file:
        try:
            with open(token_file, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as e:
            raise ProbeConfigError(f"读不到 SIDA_PROBE_TOKEN_FILE: {e}") from e
        if token:
            return token, "token:file"
    username = (env.get("SIDA_PROBE_USERNAME") or "").strip()
    password = env.get("SIDA_PROBE_PASSWORD") or ""
    if not username or not password:
        raise ProbeConfigError(
            "缺少凭据：设置 SIDA_PROBE_TOKEN（或 SIDA_PROBE_TOKEN_FILE），"
            "或 SIDA_PROBE_USERNAME/SIDA_PROBE_PASSWORD 走登录"
        )
    status, body, _ = _request(
        base, "/api/auth/login", method="POST",
        payload={"username": username, "password": password}, timeout=20.0,
    )
    if status != 200:
        raise ProbeConfigError(f"登录失败 HTTP {status}（响应体不回显）")
    data = _api_object(body)
    token = (data.get("token") or "").strip()
    if not token:
        raise ProbeConfigError("登录响应里没有 token")
    return token, "login"


# ── 分层探测 ──────────────────────────────────────────────────────────────
def probe_public(base: str) -> dict:
    status, body, _ = _request(base, "/api/health", timeout=15.0)
    data = _api_object(body)
    if status != 200:
        return {"ok": False, "detail": f"GET /api/health -> {status}"}
    if data.get("status") != "ok":
        return {"ok": False, "detail": "健康检查无明显 status 字段"}
    shell_status, shell_body, ctype = _request(base, "/", timeout=15.0)
    shell_ok = shell_status == 200 and ("<div" in shell_body or "text/html" in ctype)
    return {
        "ok": shell_ok,
        "detail": f"health={data.get('status')} shell={shell_status}",
    }


def probe_auth(base: str, token: str, source: str) -> dict:
    status, body, _ = _request(base, "/api/auth/me", token=token, timeout=15.0)
    data = _api_object(body)
    ok = status == 200 and bool((data.get("user") or {}).get("id"))
    return {"ok": ok, "detail": f"me={status} source={source}"}


def probe_ui(base: str, token: str) -> dict:
    page_status, page_body, ctype = _request(base, "/ai-copilot", token=token, timeout=15.0)
    page_ok = page_status == 200 and ("<div" in page_body or "text/html" in ctype)
    api_status, api_body, _ = _request(base, "/api/copilot/presets", token=token, timeout=15.0)
    api_ok = api_status == 200 and isinstance(_api_data(api_body), list)
    return {
        "ok": page_ok and api_ok,
        "detail": f"page={page_status} presets={api_status}",
    }


def _bound_message(body: str, conversation_id: int, run_id: str) -> str:
    payload = _api_object(body)
    conv = payload.get("conversation")
    if not isinstance(conv, dict) or conv.get("id") != conversation_id:
        return ""
    return final_answer_text(payload.get("messages") or [], run_id=run_id)


def probe_qa(base: str, token: str, *, question: str, min_chars: int,
             timeout_s: float = 300.0, poll_s: float = 2.0) -> tuple[dict, Timeline]:
    """真问题 → 会话终稿。SSE 未参与判定：进度/心跳不是成功。"""
    tl = Timeline()
    tl.mark("submit")
    status, body, _ = _request(
        base, "/api/copilot/runs", method="POST", token=token,
        payload={"question": question}, timeout=20.0,
    )
    if status not in (200, 202):
        return {"ok": False, "detail": f"submit={status}", "reasons": [f"submit_http={status}"]}, tl
    run_id = ((_api_object(body)).get("run_id") or "").strip()
    if not run_id:
        return {"ok": False, "detail": "submit 未返回 run_id", "reasons": ["no_run_id"]}, tl

    deadline = time.monotonic() + timeout_s
    run: dict = {}
    steps: list = []
    conversation_id = None
    final_content = ""
    last_status = "queued"
    while time.monotonic() < deadline:
        st, rb, _ = _request(base, f"/api/copilot/runs/{quote(run_id, safe='')}", token=token, timeout=15.0)
        if st == 404:
            return {"ok": False, "detail": "run 详情 404", "reasons": ["run_not_found"]}, tl
        payload = _api_object(rb)
        run = payload.get("run") or {}
        if st != 200 or not isinstance(run, dict) or run.get("run_id") != run_id:
            return {"ok": False, "detail": "run 详情身份无法核验", "reasons": ["run_detail_unverified"]}, tl
        steps = payload.get("steps") or []
        last_status = (run.get("status") or "unknown").lower()

        if last_status != "queued" and not tl.has("dequeued"):
            tl.mark("dequeued")
        if steps and not tl.has("first_step"):
            tl.mark("first_step")
        cid = run.get("conversation_id")
        if type(cid) is int and cid > 0:
            conversation_id = cid
        if conversation_id and not tl.has("first_body"):
            cs, cb, _ = _request(base, f"/api/chat/conversations/{conversation_id}",
                                 token=token, timeout=15.0)
            if cs == 200:
                text = _bound_message(cb, conversation_id, run_id)
                if text:
                    tl.mark("first_body")
                    tl.marks["first_body_is_final"] = last_status in TERMINAL_STATUSES

        if last_status in TERMINAL_STATUSES:
            tl.mark("final")
            break
        time.sleep(poll_s)

    if not tl.has("final"):
        return {
            "ok": False,
            "detail": f"等待终态超时 status={last_status}",
            "reasons": [f"timeout_waiting_terminal:{last_status}"],
            "run_id": run_id,
        }, tl

    # 终稿只认会话最后一条 assistant 正文（不看 SSE、不看半截步骤文本）
    if conversation_id:
        cs, cb, _ = _request(base, f"/api/chat/conversations/{conversation_id}",
                             token=token, timeout=15.0)
        if cs == 200:
            final_content = _bound_message(cb, conversation_id, run_id)
    verdict = evaluate_probe(
        terminal_status=last_status, final_content=final_content, min_chars=min_chars,
    )
    # 降级分因：只报**观测到**的失败工具名（run 详情里没有 error 原文，
    # 探针不猜错误内容；工具 ok=false 是真实可观测的证据）。
    failed_tools = sorted({
        str(s.get("tool")) for s in steps
        if s and s.get("ok") is False and s.get("tool")
    })
    reasons = list(verdict["reasons"])
    if failed_tools:
        reasons.append("failed_tools=" + ",".join(failed_tools[:10]))
    return {
        "ok": verdict["ok"],
        "detail": f"run={last_status} answer_chars={verdict['final_chars']}",
        "reasons": reasons,
        "failed_tools": failed_tools,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "tool_calls": run.get("tool_calls"),
        "elapsed_ms": run.get("elapsed_ms"),
    }, tl


def run_probe(*, base: str, allow_production: bool, question: str,
              min_chars: int, timeout_s: float, poll_s: float,
              env: dict | None = None) -> dict:
    min_chars = validate_min_chars(min_chars)
    environment = ensure_allowed(base, allow_production=allow_production)
    layers: dict[str, dict] = {"public": probe_public(base)}
    if not layers["public"]["ok"]:
        return make_report(base=base, environment=environment, layers=layers)
    token, source = resolve_token(base, env)
    layers["auth"] = probe_auth(base, token, source)
    if not layers["auth"]["ok"]:
        return make_report(base=base, environment=environment, layers=layers)
    layers["ui"] = probe_ui(base, token)
    qa, timeline = probe_qa(
        base, token, question=question, min_chars=min_chars,
        timeout_s=timeout_s, poll_s=poll_s,
    )
    layers["qa"] = qa
    return make_report(base=base, environment=environment, layers=layers, timeline=timeline)


# ── 只读基线 ──────────────────────────────────────────────────────────────
def fetch_baseline(base: str, token: str, days: int) -> dict:
    """只读：GET /api/copilot/baseline?days=7|30（只统计本人窗口内已有 run）。"""
    if days not in (7, 30):
        raise ProbeConfigError("基线窗口仅支持 7 或 30 天")
    status, body, _ = _request(
        base, f"/api/copilot/baseline?days={days}", token=token, timeout=30.0,
    )
    if status != 200:
        raise ProbeConfigError(f"GET /api/copilot/baseline?days={days} -> {status}")
    result = _api_object(body)
    if result.get("days") != days:
        raise ProbeConfigError("基线返回结构或窗口不匹配")
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="数智参谋端到端 smoke + 7/30 天基线")
    p.add_argument("--base", default=DEFAULT_BASE, help="目标地址（默认本地 127.0.0.1:8000）")
    p.add_argument("--allow-production", action="store_true",
                   help="允许对生产发问（会写入一条真实问答记录）")
    p.add_argument("--question", default=DEFAULT_QUESTION)
    p.add_argument("--min-answer-chars", type=int, default=MIN_ANSWER_CHARS)
    p.add_argument("--timeout", type=float, default=300.0, help="等待 run 终态的秒数")
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--baseline", action="store_true", help="只读拉取 7/30 天基线")
    p.add_argument("--json", action="store_true", help="整份 JSON 输出")
    p.add_argument("--out", default="", help="把 JSON 报告写到文件")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        min_chars = validate_min_chars(args.min_answer_chars)
        environment = ensure_allowed(args.base, allow_production=args.allow_production)
        if args.baseline:
            token, _ = resolve_token(args.base)
            report = {
                "ok": True,
                "base": safe_text(args.base),
                "environment": environment,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "baseline": {str(d): fetch_baseline(args.base, token, d) for d in (7, 30)},
            }
        else:
            report = run_probe(
                base=args.base, allow_production=args.allow_production,
                question=args.question, min_chars=min_chars,
                timeout_s=args.timeout, poll_s=args.poll,
            )
    except ProbeConfigError as e:
        print(f"配置错误: {safe_text(str(e))}", file=sys.stderr)
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    if args.json:
        print(payload)
    else:
        print(f"{'OK' if report['ok'] else 'FAIL'} {report['environment']} {report['base']}")
        for name, layer in (report.get("layers") or {}).items():
            print(f"  {name:6s} {'ok' if layer.get('ok') else 'FAIL'}  {layer.get('detail', '')}")
        tl = report.get("timeline") or {}
        if tl:
            print(f"  queue={tl.get('queue_ms')}ms first_step={tl.get('fetch_to_first_step_ms')}ms "
                  f"body={tl.get('first_observed_body_ms')}ms total={tl.get('total_ms')}ms "
                  f"ttft_available={tl.get('ttft_available')}")
        if "baseline" in report:
            for days, data in report["baseline"].items():
                print(f"  {days}d runs={data.get('runs')} "
                      f"p50={data.get('latency', {}).get('p50_elapsed_ms')} "
                      f"p90={data.get('latency', {}).get('p90_elapsed_ms')}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
