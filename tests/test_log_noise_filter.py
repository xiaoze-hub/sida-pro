"""传输层日志噪音过滤: 控制台与数据库**两个** handler 都必须挡掉 httpx/httpcore/thsdk 的低级别噪音。

背景(2026-09-14 生产实测): `_ConsoleNoiseFilter` 原先**只挂在控制台**, DB handler 恒 DEBUG
全量收录 ⇒ 开盘时每条 `httpx/httpcore/thsdk` 的 DEBUG 都被格式化并写进日志表, 把 app CPU 打到
122%、Postgres 打到 50-95%, 并伴随 `too many clients already`。本文件守三件事:

  ① 噪音库的 INFO/DEBUG **不进** handler(WARNING+ 必须仍然进 —— 真实错误不能因降噪而丢失);
  ② 业务/Agent 自身的 DEBUG **照旧进**(「错误日志」页签依赖它);
  ③ `setup_logging` 确实把该 filter 挂到了 **DB** handler 上(不只控制台) —— 这一条最要紧,
     因为原来漏的就是它; 只守 filter 本身会让"忘了挂"这种回归悄悄溜过。
"""
from __future__ import annotations

import logging

import pytest

from src.bootstrap.env import _TransportNoiseFilter, setup_logging


def _rec(name: str, level: int) -> logging.LogRecord:
    return logging.LogRecord(name=name, level=level, pathname=__file__, lineno=1,
                             msg="x", args=(), exc_info=None)


NOISY = ["httpx", "httpcore", "httpcore.http11", "thsdk.base", "urllib3.connectionpool",
         "hpack.hpack", "apscheduler.executors.default", "uvicorn.access"]


@pytest.mark.parametrize("name", NOISY)
def test_noisy_lib_low_level_is_dropped(name: str) -> None:
    f = _TransportNoiseFilter()
    assert f.filter(_rec(name, logging.DEBUG)) is False, f"{name} DEBUG 应被丢弃"
    assert f.filter(_rec(name, logging.INFO)) is False, f"{name} INFO 应被丢弃"


@pytest.mark.parametrize("name", NOISY)
def test_noisy_lib_warning_and_above_still_passes(name: str) -> None:
    """降噪绝不能把真实错误一起丢掉 —— thsdk 的 -6 请求超时是 ERROR 级, 必须留痕。"""
    f = _TransportNoiseFilter()
    assert f.filter(_rec(name, logging.WARNING)) is True
    assert f.filter(_rec(name, logging.ERROR)) is True
    assert f.filter(_rec(name, logging.CRITICAL)) is True


@pytest.mark.parametrize("name", ["src.web.api.stocks", "src.agents.intraday_monitor",
                                  "src.core.tdx_boards", "__main__"])
def test_business_debug_still_passes(name: str) -> None:
    """业务/Agent 的 DEBUG 必须照旧进日志板, 否则「错误日志」页签会变哑。"""
    f = _TransportNoiseFilter()
    assert f.filter(_rec(name, logging.DEBUG)) is True
    assert f.filter(_rec(name, logging.INFO)) is True


def test_prefix_match_is_label_aware() -> None:
    """前缀匹配要按"标签段"判定, 不能把 `httpx_utils` 这种同前缀的业务模块误伤。"""
    f = _TransportNoiseFilter()
    assert f.filter(_rec("httpx", logging.DEBUG)) is False
    assert f.filter(_rec("httpx._client", logging.DEBUG)) is False
    # 同名前缀但不是该包(没有点分隔) ⇒ 属业务模块, 应放行
    assert f.filter(_rec("httpx_utils", logging.DEBUG)) is True


def test_setup_logging_attaches_filter_to_DB_handler() -> None:
    """**本文件最要紧的一条**: DB handler 必须挂上噪音过滤器。

    原先 DB handler 没挂任何 filter, 这是 CPU/Postgres 被打满的直接原因。只测 filter 类本身
    无法发现"忘了挂", 故这里直接断言两个 handler 上都存在该 filter。
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    try:
        setup_logging()
        from src.web.log_handler import DBLogHandler

        db_handlers = [h for h in root.handlers if isinstance(h, DBLogHandler)]
        assert db_handlers, "setup_logging 应挂上 DBLogHandler"
        for h in db_handlers:
            assert any(isinstance(f, _TransportNoiseFilter) for f in h.filters), (
                "DB handler 必须挂 _TransportNoiseFilter —— 否则开盘日志噪音会再次打满 CPU/PG"
            )

        console_handlers = [h for h in root.handlers if getattr(h, "_panwatch_console", False)]
        assert console_handlers, "setup_logging 应挂上控制台 handler"
        for h in console_handlers:
            assert any(isinstance(f, _TransportNoiseFilter) for f in h.filters), (
                "控制台 handler 也必须挂该过滤器"
            )
    finally:
        # setup_logging 会替换 handler, 用完还原, 避免污染同进程其它用例。
        for h in list(root.handlers):
            if h not in saved:
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        root.handlers[:] = saved
        root.setLevel(saved_level)
