"""参谋端到端 smoke 探针的判据（2026-11-21）。

探针最容易自欺的三处，这里逐条钉死：
1. SSE 的 status/progress/ping 只说明「还在跑」，**不是成功**；
2. 半截正文（或工具进度文本）不是终稿，必须拿到会话里最后一条 assistant 正文；
3. 首字时间没有事实源时不许编：只能记 `first_observed_body_ms`，并标
   `ttft_available=false`，绝不把非流式结束时间叫 TTFT。

另外两条运行纪律：
- 默认只允许本地 / staging 发问；生产必须显式 `--allow-production`；
- 日志与输出里不出现 token / 密码 / 带凭据的 URL。
"""

from __future__ import annotations

import pytest

from scripts import copilot_smoke as probe


# ── 成功判据 ──────────────────────────────────────────────────────────────
def test_progress_only_is_not_success():
    # 跑了但没终稿: 只有 SSE 进度/半截
    verdict = probe.evaluate_probe(
        terminal_status="running", final_content=None, min_chars=40,
    )
    assert verdict["ok"] is False
    assert "terminal_status=running" in verdict["reasons"]


def test_degraded_run_is_not_success_even_with_answer():
    verdict = probe.evaluate_probe(
        terminal_status="degraded",
        final_content="上证指数今日收于 3xxx 点。" * 5,
        min_chars=40,
    )
    assert verdict["ok"] is False
    assert any("degraded" in r for r in verdict["reasons"])


def test_ok_with_real_final_answer_is_success():
    answer = "盘面上，算力与半导体领涨，成交额较昨日放大。" * 4
    verdict = probe.evaluate_probe(
        terminal_status="ok", final_content=answer, min_chars=40,
    )
    assert verdict["ok"] is True
    assert verdict["reasons"] == []
    assert verdict["final_chars"] == len(answer)


def test_half_answer_and_placeholder_are_failures():
    short = probe.evaluate_probe(terminal_status="ok", final_content="好的", min_chars=40)
    assert short["ok"] is False
    assert "final_answer_missing_or_too_short" in short["reasons"]

    placeholder = probe.evaluate_probe(
        terminal_status="ok", final_content="任务未能完成，请查看本轮过程。",
        min_chars=5,
    )
    assert placeholder["ok"] is False
    assert "final_answer_is_error_placeholder" in placeholder["reasons"]


def test_last_assistant_message_is_the_final_answer():
    messages = [
        {"role": "user", "content": "今天主线是什么"},
        {"role": "assistant", "content": "（工具调用中）"},
        {"role": "assistant", "content": "主线是算力，其次是半导体。"},
    ]
    assert probe.final_answer_text(messages) == "主线是算力，其次是半导体。"


def test_direct_read_refusal_is_not_a_successful_answer():
    """直查层取数失败的拒绝文案必须判失败（否则数据源故障算成功问答）。"""
    refuse = ("【机构持仓直查】取数失败：数据库查询未完成（这是取数错误，"
              "不代表该机构没有持仓）。请稍后重试。\n\n"
              "站内同口径页面：[打开国家队动向](https://x/national-team)")
    verdict = probe.evaluate_probe(terminal_status="ok", final_content=refuse, min_chars=40)
    assert verdict["ok"] is False
    assert "final_answer_is_error_placeholder" in verdict["reasons"]


# ── 目标与凭据 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("base", [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "https://staging.sida.example.com",
])
def test_local_and_staging_allowed_by_default(base):
    assert probe.classify_endpoint(base) in {"local", "staging"}


def test_production_requires_explicit_enable():
    base = "https://sida.hengsheng-elec.com"
    assert probe.classify_endpoint(base) == "production"
    with pytest.raises(probe.ProbeConfigError):
        probe.ensure_allowed(base, allow_production=False)
    probe.ensure_allowed(base, allow_production=True)  # 显式开启才放行


def test_unknown_remote_endpoint_is_treated_as_production():
    """判不准的远端主机不放行 —— 默认拒绝而不是猜。"""
    assert probe.classify_endpoint("https://some-other-host.example.com") == "production"


def test_credentials_never_echoed():
    token = "eyJhbGciOiJIUzI1NiJ9.secret-payload.sig"
    assert token not in probe.safe_text(f"Authorization: Bearer {token}")
    assert "secret-payload" not in probe.redact_url(
        "postgresql://user:supersecret@db.example.com:5432/sida"
    )
    assert "supersecret" not in probe.redact_url(
        "postgresql://user:supersecret@db.example.com:5432/sida"
    )
    assert probe.mask_secret(token).startswith("eyJh")
    assert token not in probe.mask_secret(token)


