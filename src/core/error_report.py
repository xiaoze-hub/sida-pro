"""对外错误文案的唯一出口: 安全文案 + 关联号; 细节按**白名单**记录, 永不落原文。

## 为什么需要这个模块

2026-11-12 复审发现两个端点把 `str(e)` 原样回给客户端:

- `GET /api/factors/calibration` 的 `error` 字段;
- `POST /api/recommendations/strategy-weights/rebalance` 的 500 `message`。

SQLAlchemy / psycopg 的异常文本经常自带
`SELECT ... FROM factor_weights WHERE ...`、表名/列名,
`[parameters: ('622', 'CN', ...)]` 这种**绑定的 SQL 参数**, 甚至
`postgresql://user:pw@host:5432/db` 这种完整 DSN。塞进 HTTP 响应或日志,
等于把库结构、参数和凭证泄漏到浏览器、日志盘、截图和外网工单里。

## 约定(2026-11-12 复审二: 不靠正则兜底)

第一版用正则去「洗」异常文本 —— 那是在跟 DSN/JSON/复合键名的变体赛跑, 注定漏:

- `{"password": "xxx"}` (JSON 形态, 引号隔开) 洗不掉;
- `DB_PASSWORD=xxx` / `aws_secret_access_key=xxx` (复合键名, 无 ``\\b`` 边界) 洗不掉;
- `[parameters: ('secret',)]` (SQL 绑定参数) 压根不在键值形态里。

所以现在的口径是**不给原文进入日志的机会**, 而不是事后擦:

- **对外**: `public_error(action, ref)` —— 固定格式, 只含「动作 + 关联号」,
  不含异常文本、SQL、表名、DSN、参数。调用方原样透传, 不要再拼 `str(exc)`。
- **对内**: `log_failure(logger, action, detail, ref=..., context={...})` 只记录
  三样东西: ①关联号 ②**异常类型 + 帧位置**(文件名:行号:函数名, 不含源码行、
  局部变量、异常文本) ③**白名单字段**上下文。其余一概丢弃。
- **上下文白名单**: 只有 `SAFE_CONTEXT_KEYS` 里的字段会被写进日志, 且值必须符合
  短标识符/数字形态(`_SAFE_VALUE_RE`); 不在白名单或形态不符的字段只记录
  `dropped_fields=N`, 不落值。请求体、SQL、异常参数永远不进这个白名单。
- **不知道类型就不写内容**: 传入的若是自由文本(老调用), 只记录字符数, 不落正文。

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
from collections.abc import Mapping

#: 单条日志里保留的最大字符数。白名单之后正常远小于此值, 这里只防上下文被
#: 塞进上千个字段把日志盘刷爆。
MAX_LOG_CHARS = 4000

#: 允许写进日志的上下文字段**白名单**。加字段必须确认它是「有界的枚举/编号」;
#: 用户输入、SQL、异常参数、请求体一律不在其中。
SAFE_CONTEXT_KEYS = frozenset(
    {
        "actor",
        "actor_kind",
        "alpha",
        "days",
        "endpoint",
        "factor_code",
        "horizon",
        "horizons",
        "limit",
        "market",
        "min_samples",
        "regime",
        "role",
        "snapshot_days",
        "strategy_code",
        "via",
        "window_days",
    }
)

#: 白名单字段允许的**值形态**: 短标识符 / 数字 / 日期时间 / 点分版本。
#: 特意不含空格、引号、`=`、`@`、`/`、`,` —— 这些是 DSN、JSON、SQL 参数的特征字符。
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")

#: 异常类型名(含模块前缀)允许的形态。
_SAFE_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")

#: traceback 里最多保留的帧数: 够定位调用链, 又不至于把日志写成一屏堆栈。
_MAX_FRAMES = 6


def new_error_ref() -> str:
    """生成一个短关联号(日志与响应共用), 不含任何可推断的库内信息。"""
    return f"ERR-{uuid.uuid4().hex[:8].upper()}"


def _safe_value(value) -> str | None:
    """把上下文取值收敛成白名单形态; 形态不符返回 None(调用方记为已丢弃)。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, str):
        text = value.strip()
        return text if _SAFE_VALUE_RE.match(text) else None
    return None


