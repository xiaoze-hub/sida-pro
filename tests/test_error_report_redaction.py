# -*- coding: utf-8 -*-
"""对外错误文案 / 日志脱敏的最小验收(2026-11-12 复审)。

红线:
- 客户端只看到「动作失败 + 关联号」, 看不到异常文本、SQL、表名、DSN;
- 服务端日志保留排障细节, 但**抹掉凭证**(DSN 里的 user:pass、password=/token=
  等键值、Bearer token), 且不无限长。
"""

from __future__ import annotations

import logging

from src.core.error_report import (
    MAX_LOG_CHARS,
    log_failure,
    new_error_ref,
    public_error,
    redact,
)


def test_public_error_has_action_and_ref_only():
    ref = new_error_ref()
    msg = public_error("标定计算", ref)
    assert "标定计算失败" in msg
    assert ref in msg
    assert "Traceback" not in msg
    assert "sqlite" not in msg.lower()


def test_error_ref_is_short_and_unique():
    refs = {new_error_ref() for _ in range(50)}
    assert len(refs) == 50
    sample = next(iter(refs))
    assert sample.startswith("ERR-") and 8 <= len(sample) <= 16


def test_redact_removes_dsn_credentials():
    text = "OperationalError: postgresql://sida:sup3r-secret@db.internal:5432/sida"
    out = redact(text)
    assert "sup3r-secret" not in out
    assert "postgresql://***@db.internal" in out
    # 表名/主机保留: 那是排障需要的细节, 只是不该出现在 HTTP 响应里。
    assert "db.internal" in out


def test_redact_removes_secret_key_values_and_bearer():
    text = (
        'password=Hunter2 token: abcdef123456 api_key="k-999" '
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload"
    )
    out = redact(text)
    for secret in ("Hunter2", "abcdef123456", "k-999", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in out, f"凭证未脱敏: {secret}"


def test_redact_truncates_long_text():
    out = redact("x" * (MAX_LOG_CHARS + 500))
    assert len(out) < MAX_LOG_CHARS + 200
    assert "截断" in out


def test_log_failure_keeps_detail_but_redacts(caplog):
    ref = new_error_ref()
    logger = logging.getLogger("tests.error_report")
    with caplog.at_level(logging.ERROR, logger="tests.error_report"):
        log_failure(
            logger, "策略调权",
            RuntimeError("SELECT * FROM factor_weights; dsn=postgresql://u:p@h/db"),
            ref=ref, context="window_days=45",
        )
    text = caplog.text
    assert ref in text
    assert "策略调权" in text
    assert "factor_weights" in text, "排障细节应保留在服务端日志"
    assert "u:p@h" not in text, "日志里不得留明文 DSN 口令"