# ── 时间线: 只报观测到的东西 ──────────────────────────────────────────────
def test_timeline_only_reports_observed_marks_and_never_ttft():
    tl = probe.Timeline()
    tl.mark("submit", t=1000.0)
    tl.mark("dequeued", t=1002.0)
    tl.mark("first_step", t=1003.0)
    tl.mark("final", t=1010.0)
    out = tl.as_dict()

    assert out["queue_ms"] == 2000
    assert out["fetch_to_first_step_ms"] == 1000
    assert out["total_ms"] == 10000
    assert out["first_observed_body_ms"] is None
    assert out["ttft_available"] is False
    # 绝不出现 ttft_ms 这类冒充字段
    assert "ttft_ms" not in out


def test_timeline_missing_marks_are_none_not_zero():
    tl = probe.Timeline()
    tl.mark("submit", t=50.0)
    tl.mark("final", t=52.0)
    out = tl.as_dict()

    assert out["queue_ms"] is None
    assert out["fetch_to_first_step_ms"] is None
    assert out["total_ms"] == 2000


def test_probe_payload_is_json_safe_and_has_layer_results():
    result = probe.make_report(
        base="http://127.0.0.1:8000",
        environment="local",
        layers={
            "public": {"ok": True, "detail": "GET /api/health 200"},
            "auth": {"ok": True, "detail": "token"},
            "ui": {"ok": True, "detail": "presets 200"},
            "qa": {"ok": False, "detail": "terminal_status=running", "reasons": ["terminal_status=running"]},
        },
        timeline=probe.Timeline(),
    )
    assert result["ok"] is False
    assert [k for k in ("public", "auth", "ui", "qa")] == list(result["layers"])
    assert result["environment"] == "local"
    assert result["timestamp"]


def test_min_answer_chars_cannot_be_zeroed():
    """把门槛设成 0 等于让空回答过验收, 探针自己拒绝这种配置。"""
    with pytest.raises(probe.ProbeConfigError):
        probe.validate_min_chars(0)
    assert probe.validate_min_chars(40) == 40


# ── 凭据来源: 只认 token, 不留口令路径 (2026-11-21 复审) ───────────────────
def test_password_login_path_is_removed(monkeypatch):
    """没有 token 就是配置错误，不许退化成「用户名+密码登录」。

    口令登录要过验证码/限流，探针自己走这条路径只会制造假失败，还会把口令
    带进一个更宽的入口；凭据只从 env / 文件来。
    """
    calls = []
    monkeypatch.setattr(probe, "_request", lambda *a, **k: calls.append((a, k)) or (200, "{}", ""))

    with pytest.raises(probe.ProbeConfigError) as ei:
        probe.resolve_token(
            "http://127.0.0.1:8000",
            {"SIDA_PROBE_USERNAME": "someone", "SIDA_PROBE_PASSWORD": "pw"},
        )

    assert calls == [], "口令路径必须彻底移除：不得再向 /api/auth/login 发请求"
    assert "SIDA_PROBE_TOKEN" in str(ei.value)


def test_token_file_is_still_supported(tmp_path):
    f = tmp_path / "tok"
    f.write_text("  tok-from-file\n", encoding="utf-8")
    token, source = probe.resolve_token(
        "http://127.0.0.1:8000", {"SIDA_PROBE_TOKEN_FILE": str(f)},
    )
    assert token == "tok-from-file"
    assert source == "token:file"


# ── 口径: 只读 vs 会写 / 超时略高于服务端 ─────────────────────────────────
def test_default_timeout_is_just_above_server_run_timeout():
    """探针比服务端先放弃 = 把「服务端还在跑」误判成超时。"""
    from src.web.api.copilot_jobs import RUN_TIMEOUT_S

    assert probe.DEFAULT_TIMEOUT_S > RUN_TIMEOUT_S
    assert probe.DEFAULT_TIMEOUT_S - RUN_TIMEOUT_S <= 120, "略高于即可, 不是无限等"


def test_reports_declare_read_only_vs_write():
    """读者必须能一眼看出这次跑会不会写一条真实问答记录。"""
    probe_report = probe.make_report(
        base="http://127.0.0.1:8000", environment="local",
        layers={"public": {"ok": True, "detail": "health=ok"}},
    )
    assert probe_report["mode"] == "probe"
    assert probe_report["read_only"] is False
    assert probe_report["writes_run"] is True

    base_report = probe.make_baseline_report(
        base="http://127.0.0.1:8000", environment="local",
        baseline={"7": {"days": 7, "runs": 0}},
    )
    assert base_report["mode"] == "baseline"
    assert base_report["read_only"] is True
    assert base_report["writes_run"] is False
