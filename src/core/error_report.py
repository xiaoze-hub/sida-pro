"""对外错误文案的唯一出口: 安全文案 + 关联号; 细节只进服务端日志(脱敏)。

## 为什么需要这个模块

2026-11-12 复审发现两个端点把 `str(e)` 原样回给客户端:

- `GET /api/factors/calibration` 的 `error` 字段;
- `POST /api/recommendations/strategy-weights/rebalance` 的 500 `message`。

SQLAlchemy / psycopg 的异常文本经常自带
`SELECT ... FROM factor_weights WHERE ...`、表名/列名, 甚至
`postgresql://user:pass@host:5432/db` 这种完整 DSN。把它塞进 HTTP 响应, 等于
把库结构、连接串和凭证泄漏到浏览器、前端日志、截图和外网工单里。

## 约定

- **对外**: `public_error(action, ref)` —— 固定格式, 只含「动作 + 关联号」,
  不含异常文本、SQL、表名、DSN。调用方原样透传, 不要再拼接 `str(exc)`。
- **对内**: `log_failure(logger, action, exc_or_text, ref=..., context=...)` ——
  服务端日志里按 `[err:<ref>]` 输出类型 + traceback(脱敏后), 用户报障只报
  关联号即可定位。
- **脱敏**: DSN 的 `user:pass@`、`password=/token=/secret=/api_key=` 键值、
  `Bearer xxx` 一律替换为 `***`。日志同样不得留明文凭证。

## 边界

本模块只负责「怎么说」, 不负责「是否已回滚」。调用方在返回安全文案之前,
必须自己确保事务已回滚(`safe_rollback`)或本来就没有写入 —— 否则
`public_error` 里那句「本次未做任何改动」就成了新的谎话。
"""

from __future__ import annotations

import logging
import re
import traceback
import uuid

#: 单条日志里保留的最大字符数: 超长 SQL 参数列表既没阅读价值, 也会刷爆日志盘。
MAX_LOG_CHARS = 4000

#: `scheme://user:pass@host` → `scheme://***@host`
_DSN_CREDS_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^/\s@]+)@"
)
#: `password=xxx` / `token: xxx` / `api_key = xxx` 之类键值
_SECRET_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"access[_-]?token|private[_-]?key)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
#: `Authorization: Bearer xxx`
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{8,})")


def new_error_ref() -> str:
    """生成一个短关联号(日志与响应共用), 不含任何可推断的库内信息。"""
    return f"ERR-{uuid.uuid4().hex[:8].upper()}"


def redact(text: str, *, limit: int = MAX_LOG_CHARS) -> str:
    """抹掉文本里的凭证与连接串口令, 并截断到可读长度。

    只处理「凭证」这一件事: 表名/SQL 仍保留在服务端日志里 —— 那是排障需要的
    细节, 只是不该出现在 HTTP 响应里。
    """
    out = str(text or "")
    out = _DSN_CREDS_RE.sub(lambda m: f"{m.group('scheme')}***@", out)
    out = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}***", out)
    if len(out) > limit:
        out = out[:limit] + f"...(截断, 原长 {len(out)})"
    return out


def _detail_text(detail) -> str:
    """异常 → 完整 traceback; 其他 → str()。"""
    if isinstance(detail, BaseException):
        formatted = "".join(
            traceback.format_exception(type(detail), detail, detail.__traceback__)
        )
        return formatted or repr(detail)
    return str(detail or "")


def log_failure(
    logger: logging.Logger,
    action: str,
    detail,
    *,
    ref: str,
    context: str = "",
) -> None:
    """把失败细节写进服务端日志(脱敏), 前缀带关联号。

    级别固定 ERROR: 这些路径都是「用户看到一个失败」, 不是可忽略的降级分支。
    """
    ctx = f" context={context}" if context else ""
    try:
        logger.error(
            "[err:%s] %s失败%s: %s", ref, action, ctx, redact(_detail_text(detail))
        )
    except Exception:  # noqa: BLE001 - 日志失败绝不能顶掉主流程的错误路径
        pass


def public_error(action: str, ref: str) -> str:
    """给客户端的固定安全文案: 只有动作 + 关联号, 不含任何内部细节。

    `action` 用中文短动作名(如「标定计算」「策略调权」), 拼出来是
    `标定计算失败; 本次未做任何改动。错误参考号: ERR-XXXXXXXX(详情见服务端日志)`。
    """
    return (
        f"{action}失败; 本次未做任何改动。"
        f"错误参考号: {ref}(详情见服务端日志)"
    )


__all__ = [
    "MAX_LOG_CHARS",
    "log_failure",
    "new_error_ref",
    "public_error",
    "redact",
]
