# -*- coding: utf-8 -*-
"""对外错误文案 / 服务端失败日志的**白名单**口径(2026-11-12 复审二)。

上一版用正则去洗异常文本, 测试却拿 `***` 这种**已脱敏的占位串**当 secret ——
`assert "sup3r-secret" not in out` 对一个根本不含该串的输入自然永远通过, 等于
什么都没验。而且正则方案本身就漏: JSON 形态(`{"password": "x"}`)、复合键名
(`db_password=` / `aws_secret_access_key=`)、SQL 绑定参数
(`[parameters: ('x',)]`) 都洗不掉。

现在的验收线(全部用**真实的假 secret**, 且同时检查响应与日志):
1. 客户端只看到「动作失败 + 关联号」, 异常文本/SQL/表名/DSN/参数一律不在响应里;
2. 服务端日志只落 关联号 + 异常**类型 + 帧位置** + **白名单**上下文;
   异常消息里的 DSN/JSON/复合键名/绑定参数一个都不能出现;
3. 白名单外字段、形态不符的值只记 `dropped_fields=N`, 不落值;
4. 老调用传自由文本时只记长度, 不落正文;
5. 单条日志长度有上限。
"""

from __future__ import annotations

import logging

import pytest

from src.core.error_report import (
    MAX_LOG_CHARS,
    describe_exception,
    format_context,
    log_failure,
    new_error_ref,
    public_error,
)

# ── 真实的假 secret(不是 `***` 那种占位): 任何一处泄漏都能被断言抓到 ──────
DB_PASSWORD = "S1da-Pr0d-Pa55w0rd"
DSN = f"postgresql://sida:{DB_PASSWORD}@db.internal:5432/sida"
JSON_SECRET = "json-pw-7f3a91"
COMPOSITE_SECRET = "composite-pw-2b8c44"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
BOUND_PARAM = "622-CN-bound-param"
SQL = "SELECT weight FROM factor_weights WHERE market='CN' AND symbol='600519'"


