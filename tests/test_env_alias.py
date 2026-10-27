"""品牌改名兼容层的行为钉子（2026-10-27 PANWATCH_* → SIDA_*）。

为什么需要这组测试：
生产容器的 env 由部署脚本写入，改名与镜像发布无法原子完成——中间必然存在
「代码已经读 SIDA_*、容器里还是 PANWATCH_*」的窗口。这组用例钉死那个窗口
里读到的值必须正确，否则线上表现是"配置全空、静默回落默认值"，最难查。
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def fresh_alias(monkeypatch):
    """重置模块内的 _applied 标志，让每个用例都能独立触发一次别名。"""
    from src.core import env_alias

    monkeypatch.setattr(env_alias, "_applied", False, raising=False)
    importlib.reload(env_alias)
    return env_alias


def test_legacy_prefix_is_mirrored_to_new(fresh_alias, monkeypatch):
    """只设置旧名 PANWATCH_X 时，SIDA_X 必须读到同一个值。"""
    monkeypatch.delenv("SIDA_DEMO_KEY", raising=False)
    monkeypatch.setenv("PANWATCH_DEMO_KEY", "legacy-value")

    fresh_alias.alias_legacy_env()

    import os

    assert os.environ.get("SIDA_DEMO_KEY") == "legacy-value"


def test_explicit_new_prefix_wins(fresh_alias, monkeypatch):
    """两侧都设置了：新名优先，绝不被旧值覆盖。"""
    monkeypatch.setenv("SIDA_DEMO_KEY2", "new-value")
    monkeypatch.setenv("PANWATCH_DEMO_KEY2", "old-value")

    fresh_alias.alias_legacy_env()

    import os

    assert os.environ.get("SIDA_DEMO_KEY2") == "new-value"


def test_env_helper_reads_new_prefix(fresh_alias, monkeypatch):
    """env("X") 读的是 SIDA_X，而不是任何别的拼法。"""
    monkeypatch.setenv("SIDA_HELPER_DEMO", "42")
    assert fresh_alias.env("HELPER_DEMO") == "42"
    assert fresh_alias.env("HELPER_MISSING", "fallback") == "fallback"


def test_alias_is_idempotent(monkeypatch):
    """重复调用不改变任何已有值（进程内多处 import 都会触发）。"""
    from src.core import env_alias

    monkeypatch.setattr(env_alias, "_applied", False, raising=False)
    monkeypatch.setenv("PANWATCH_IDEMPOTENT_DEMO", "a")
    env_alias.alias_legacy_env()
    env_alias.alias_legacy_env()

    import os

    assert os.environ.get("SIDA_IDEMPOTENT_DEMO") == "a"


def test_alias_never_writes_back_to_legacy(fresh_alias, monkeypatch):
    """只进不出：新名绝不回写旧名。

    曾经的坑：反向映射会把 `SIDA_DB_URL` 写成 `PANWATCH_DB_URL` 留在 os.environ，
    子进程继承后兼容层再把它映射回 `SIDA_DB_URL` —— 测试里已经 delenv 的变量
    在子进程「复活」，隔离失效（2026-10-27 CI 红在 test_conftest_isolation）。
    """
    import os

    monkeypatch.delenv("PANWATCH_ONEWAY_DEMO", raising=False)
    monkeypatch.setenv("SIDA_ONEWAY_DEMO", "only-new")

    fresh_alias.alias_legacy_env()

    assert os.environ.get("SIDA_ONEWAY_DEMO") == "only-new"
    assert os.environ.get("PANWATCH_ONEWAY_DEMO") is None, (
        "新前缀被回写成了旧前缀 —— 子进程会把已删除的变量复活"
    )
