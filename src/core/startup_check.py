"""启动配置自检模块(2026-08-21)。

背景: 生产环境(腾讯云 101.35.244.238, docker 容器 panwatch)近期暴露配置问题:
- 部署时丢 SIDA_DB_URL 环境变量 → 静默回退 SQLite 跑了 4 天。

本模块在应用启动时(init_db 之后)对关键配置做一次显式自检:
- level 为 warning/error 的结果打印醒目横幅(前后各一行 `=`)告警;
- level 为 error 的结果额外尝试写一条站内 Notification(category='system', level='error')。

设计原则:
- fail-fast 告警而非 fail-stop: 任何检查异常都用 try/except 兜住, 绝不阻塞启动;
- 每个检查独立, 单个失败不影响其它检查;
- 可被动 monkeypatch 相关模块级引用以方便单测。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from src.web.database import IS_PG

logger = logging.getLogger(__name__)

_BANNER = "=" * 70


@dataclass
class CheckResult:
    """单个自检项结果。level ∈ {ok, info, warning, error}。"""

    name: str
    level: str
    message: str


def _check_db_dialect() -> CheckResult:
    """检查 1: 数据库方言。IS_PG=False(SQLite) → warning; True → info。"""
    if IS_PG:
        return CheckResult(
            "database.dialect",
            "info",
            "数据库方言: PostgreSQL(生产推荐)。",
        )
    return CheckResult(
        "database.dialect",
        "warning",
        "数据库方言: SQLite(单文件, 并发写性能差)。生产环境建议配置 PostgreSQL。",
    )


def _check_db_url_explicit() -> CheckResult:
    """检查 2: SIDA_DB_URL 是否显式配置。缺失(走默认 SQLite 回退) → warning + 醒目横幅。"""
    if os.environ.get("SIDA_DB_URL"):
        return CheckResult(
            "database.url",
            "info",
            "SIDA_DB_URL 已显式配置。",
        )
    return CheckResult(
        "database.url",
        "warning",
        "\u26a0\ufe0f 未检测到 SIDA_DB_URL, 正在使用 SQLite 回退。"
        "部署时请务必显式设置 SIDA_DB_URL, 否则会静默丢失 PG 数据。",
    )


def _check_thsdk() -> CheckResult:
    """检查 3: thsdk 账户。无 THS_USERNAME → info 提示游客模式。"""
    if os.environ.get("THS_USERNAME"):
        return CheckResult(
            "thsdk",
            "info",
            "thsdk: 正式账户已配置(THS_USERNAME)。",
        )
    return CheckResult(
        "thsdk",
        "info",
        "thsdk 游客模式(未配置 THS_USERNAME)。",
    )


def _check_jwt_secret() -> CheckResult:
    """检查 5: JWT_SECRET。既无 env 也无法从 DB 读取/生成时 → error。"""
    if os.environ.get("JWT_SECRET"):
        return CheckResult(
            "jwt_secret",
            "ok",
            "JWT_SECRET 已通过环境变量配置。",
        )
    try:
        from src.web.api.auth import get_jwt_secret

        get_jwt_secret()
        return CheckResult(
            "jwt_secret",
            "info",
            "JWT_SECRET 已从数据库读取/自动生成。",
        )
    except Exception as e:  # noqa: BLE001 - 自检绝不抛出
        return CheckResult(
            "jwt_secret",
            "error",
            f"JWT_SECRET 既无环境变量也无法从数据库读取: {e!r}",
        )


def _check_data_dir_writable() -> CheckResult:
    """检查 6: DATA_DIR 可写性。不可写 → error。"""
    data_dir = os.environ.get("DATA_DIR", "./data")
    try:
        os.makedirs(data_dir, exist_ok=True)
        probe = os.path.join(data_dir, ".startup_check_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return CheckResult(
            "data_dir",
            "ok",
            f"数据目录可写: {data_dir}",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "data_dir",
            "error",
            f"数据目录不可写: {data_dir} ({e!r})",
        )


def _check_zhitu_token() -> CheckResult:
    """检查 8: ZHITU_TOKEN 配置(P0-2, 2026-08-23 审计)。

    之前 quotes.py 公司简介接口硬编码 fallback UUID, 导致任何 fork 仓库的部署
    都会用同一个公开 token 调智兔 API。改为必须显式配置; 缺失时启动告警。
    """
    # 池化/DB 优先 (与 quotes.py 同等优先级)
    token_pool = ""
    try:
        from marketdata.vendors.zhitu import pick_zhitu_token
        token_pool = pick_zhitu_token() or ""
    except Exception:
        pass
    if token_pool:
        return CheckResult(
            "zhitu_token",
            "ok",
            "ZHITU_TOKEN 池化已配置(多 key)。",
        )
    try:
        from src.web.database import SessionLocal
        from src.web.models import AppSettings
        db = SessionLocal()
        row = db.query(AppSettings).filter(AppSettings.key == "zhitu_token").first()
        db.close()
        if row and row.value and row.value != "********":
            return CheckResult(
                "zhitu_token",
                "ok",
                "ZHITU_TOKEN 已从数据库 AppSettings 配置。",
            )
    except Exception:
        pass

    env_token = os.environ.get("ZHITU_TOKEN", "")
    if env_token:
        return CheckResult(
            "zhitu_token",
            "ok",
            "ZHITU_TOKEN 已通过环境变量配置。",
        )
    return CheckResult(
        "zhitu_token",
        "warning",
        "⚠️ ZHITU_TOKEN 未配置(也无池化/DB 设置), 公司简介接口将返回空。"
        "请设置环境变量 ZHITU_TOKEN 或在设置页/zhitu 池化中配置。"
        "P0-2 (2026-08-23 审计): 之前源码硬编码的 UUID fallback 已删除, 必须显式配置。",
    )


def _check_notify_channels() -> CheckResult:
    """检查 7: 通知渠道。notify_channels enabled 数量为 0 → warning。"""
    from src.web.database import SessionLocal
    from src.web.models import NotifyChannel

    db = SessionLocal()
    try:
        count = db.query(NotifyChannel).filter(NotifyChannel.enabled == True).count()  # noqa: E712
    finally:
        db.close()
    if count > 0:
        return CheckResult(
            "notify_channels",
            "ok",
            f"可用通知渠道: {count} 个。",
        )
    return CheckResult(
        "notify_channels",
        "warning",
        "无可用通知渠道, 告警将不可达。请在通知设置中至少启用一个渠道。",
    )


# 自检项注册表(顺序即输出顺序)
_CHECKS: list[tuple[str, Callable[[], CheckResult]]] = [
    ("database.dialect", _check_db_dialect),
    ("database.url", _check_db_url_explicit),
    ("thsdk", _check_thsdk),
    ("jwt_secret", _check_jwt_secret),
    ("data_dir", _check_data_dir_writable),
    ("zhitu_token", _check_zhitu_token),
    ("notify_channels", _check_notify_channels),
]


def _render_results(results: list[CheckResult]) -> None:
    """按级别输出: warning/error 打醒目横幅; error 额外尝试发站内 Notification。"""
    for r in results:
        level = (r.level or "info").lower()
        if level in ("warning", "error"):
            logger.warning(
                "\n%s\n[启动自检 %s] %s: %s\n%s",
                _BANNER,
                level.upper(),
                r.name,
                r.message,
                _BANNER,
            )
        elif level == "ok":
            logger.info("[启动自检 OK] %s: %s", r.name, r.message)
        else:
            logger.info("[启动自检 INFO] %s: %s", r.name, r.message)

        if level == "error":
            # 延迟解析模块全局名: 保证测试 monkeypatch sc._push_error_notification 可替换
            globals()["_push_error_notification"](r)


def _push_error_notification(r: CheckResult) -> None:
    """错误级别额外尝试发一条 Notification(category='system', level='error')。

    借用 notify_center.push_notification(绝不抛异常)完成站内落库 + 外发。
    此处再包一层 try/except, 保证任何失败都不影响启动。
    """
    try:
        from src.core.notify_center import push_notification

        push_notification(
            title=f"[启动自检] {r.name}",
            body=r.message,
            category="system",
            level="error",
            source="startup_check",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[启动自检] 发送错误通知失败(不阻断启动): %s", e)


def run_startup_checks() -> list[CheckResult]:
    """执行全部启动自检, 返回 CheckResult 列表。

    任何单个检查异常都会被捕获并降级为一条 warning 日志, 绝不抛出。
    """
    results: list[CheckResult] = []
    # 延迟解析: 从模块 globals 取函数, 保证测试 monkeypatch sc._check_xxx 可生效
    for name, fn in _CHECKS:
        try:
            fn_resolved = globals().get(fn.__name__, fn)
            results.append(fn_resolved())
        except Exception as e:  # noqa: BLE001 - 自检绝不阻塞启动
            logger.warning("[启动自检] 检查 %s 异常(跳过, 不阻断启动): %s", name, e)
    _render_results(results)
    return results
