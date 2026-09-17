"""运维告警体系 (2026-09-18 tier1-compliance)。

统一入口: `AlertManager` 负责格式化、去重、投递; 业务侧只调
`get_alert_manager().send(...)` 或各类型快捷函数(`alert_api_error_5xx` 等)。

设计要点:
- 渠道: 企业微信群机器人 webhook(`WECHAT_WEBHOOK_URL`); 未配置时只打日志
  (本地开发/CI 不会因缺 webhook 静默丢告警)。
- 去重: 同一 alert key 在 `ALERT_COOLDOWN_MINUTES`(默认 30)内只发一次,
  进程内 dict + Lock, 幂等且线程安全。
- 投递 fail-soft: 网络/序列化失败只记日志, 绝不反向影响业务请求。
- 5xx 突增 / LLM 429 风暴用滑动窗口计数, 避免单次故障刷屏。

环境变量:
- WECHAT_WEBHOOK_URL   企业微信 webhook 完整 URL(可选)
- ALERT_COOLDOWN_MINUTES  同 key 冷却分钟数(默认 30)
- ALERT_5XX_THRESHOLD     5 分钟窗口内 5xx 次数阈值(默认 10)
- ALERT_LLM_429_THRESHOLD 5 分钟窗口内 429 次数阈值(默认 5)
- ALERT_DISK_THRESHOLD_PCT 磁盘使用率告警阈值百分比(默认 85)
- ALERT_DS_FAIL_THRESHOLD  数据源连续失败次数阈值(默认 5)
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# 预置告警类型(与运维约定一致, key 稳定不可改)
ALERT_API_ERROR_5XX = "api_error_5xx"
ALERT_DISK_HIGH = "disk_high"
ALERT_LLM_RATE_LIMIT = "llm_rate_limit"
ALERT_DB_CONNECTION = "db_connection_error"
ALERT_DATA_SOURCE_DOWN = "data_source_down"
ALERT_BACKUP_FAILED = "backup_failed"

_LEVEL_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🔴",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except Exception:
        return default


def get_data_dir() -> str:
    """数据根目录(W2.2/E4: DATA_DIR 唯一口径; 本地开发回落仓库 data/)。"""
    env = (os.environ.get("DATA_DIR") or "").strip()
    if env:
        return env
    # src/core/alerting.py → repo root
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(root, "data")


@dataclass
class AlertMessage:
    key: str
    level: AlertLevel
    title: str
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def format_markdown(self) -> str:
        ts = datetime.fromtimestamp(self.created_at).strftime("%Y-%m-%d %H:%M:%S")
        emoji = _LEVEL_EMOJI.get(self.level, "")
        lines = [
            f"## {emoji} {self.title}",
            "",
            f"> **级别**: {self.level.value}",
            f"> **时间**: {ts}",
            f"> **Key**: `{self.key}`",
        ]
        if self.detail:
            lines += ["", "**详情**:", "", self.detail]
        if self.extra:
            lines += ["", "**附加**:"]
            for k, v in self.extra.items():
                lines.append(f"- {k}: `{v}`")
        return "\n".join(lines)


class AlertManager:
    """告警管理器: 去重 + 企业微信 markdown 卡片投递。"""

    def __init__(
        self,
        webhook_url: str | None = None,
        cooldown_minutes: float | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._webhook_url = (
            (webhook_url if webhook_url is not None else os.environ.get("WECHAT_WEBHOOK_URL") or "")
            .strip()
        )
        self._cooldown = (
            float(cooldown_minutes)
            if cooldown_minutes is not None
            else _env_float("ALERT_COOLDOWN_MINUTES", 30.0)
        )
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        # key → 上次发送 monotonic 时间戳
        self._last_sent: dict[str, float] = {}

    # ── 去重 ──
    def _should_send(self, key: str, now: float, force: bool) -> bool:
        if force:
            return True
        if self._cooldown <= 0:
            return True
        last = self._last_sent.get(key)
        if last is not None and (now - last) < self._cooldown * 60.0:
            return False
        return True

    def _mark_sent(self, key: str, now: float) -> None:
        self._last_sent[key] = now

    def reset_cooldown(self, key: str | None = None) -> None:
        """测试/运维用: 清冷却。key=None 清空全部。"""
        with self._lock:
            if key is None:
                self._last_sent.clear()
            else:
                self._last_sent.pop(key, None)

    # ── 发送 ──
    def send(
        self,
        key: str,
        level: AlertLevel | str,
        title: str,
        detail: str = "",
        *,
        extra: dict[str, Any] | None = None,
        force: bool = False,
    ) -> bool:
        """发送一条告警。返回 True=已投递(或已写日志), False=冷却去重跳过。"""
        try:
            lvl = level if isinstance(level, AlertLevel) else AlertLevel(str(level))
        except ValueError:
            lvl = AlertLevel.WARNING

        now = time.monotonic()
        with self._lock:
            if not self._should_send(key, now, force):
                logger.debug("[alerting] 冷却去重跳过: key=%s", key)
                return False
            self._mark_sent(key, now)

        msg = AlertMessage(
            key=key,
            level=lvl,
            title=title,
            detail=detail or "",
            extra=dict(extra or {}),
        )
        body = msg.format_markdown()

        # 未配 webhook: 只打日志(保证本地/CI 仍可观测)
        if not self._webhook_url:
            log_fn = logger.critical if lvl == AlertLevel.CRITICAL else (
                logger.warning if lvl == AlertLevel.WARNING else logger.info
            )
            log_fn("[alerting:%s] %s\n%s", key, title, body)
            return True

        self._post_webhook(body)
        return True

    def _post_webhook(self, markdown: str) -> None:
        """企业微信群机器人 markdown 卡片。失败只记日志。"""
        try:
            import httpx

            payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(self._webhook_url, json=payload)
                data = {}
                try:
                    data = resp.json()
                except Exception:
                    pass
                if data.get("errcode") not in (0, None) and resp.status_code != 200:
                    logger.warning(
                        "[alerting] 企业微信发送失败: status=%s errcode=%s errmsg=%s",
                        resp.status_code,
                        data.get("errcode"),
                        data.get("errmsg"),
                    )
                else:
                    logger.info("[alerting] 企业微信告警已发送")
        except Exception as e:  # noqa: BLE001 — 告警投递绝不反向拖垮业务
            logger.warning("[alerting] 企业微信发送异常(忽略): %s", e)

    # ── 便捷类型入口 ──
    def send_api_error_5xx(self, count: int, window_seconds: int, sample_paths: list[str] | None = None) -> bool:
        paths = ", ".join((sample_paths or [])[:5]) or "-"
        return self.send(
            ALERT_API_ERROR_5XX,
            AlertLevel.CRITICAL,
            "接口 5xx 突增",
            f"{window_seconds // 60} 分钟内出现 **{count}** 次 5xx 响应。",
            extra={"sample_paths": paths, "count": count},
        )

    def send_llm_rate_limit(self, scene: str, count: int, window_seconds: int) -> bool:
        return self.send(
            f"{ALERT_LLM_RATE_LIMIT}:{scene or 'default'}",
            AlertLevel.WARNING,
            "LLM 429 限流风暴",
            f"场景 `{scene or 'default'}` 在 {window_seconds // 60} 分钟内触发 **{count}** 次 429。",
            extra={"scene": scene or "default", "count": count},
        )

    def send_disk_high(self, path: str, used_pct: float, threshold_pct: float) -> bool:
        return self.send(
            ALERT_DISK_HIGH,
            AlertLevel.WARNING if used_pct < 95 else AlertLevel.CRITICAL,
            "磁盘使用率过高",
            f"目录 `{path}` 所在分区使用率 **{used_pct:.1f}%** (阈值 {threshold_pct:.0f}%)。",
            extra={"path": path, "used_pct": round(used_pct, 1)},
        )

    def send_db_connection_error(self, detail: str) -> bool:
        return self.send(
            ALERT_DB_CONNECTION,
            AlertLevel.CRITICAL,
            "数据库连接失败",
            detail or "健康检查无法连接数据库。",
        )

    def send_data_source_down(self, provider: str, fail_count: int, last_detail: str = "") -> bool:
        return self.send(
            f"{ALERT_DATA_SOURCE_DOWN}:{provider}",
            AlertLevel.CRITICAL,
            f"数据源不可用: {provider}",
            f"连续失败 **{fail_count}** 次。" + (f"\n最近错误: {last_detail[:300]}" if last_detail else ""),
            extra={"provider": provider, "fail_count": fail_count},
        )

    def send_backup_failed(self, detail: str) -> bool:
        return self.send(
            ALERT_BACKUP_FAILED,
            AlertLevel.CRITICAL,
            "数据库自动备份失败",
            detail or "备份任务失败, 请尽快人工介入。",
        )


# ── 进程级单例 ──
_manager_lock = threading.Lock()
_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AlertManager()
    return _manager


def set_alert_manager(manager: AlertManager | None) -> None:
    """测试注入/重置。传 None 恢复默认单例。"""
    global _manager
    with _manager_lock:
        _manager = manager


# ── 5xx 滑动窗口计数 ──
_5XX_WINDOW_SEC = 300.0
_5xx_lock = threading.Lock()
_5xx_events: deque[float] = deque()
_5xx_sample_paths: deque[str] = deque(maxlen=20)


def record_http_status(status: int, path: str = "") -> None:
    """由请求中间件调用: 记录 5xx, 超阈值触发 api_error_5xx。"""
    try:
        if status < 500:
            return
        threshold = _env_int("ALERT_5XX_THRESHOLD", 10)
        now = time.monotonic()
        with _5xx_lock:
            _5xx_events.append(now)
            if path:
                _5xx_sample_paths.append(path)
            cutoff = now - _5XX_WINDOW_SEC
            while _5xx_events and _5xx_events[0] < cutoff:
                _5xx_events.popleft()
            count = len(_5xx_events)
            samples = list(_5xx_sample_paths)
        if count > threshold:
            get_alert_manager().send_api_error_5xx(
                count=count,
                window_seconds=int(_5XX_WINDOW_SEC),
                sample_paths=samples,
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("[alerting] record_http_status 失败(忽略): %s", e)


def reset_5xx_window() -> None:
    """测试隔离用。"""
    with _5xx_lock:
        _5xx_events.clear()
        _5xx_sample_paths.clear()


# ── LLM 429 风暴窗口 ──
_LLM_429_WINDOW_SEC = 300.0
_llm_lock = threading.Lock()
_llm_429_events: dict[str, deque[float]] = defaultdict(deque)


def record_llm_429(scene: str = "") -> None:
    """由 AIClient 调用: 记录一次 429, 超阈值触发 llm_rate_limit。"""
    try:
        scene = (scene or "default").strip() or "default"
        threshold = _env_int("ALERT_LLM_429_THRESHOLD", 5)
        now = time.monotonic()
        with _llm_lock:
            q = _llm_429_events[scene]
            q.append(now)
            cutoff = now - _LLM_429_WINDOW_SEC
            while q and q[0] < cutoff:
                q.popleft()
            count = len(q)
        if count >= threshold:
            get_alert_manager().send_llm_rate_limit(
                scene=scene,
                count=count,
                window_seconds=int(_LLM_429_WINDOW_SEC),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("[alerting] record_llm_429 失败(忽略): %s", e)


def reset_llm_429_window() -> None:
    with _llm_lock:
        _llm_429_events.clear()


# ── 数据源连续失败 ──
_ds_lock = threading.Lock()
_ds_fail_streak: dict[str, int] = defaultdict(int)
_ds_last_detail: dict[str, str] = {}


def record_data_source_failure(provider: str, detail: str = "") -> None:
    """连续失败计数; 达阈值触发 data_source_down。"""
    try:
        provider = (provider or "unknown").strip() or "unknown"
        threshold = _env_int("ALERT_DS_FAIL_THRESHOLD", 5)
        with _ds_lock:
            _ds_fail_streak[provider] += 1
            count = _ds_fail_streak[provider]
            if detail:
                _ds_last_detail[provider] = str(detail)[:300]
            last = _ds_last_detail.get(provider, "")
        if count >= threshold:
            get_alert_manager().send_data_source_down(provider, count, last)
    except Exception as e:  # noqa: BLE001
        logger.debug("[alerting] record_data_source_failure 失败(忽略): %s", e)


def record_data_source_success(provider: str) -> None:
    """成功一次即清零连续失败计数(恢复后冷却仍由 AlertManager 控制)。"""
    try:
        provider = (provider or "unknown").strip() or "unknown"
        with _ds_lock:
            _ds_fail_streak.pop(provider, None)
            _ds_last_detail.pop(provider, None)
    except Exception:
        pass


def reset_data_source_streaks() -> None:
    with _ds_lock:
        _ds_fail_streak.clear()
        _ds_last_detail.clear()


def notify_db_connection_error(detail: str = "") -> bool:
    """数据库连接失败告警(健康检查/启动探测调用)。"""
    try:
        return get_alert_manager().send_db_connection_error(detail)
    except Exception as e:  # noqa: BLE001
        logger.debug("[alerting] db_connection 告警失败(忽略): %s", e)
        return False


# ── 磁盘使用率检查 ──
def disk_usage_percent(path: str | None = None) -> tuple[str, float]:
    """返回 (检查路径, 使用率百分比)。路径不存在时取其最近存在的父目录。"""
    target = path or get_data_dir()
    probe = os.path.abspath(target)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if not probe or not os.path.exists(probe):
        probe = os.path.abspath(os.sep) if os.name != "nt" else os.path.abspath("C:\\")
    usage = shutil.disk_usage(probe)
    pct = (usage.used / usage.total * 100.0) if usage.total else 0.0
    return probe, pct


def check_disk_usage(path: str | None = None, threshold_pct: float | None = None) -> dict[str, Any]:
    """检查 DATA_DIR 所在分区使用率; 超阈值发 disk_high 告警。

    Returns:
        {"path", "used_pct", "threshold_pct", "alerted"}
    """
    threshold = (
        float(threshold_pct)
        if threshold_pct is not None
        else _env_float("ALERT_DISK_THRESHOLD_PCT", 85.0)
    )
    probe, pct = disk_usage_percent(path)
    alerted = False
    if pct > threshold:
        alerted = get_alert_manager().send_disk_high(probe, pct, threshold)
    else:
        logger.debug("[alerting] 磁盘正常: %s used=%.1f%%", probe, pct)
    return {
        "path": probe,
        "used_pct": round(pct, 2),
        "threshold_pct": threshold,
        "alerted": alerted,
    }


def _disk_check_job() -> None:
    """APScheduler 同步入口。"""
    try:
        check_disk_usage()
    except Exception as e:  # noqa: BLE001
        logger.warning("[alerting] 磁盘检查任务异常: %s", e)


def register_disk_check_job(scheduler=None):
    """注册每小时磁盘检查 job。

    Args:
        scheduler: 已有 APScheduler 实例则复用; 否则新建 BackgroundScheduler。
    Returns:
        配置好的 scheduler(不自动 start, 由调用方控生命周期)。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    try:
        from src.core.timezone import _get_app_tz

        tz = _get_app_tz()
    except Exception:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")

    sched = scheduler if scheduler is not None else BackgroundScheduler(timezone=tz)
    sched.add_job(
        _disk_check_job,
        IntervalTrigger(hours=1, timezone=tz),
        id="alerting_disk_check",
        name="磁盘使用率检查",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.info("[alerting] 已注册每小时磁盘使用率检查 job")
    return sched
