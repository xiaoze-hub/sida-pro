"""resolve_ths_creds: 设置页 DB > env(2026-09-05, v0.5.5)。"""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mock_thsdk_module():
    fake = MagicMock()
    fake.THS = MagicMock()
    fake.THSResponse = MagicMock()
    sys.modules["thsdk"] = fake
    yield
    sys.modules.pop("thsdk", None)


def test_db_priority_over_env(monkeypatch):
    import data_source.thsdk_l2 as M

    M._CRED_CACHE.update(ts=0.0, username="", password="")
    monkeypatch.setattr(M, "_db_ths_creds", lambda: ("mx_db", "pw_db"))
    monkeypatch.setenv("THS_USERNAME", "mx_env")
    monkeypatch.setenv("THS_PASSWORD", "pw_env")
    u, p, src = M.resolve_ths_creds()
    assert (u, p, src) == ("mx_db", "pw_db", "db")


def test_env_fallback_when_db_empty(monkeypatch):
    import data_source.thsdk_l2 as M

    M._CRED_CACHE.update(ts=0.0, username="", password="")
    monkeypatch.setattr(M, "_db_ths_creds", lambda: ("", ""))
    monkeypatch.setenv("THS_USERNAME", "mx_env")
    monkeypatch.setenv("THS_PASSWORD", "pw_env")
    u, p, src = M.resolve_ths_creds()
    assert (u, p, src) == ("mx_env", "pw_env", "env")


def test_none_when_nothing(monkeypatch):
    import data_source.thsdk_l2 as M

    M._CRED_CACHE.update(ts=0.0, username="", password="")
    monkeypatch.setattr(M, "_db_ths_creds", lambda: ("", ""))
    monkeypatch.delenv("THS_USERNAME", raising=False)
    monkeypatch.delenv("THS_PASSWORD", raising=False)
    u, p, src = M.resolve_ths_creds()
    assert (u, p, src) == ("", "", "none")


def test_cache_ttl(monkeypatch):
    """30s 内不重复读 DB。"""
    import data_source.thsdk_l2 as M

    calls = []
    M._CRED_CACHE.update(ts=0.0, username="", password="")

    def _fake_db():
        calls.append(1)
        return ("mx_db", "pw_db")

    monkeypatch.setattr(M, "_db_ths_creds", _fake_db)
    M.resolve_ths_creds()
    M.resolve_ths_creds()
    assert len(calls) == 1


def test_explicit_arg_wins(monkeypatch):
    """显式参数优先级最高。"""
    import data_source.thsdk_l2 as M

    M._CRED_CACHE.update(ts=0.0, username="", password="")
    monkeypatch.setattr(M, "_db_ths_creds", lambda: ("mx_db", "pw_db"))
    cli = M.THSDKL2(username="mx_arg", password="pw_arg")
    assert cli.username == "mx_arg" and cli.password == "pw_arg"
    assert not cli._is_guest
