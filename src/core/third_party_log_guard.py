"""第三方库日志的 **logger 层**安全处理: 只留「安全类别 + 关联号」, 不留原始正文。

## 问题(2026-11-12 复审六)

`apprise` 自己会打 LOG 级别的正文, 里面有渠道凭据:

    apprise|DEBUG|Loaded Bark URL: bark://host/****?image=yes...
    apprise|DEBUG|Bark Payload: {'device_key': '<真实 device_key>', ...}
    apprise|DEBUG|Bark POST URL: http://host/push (cert_verify=True, encrypted=False)
    apprise|ERROR|Unparseable Telegram URL tgram://B...Z/123/
    apprise|WARNING|A Connection error occurred sending Bark notification to ****.

`apprise` 不在 `_TransportNoiseFilter` 的噪音名单里, 所以这些 **DEBUG 会原样写进
DB 日志表**(`log_entries`)和控制台 —— 落盘、可导出、可在「错误日志」页签里被任何
管理员账号看到, 令牌等于进了另一个数据库。apprise 的插件名(NotifyBark/NotifyTelegram)
和输入 URL 决定了正文不可控: 换渠道、换版本都可能多打一行。

## 为什么不是"提高日志级别"

调 `logging.getLogger("apprise").setLevel(WARNING)` 只是**少打**一部分, 不是"安全":

- WARNING/ERROR 档位里同样有 URL 与令牌(上面的 `sending ... to ****` 就在 WARNING);
- 第三方将来升版把凭据打到 INFO, 策略会静默失效;
- 级别过滤把「出错了」这个事实也一起丢了, 排障时只剩一句"没日志"。

所以这里在 **LogRecord 层**动手: 记录照常产生(级别、来源、时间都在), 但**正文被
整体替换**成 `[apprise] suppressed level=WARNING category=apprise ref=ERR-XXXXXXXX`。
没有任何原文进入 handler, 控制台 / DB / Loki **任何**下游 handler 都拿不到令牌,
也不需要给每个 handler 挂 filter(新增 handler 不会漏)。

## 关联号怎么来

`third_party_log_scope(ref=..., category=...)` 在**一次外发**期间绑定关联号, 于是
apprise 这一路的日志行与 `push_error` 里的 `参考号: ERR-XXXXXXXX` 对得上 —— 运维
能定位"是这次发送的哪一段炸了", 但看不到 URL/令牌/回包。作用域外(插件加载等)
记录仍然被替换, 关联号写 `-`。

## 覆盖范围

- `apprise`(含全部子 logger): 渠道凭据/URL 的主要泄漏源;
- `httpx` / `httpcore` / `urllib3`: 它们的 INFO/DEBUG 里有完整请求 URL
  (`HTTP Request: POST https://qyapi.weixin.qq.com/...?key=<webhook key>`), 我们自己的
  自定义渠道也走 httpx —— 同样换成安全行。WARNING+ 也一样替换(正文可能含 URL)。

不在名单里的第三方库不受影响: 本模块只做"这些 logger 的正文不许落盘", 不改级别、
不改其它库的行为。
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar

#: 正文必须被整体替换的第三方 logger 前缀。加新前缀前先确认: 它的日志正文里
#: **不需要**任何原文来排障(级别/来源/关联号已足够)。
GUARDED_PREFIXES: tuple[str, ...] = ("apprise", "httpx", "httpcore", "urllib3")

#: 允许作为 `category=` 落日志的形态(有界短标识符)。
_SAFE_CATEGORY_MAX = 32

_scope_var: ContextVar[tuple[str, str] | None] = ContextVar(
    "third_party_log_scope", default=None
)

_install_lock = threading.Lock()
_installed = False


def _is_guarded(name: object) -> str | None:
    """命中前缀则返回该前缀(作为安全类别), 否则 None。"""
    text = str(name or "")
    for prefix in GUARDED_PREFIXES:
        if text == prefix or text.startswith(prefix + "."):
            return prefix
    return None


def _safe_token(value: object, *, default: str = "-") -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64:
        return default
    if not all(ch.isalnum() or ch in "-_." for ch in text):
        return default
    return text


@contextmanager
def third_party_log_scope(*, ref: str = "", category: str = ""):
    """在一次外发/一次风险调用期间绑定关联号与安全类别。"""
    token = _scope_var.set(
        (
            _safe_token(ref, default="-"),
            _safe_token(category, default="")[: _SAFE_CATEGORY_MAX],
        )
    )
    try:
        yield
    finally:
        _scope_var.reset(token)


def _sanitize_record(record: logging.LogRecord, prefix: str) -> None:
    """把 record 的**正文/参数/异常/堆栈**换成安全摘要(就地, 无副产物)。"""
    scope = _scope_var.get()
    ref = scope[0] if scope else "-"
    category = (scope[1] if scope and scope[1] else prefix) or prefix
    level = _safe_token(record.levelname, default="LOG")
    # category 只留前缀本身: 渠道类型由调用方通过 scope 传(有界枚举), 不取 record 名,
    # 因为子 logger 名里可能带插件类名与用户配置的片段。
    safe = f"[{prefix}] suppressed level={level} category={category} ref={ref}"
    record.msg = safe
    record.args = ()
    # 异常堆栈是最容易被忽略的泄漏面: httpx/apprise 的异常文本带完整 URL。
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None


def install_third_party_log_guard() -> None:
    """安装全局 LogRecord 工厂包装(幂等)。

    用 record factory 而不是 `logger.addFilter`: filter 只作用于**该 logger 自身**
    产生的记录, 子 logger(`apprise.plugins.NotifyBark`)与第三方自己 new 出来的
    logger 照样漏; 而 factory 在**任何** handler/任何 logger 之前执行, 天然覆盖全链路
    (含控制台、DBLogHandler、Loki, 以及未来新增的 handler)。
    """
    global _installed
    with _install_lock:
        if _installed:
            return
        old_factory = logging.getLogRecordFactory()

        def record_factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            prefix = _is_guarded(getattr(record, "name", ""))
            if prefix:
                try:
                    _sanitize_record(record, prefix)
                except Exception:  # noqa: BLE001 - 日志路径绝不能抛
                    record.msg = f"[{prefix}] suppressed"
                    record.args = ()
                    record.exc_info = None
            return record

        logging.setLogRecordFactory(record_factory)
        _installed = True


__all__ = [
    "GUARDED_PREFIXES",
    "install_third_party_log_guard",
    "third_party_log_scope",
]