def format_context(context) -> str:
    """把上下文渲染成 `k=v k=v dropped_fields=N`; 非白名单/坏形态一律不落值。

    - dict/Mapping: 只保留白名单键 + 合法形态值;
    - 自由文本(老调用传拼好的字符串): 只报字符数 —— 那串里可能就是 SQL/参数;
    - 其它类型: 记为 `<ignored>`。
    """
    if not context:
        return ""
    if isinstance(context, str):
        return f"<ignored free_text len={len(context)}>"
    if not isinstance(context, Mapping):
        return "<ignored>"

    kept: list[str] = []
    dropped = 0
    for key, value in context.items():
        name = str(key)
        if name not in SAFE_CONTEXT_KEYS:
            dropped += 1
            continue
        safe = _safe_value(value)
        if safe is None:
            dropped += 1
            continue
        kept.append(f"{name}={safe}")
    if dropped:
        kept.append(f"dropped_fields={dropped}")
    return " ".join(kept)


def _safe_type_name(name) -> str | None:
    text = str(name or "").strip()
    return text if _SAFE_TYPE_RE.match(text) else None


def describe_exception(exc: BaseException) -> str:
    """异常**类型 + 帧位置**, 不含异常文本/SQL 参数/局部变量。

    返回形如
    `sqlalchemy.exc.OperationalError frames=strategy_engine.py:2260:rebalance <- ...`。
    帧信息只取 (文件名 basename, 行号, 函数名) —— 那不是数据, 是代码位置;
    用来定位就够, 泄漏不了绑定参数或连接串。
    """
    type_name = _safe_type_name(f"{type(exc).__module__}.{type(exc).__name__}")
    if type_name is None:
        type_name = "<unknown>"
    frames: list[str] = []
    try:
        for frame in traceback.extract_tb(exc.__traceback__)[-_MAX_FRAMES:]:
            filename = str(frame.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
            func = str(frame.name or "")
            if not _SAFE_VALUE_RE.match(filename or "") or not _SAFE_VALUE_RE.match(func or ""):
                continue
            frames.append(f"{filename}:{int(frame.lineno or 0)}:{func}")
    except Exception:  # noqa: BLE001 - 取帧失败不能顶掉错误路径
        frames = []
    return f"{type_name} frames={' <- '.join(frames)}" if frames else type_name


def log_failure(
    logger: logging.Logger,
    action: str,
    detail=None,
    *,
    ref: str,
    context=None,
    error_type: str | None = None,
) -> None:
    """把失败写进服务端日志: **关联号 + 异常类型 + 帧位置 + 白名单上下文**。

    - `detail` 是异常对象 → 只取类型与帧位置; 是自由文本/其它 → 只记长度,
      **不落正文**(正文里可能就是 SQL 参数或 DSN);
    - `error_type` 用于「异常已被别人吞成字符串」的调用方(如 factor_eval 返回
      `error_type`), 显式给出类型名, 免得日志里只剩 `type=<unavailable>`;
    - 级别固定 ERROR: 这些路径都是「用户看到一个失败」, 不是可忽略的降级分支。
    """
    parts = [f"[err:{ref}] {action}失败"]
    if error_type:
        safe_type = _safe_type_name(error_type) or "<unknown>"
        parts.append(f"type={safe_type}")
    elif isinstance(detail, BaseException):
        parts.append(f"type={describe_exception(detail)}")
    elif detail is not None:
        # 不落正文: 只留下「有这段文本」这个事实, 便于判断是不是空异常。
        parts.append(f"type=<unavailable> text_len={len(str(detail))}")

    ctx = format_context(context)
    if ctx:
        parts.append(f"context={ctx}")

    text = " | ".join(parts)
    if len(text) > MAX_LOG_CHARS:
        text = text[:MAX_LOG_CHARS] + f"...(截断, 原长 {len(text)})"
    try:
        # 预格式化后再交给 logging: 异常文本里的 `%` 不会二次插值。
        logger.error("%s", text)
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
    "SAFE_CONTEXT_KEYS",
    "describe_exception",
    "format_context",
    "log_failure",
    "new_error_ref",
    "public_error",
]
