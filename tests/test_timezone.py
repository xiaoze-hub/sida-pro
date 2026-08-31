"""tests for src/core/timezone.py"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.core.timezone import (
    format_app_tz,
    format_beijing,
    to_beijing,
    to_iso_utc,
    to_iso_with_tz,
    to_utc,
)

# 方言感知口径(2026-08-24):
# - SQLite(默认, 无 SIDA_DB_URL)的 func.now() 存 UTC naive
# - PG 的 func.now() 存 app 时区(北京) naive
PG_URL = "postgresql+psycopg2://u:p@127.0.0.1:5432/sida"


class TestToUtc:
    def test_aware_datetime(self):
        """转 UTC — 带时区的 datetime 正确转换"""
        dt = datetime(2024, 1, 15, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        result = to_utc(dt)
        assert result.tzinfo == timezone.utc
        assert result.hour == 2  # Shanghai is UTC+8

    def test_naive_sqlite_as_utc(self, monkeypatch):
        """SQLite(默认)下 naive 视为 UTC"""
        monkeypatch.delenv("SIDA_DB_URL", raising=False)
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2024, 1, 15, 10, 0)
        result = to_utc(dt)
        assert result.tzinfo == timezone.utc
        assert result.hour == 10  # UTC naive → UTC 不变

    def test_naive_pg_as_app_tz(self, monkeypatch):
        """PG 下 naive 视为 app 时区(北京)"""
        monkeypatch.setenv("SIDA_DB_URL", PG_URL)
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2024, 1, 15, 10, 0)
        result = to_utc(dt)
        assert result.tzinfo == timezone.utc
        assert result.hour == 2  # 北京 10:00 → UTC 2:00


class TestToBeijing:
    def test_utc_to_beijing(self, monkeypatch):
        """转北京时间 — UTC 02:00 → 10:00"""
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2024, 1, 15, 2, 0, tzinfo=timezone.utc)
        result = to_beijing(dt)
        assert result.hour == 10

    def test_naive_sqlite_as_utc(self, monkeypatch):
        """SQLite(默认)下 naive 视为 UTC"""
        monkeypatch.delenv("SIDA_DB_URL", raising=False)
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2024, 1, 15, 2, 0)
        result = to_beijing(dt)
        assert result.hour == 10  # UTC naive 2:00 → 北京 10:00

    def test_naive_pg_as_app_tz(self, monkeypatch):
        """PG 下 naive 视为 app 时区(北京)本地时间"""
        monkeypatch.setenv("SIDA_DB_URL", PG_URL)
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2024, 1, 15, 2, 0)
        result = to_beijing(dt)
        assert result.hour == 2  # 北京 naive 2:00 → 北京 2:00


class TestFormatBeijing:
    def test_default_format(self, monkeypatch):
        """格式化 — 默认格式 YYYY-MM-DD HH:MM:SS"""
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2024, 1, 15, 2, 30, 0, tzinfo=timezone.utc)
        result = format_beijing(dt)
        assert result == "2024-01-15 10:30:00"

    def test_custom_format(self, monkeypatch):
        """格式化 — 自定义格式 HH:MM"""
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2024, 1, 15, 2, 0, 0, tzinfo=timezone.utc)
        result = format_beijing(dt, fmt="%H:%M")
        assert result == "10:00"


class TestToIsoUtc:
    def test_utc_input(self):
        """ISO UTC — UTC 输入带 Z 后缀"""
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert to_iso_utc(dt) == "2024-01-15T10:30:00Z"

    def test_non_utc_input(self):
        """ISO UTC — 非 UTC 输入自动转换"""
        dt = datetime(2024, 1, 15, 18, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert to_iso_utc(dt) == "2024-01-15T10:30:00Z"


class TestToIsoWithTz:
    def test_aware(self):
        """ISO 带时区 — 保留原始时区偏移"""
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = to_iso_with_tz(dt)
        assert "10:30:00" in result
        assert "+00:00" in result

    def test_naive_sqlite_gets_utc(self, monkeypatch):
        """SQLite(默认)下 naive 视为 UTC → +00:00"""
        monkeypatch.delenv("SIDA_DB_URL", raising=False)
        dt = datetime(2024, 1, 15, 10, 30, 0)
        result = to_iso_with_tz(dt)
        assert "+00:00" in result

    def test_naive_pg_gets_app_tz(self, monkeypatch):
        """PG 下 naive 视为 app 时区(北京) → +08:00"""
        monkeypatch.setenv("SIDA_DB_URL", PG_URL)
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2024, 1, 15, 10, 30, 0)
        result = to_iso_with_tz(dt)
        assert "+08:00" in result
        assert "10:30:00" in result


class TestFormatAppTz:
    def test_naive_pg_as_app_tz(self, monkeypatch):
        """PG 下 naive 按 app 时区(北京)解读 → 输出 +08:00"""
        monkeypatch.setenv("SIDA_DB_URL", PG_URL)
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2026, 8, 24, 9, 27, 0)
        result = format_app_tz(dt)
        assert result.startswith("2026-08-24T09:27:00")
        assert "+08:00" in result

    def test_naive_sqlite_as_utc(self, monkeypatch):
        """SQLite 存 UTC naive(01:27) → 转北京输出 09:27+08:00"""
        monkeypatch.delenv("SIDA_DB_URL", raising=False)
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        dt = datetime(2026, 8, 24, 1, 27, 0)  # SQLite 的 func.now() 存 UTC naive
        result = format_app_tz(dt)
        assert result.startswith("2026-08-24T09:27:00")  # UTC 1:27 → 北京 9:27
        assert "+08:00" in result

    def test_none_returns_empty(self):
        assert format_app_tz(None) == ""
