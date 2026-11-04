"""时区处理工具 - 统一时间存储和显示。

默认时区可通过环境变量覆盖：
- TZ（推荐）

未设置时默认 Asia/Shanghai。

naive 时间口径（方言感知，2026-08-24 修复 +8 偏移）：
- SQLite 的 CURRENT_TIMESTAMP(func.now()) 存【UTC naive】
- PG(Asia/Shanghai) 的 now() 写入 timestamp without time zone 存【北京 naive】
- 所有 DB 列读出的 naive 时间一律走 _db_naive_tz() 判断口径，避免 +8/-8 偏移。
"""

from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo


def _get_app_tz() -> ZoneInfo:
    tz_name = os.environ.get("TZ") or os.environ.get("APP_TIMEZONE") or "Asia/Shanghai"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _db_naive_tz():
    """DB 列(server_default=func.now())的 naive 时间口径。

    SQLite 的 CURRENT_TIMESTAMP = UTC naive；
    PG(Asia/Shanghai) 的 now() 写入 timestamp without time zone = app 时区(北京) naive。
    据此判断 DB 读出的 naive 时间应按哪个时区解读。

    为什么不直接调 `dialect.declared_backend()`: 那个模块在 import 时就把方言判定
    结果缓存住了, 单测/热重载里改环境变量后不会跟着变（会静默给出反向口径）。这里按同一套
    环境变量**即时**解析，优先级与 `src/db/dialect.py` 保持一致：
    `DATABASE_URL_WRITE` 优先, 其次 `SIDA_DB_URL`, 都没有 = SQLite。
    （只嗅 SIDA_DB_URL 是不够的: 只配 DATABASE_URL_WRITE 的部署会被误判成 SQLite,
    写库时刻整体偏 8 小时且没有任何报错。）
    """
    url = (
        os.environ.get("DATABASE_URL_WRITE")
        or os.environ.get("SIDA_DB_URL")
        or ""
    ).strip()
    return _get_app_tz() if url.startswith("postgresql") else timezone.utc


def utc_now() -> datetime:
    """获取当前 UTC 时间（带时区信息）"""
    return datetime.now(timezone.utc)


def beijing_now() -> datetime:
    """获取当前默认时区时间（历史命名保留；带时区信息）"""
    return datetime.now(_get_app_tz())


def beijing_now_naive() -> datetime:
    """获取当前默认时区(北京)时间, 去除时区信息(naive)。

    统一存储口径: PG 的 timestamp without time zone 列 + func.now() 在
    Asia/Shanghai 时区下存的是【北京 naive 时间】。所有写库/比较的"当前时间"
    都应走本函数, 避免 datetime.utcnow() 混入 UTC naive 造成 8 小时口径割裂。
    """
    return datetime.now(_get_app_tz()).replace(tzinfo=None)


def db_now_naive() -> datetime:
    """与 DB 的 ``func.now()`` **同口径**的"当前时间"（naive）。

    - PG(TZ=Asia/Shanghai): ``now()`` 写 timestamp 列 → 北京 naive
    - SQLite: ``CURRENT_TIMESTAMP`` → UTC naive

    凡是"写进库"或"与库里的列比较"的现在, 都用它而不是 ``datetime.utcnow()`` /
    ``beijing_now_naive()``: 这样 Python 写入的值和 ``server_default`` 写入的值在
    两种方言下都落在同一个时钟上（SQLite 本地开发库也不会因为 +8 而和存量行割裂）。
    只用于展示/跨系统传输的绝对时刻请用 ``utc_now()`` / ``to_iso_utc()``。
    """
    return datetime.now(_db_naive_tz()).replace(tzinfo=None)


def to_utc(dt: datetime) -> datetime:
    """将时间转换为 UTC。naive 时间按 DB 方言解读(SQLite=UTC, PG=北京)。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_db_naive_tz())
    return dt.astimezone(timezone.utc)


def db_naive_from_utc(dt: datetime) -> datetime:
    """把绝对时刻转成 **DB 列同口径**的 naive 时间。

    `db_now_naive()` 的反向操作: 已知一个 UTC 时刻(如"UTC 自然日 00:00"),
    要拿去和 `created_at`(PG=北京 naive / SQLite=UTC naive)比较时用它。
    配额结算按 UTC 自然日, 而列可能是北京 naive —— 直接拿 UTC 零点去比会差 8 小时。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_db_naive_tz()).replace(tzinfo=None)


def to_beijing(dt: datetime) -> datetime:
    """将时间转换为默认时区（历史命名保留）。

    naive 时间按 DB 方言解读(SQLite=UTC, PG=北京)后转 app 时区。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_db_naive_tz())
    return dt.astimezone(_get_app_tz())


def format_beijing(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """格式化为默认时区字符串（历史命名保留）"""
    return to_beijing(dt).strftime(fmt)


def to_iso_utc(dt: datetime) -> str:
    """转换为 ISO 格式的 UTC 时间字符串（带 Z 后缀）"""
    utc_dt = to_utc(dt)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso_with_tz(dt: datetime) -> str:
    """转换为 ISO 格式字符串（带时区偏移）。naive 按 DB 方言解读。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_db_naive_tz())
    return dt.isoformat()


def format_app_tz(dt: datetime | None, tz_name: str | None = None) -> str:
    """把 DB 存储的 naive 时间格式化为 ISO 字符串（带时区偏移）。

    naive 时间按 DB 方言解读(SQLite=UTC, PG=北京), 再按目标时区
    (默认 app 时区)输出带偏移的 ISO。
    """
    if dt is None:
        return ""
    app_tz = _get_app_tz()
    try:
        target = ZoneInfo(tz_name) if tz_name else app_tz
    except Exception:
        target = app_tz
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_db_naive_tz())
    return dt.astimezone(target).isoformat()