def _logged(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# ── 1. 对外文案 ─────────────────────────────────────────────────────────
def test_public_error_has_action_and_ref_only():
    ref = new_error_ref()
    msg = public_error("标定计算", ref)
    assert "标定计算失败" in msg
    assert ref in msg
    assert "Traceback" not in msg
    assert "sqlite" not in msg.lower()
    for leak in (SQL, DSN, DB_PASSWORD, "factor_weights"):
        assert leak not in msg


def test_error_ref_is_short_and_unique():
    refs = {new_error_ref() for _ in range(50)}
    assert len(refs) == 50
    sample = next(iter(refs))
    assert sample.startswith("ERR-") and 8 <= len(sample) <= 16


# ── 2. 日志: 异常消息里的凭证/参数一律不落 ───────────────────────────────
def test_log_failure_never_writes_exception_text(caplog):
    """异常消息里塞满 DSN/JSON/复合键名/绑定参数 —— 日志里一个都不能有。

    只允许出现: 关联号、动作名、异常类型(+帧位置)、白名单上下文。
    """
    ref = new_error_ref()
    logger = logging.getLogger("tests.error_report.text")

    def _raise_with_secrets():
        # 真的抛一次, 才有 traceback: 类型 + 帧位置要能在日志里定位到代码位置。
        raise RuntimeError(
            f"{SQL} 失败; dsn={DSN}; "
            f'{{"password": "{JSON_SECRET}", "db_password": "{COMPOSITE_SECRET}"}}; '
            f"aws_secret_access_key={AWS_SECRET}; [parameters: ('{BOUND_PARAM}',)]"
        )

    exc = pytest.raises(RuntimeError, _raise_with_secrets).value
    with caplog.at_level(logging.ERROR, logger="tests.error_report.text"):
        log_failure(
            logger, "策略调权", exc, ref=ref,
            context={"window_days": 45, "market": "CN"},
        )

    text = _logged(caplog)
    assert ref in text and "策略调权" in text
    # 类型与帧位置保留(排障需要的代码位置, 不是数据)
    assert "RuntimeError" in text
    assert "test_error_report_redaction.py" in text
    # 白名单上下文保留
    assert "window_days=45" in text and "market=CN" in text
    # 正文/凭证/参数/表名: 一个都不许出现
    for leak in (
        DB_PASSWORD, DSN, JSON_SECRET, COMPOSITE_SECRET, AWS_SECRET,
        BOUND_PARAM, "factor_weights", "SELECT weight", "parameters",
    ):
        assert leak not in text, f"日志泄漏: {leak!r} in {text!r}"


def test_log_failure_drops_non_whitelisted_context_values(caplog):
    """白名单外字段(请求体/凭证)与形态不符的值: 只记数量, 不落值。"""
    ref = new_error_ref()
    logger = logging.getLogger("tests.error_report.ctx")
    with caplog.at_level(logging.ERROR, logger="tests.error_report.ctx"):
        log_failure(
            logger, "因子权重写入", ValueError("boom"), ref=ref,
            context={
                "factor_code": "alpha_score",
                "market": "CN",
                "db_password": COMPOSITE_SECRET,          # 非白名单键
                "request_body": f'{{"token": "{JSON_SECRET}"}}',  # 非白名单键
                "actor": f"{JSON_SECRET}/../etc",          # 白名单键但形态不符
            },
        )
    text = _logged(caplog)
    assert "factor_code=alpha_score" in text and "market=CN" in text
    assert "dropped_fields=3" in text, text
    for leak in (COMPOSITE_SECRET, JSON_SECRET):
        assert leak not in text, f"日志泄漏: {leak!r} in {text!r}"


def test_log_failure_free_text_detail_keeps_only_length(caplog):
    """老调用把 `str(e)` 当 detail 传进来: 只记长度, 不落正文。"""
    ref = new_error_ref()
    logger = logging.getLogger("tests.error_report.text2")
    with caplog.at_level(logging.ERROR, logger="tests.error_report.text2"):
        log_failure(logger, "因子标定", DSN, ref=ref)

    text = _logged(caplog)
    assert ref in text
    assert f"text_len={len(DSN)}" in text
    assert DB_PASSWORD not in text and "postgresql://" not in text


def test_log_failure_accepts_explicit_error_type(caplog):
    """异常已被别人吞成字符串时, 用 error_type 保住「是什么错」这条信息。"""
    ref = new_error_ref()
    logger = logging.getLogger("tests.error_report.type")
    with caplog.at_level(logging.ERROR, logger="tests.error_report.type"):
        log_failure(
            logger, "因子标定", None, ref=ref,
            error_type="sqlalchemy.exc.OperationalError",
        )
    text = _logged(caplog)
    assert "type=sqlalchemy.exc.OperationalError" in text
    assert ref in text


def test_log_failure_bounds_length(caplog):
    """上下文被塞爆时, 单条日志仍有上限(不刷爆日志盘)。"""
    ref = new_error_ref()
    logger = logging.getLogger("tests.error_report.len")
    with caplog.at_level(logging.ERROR, logger="tests.error_report.len"):
        log_failure(
            logger, "策略调权", RuntimeError("x" * 50_000), ref=ref,
            context={"actor": "u" * 64, "market": "CN"},
        )
    text = _logged(caplog)
    assert len(text) < MAX_LOG_CHARS + 200


# ── 3. 工具函数口径 ────────────────────────────────────────────────────
def test_describe_exception_has_type_and_frames_not_message():
    def _inner():
        raise RuntimeError(f"{SQL}; dsn={DSN}")

    exc = pytest.raises(RuntimeError, _inner).value
    desc = describe_exception(exc)
    assert "RuntimeError" in desc
    assert "test_error_report_redaction.py" in desc and "_inner" in desc
    assert DB_PASSWORD not in desc and "SELECT" not in desc


def test_format_context_rejects_free_text():
    """自由文本不解析: 只报「被忽略 + 长度」, 免得把 SQL 片段当上下文写进日志。"""
    out = format_context(f"{SQL} dsn={DSN}")
    assert out.startswith("<ignored free_text len=")
    assert "SELECT" not in out and DB_PASSWORD not in out
    assert format_context(object()) == "<ignored>"
    assert format_context(None) == ""
