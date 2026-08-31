"""启动配置自检模块测试。"""
import pytest

# 触发 src.web.models 在 Base.metadata 上注册全表 —— conftest 的 session 级
# init_db() 只在模型已被注册时才建表(否则孤立跑本文件会 no such table)。
import src.web.models  # noqa: F401
import src.core.startup_check as sc
from src.core.startup_check import CheckResult, run_startup_checks


@pytest.fixture()
def _base_env(monkeypatch, tmp_path):
    """默认环境: 常规配置, 供单测按需覆盖。"""
    monkeypatch.setenv("SIDA_DB_URL", "postgresql://u:p@h:5432/sida")
    monkeypatch.setenv("THS_USERNAME", "ths_user")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return sc


def _levels(results):
    return {r.name: r.level for r in results}


def test_missing_sida_db_url_produces_warning(monkeypatch, tmp_path):
    """SIDA_DB_URL 缺失(走默认 SQLite 回退) → 产生 warning。"""
    monkeypatch.delenv("SIDA_DB_URL", raising=False)
    monkeypatch.setenv("THS_USERNAME", "ths_user")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    results = run_startup_checks()
    levels = _levels(results)
    assert levels["database.url"] == "warning"
    # 提示 SQLite 回退
    assert any(r.name == "database.url" and "SQLite" in r.message for r in results)


def test_is_pg_false_produces_warning(monkeypatch, tmp_path):
    """数据库方言非 PG(SQLite) → warning。"""
    # IS_PG 是 src.web.database 的模块常量, 在 startup_check 里以模块全局引用, 可 monkeypatch
    monkeypatch.setattr(sc, "IS_PG", False)
    monkeypatch.setenv("SIDA_DB_URL", "sqlite:////tmp/x.db")
    monkeypatch.setenv("THS_USERNAME", "ths_user")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    results = run_startup_checks()
    assert _levels(results)["database.dialect"] == "warning"


def test_all_normal_has_no_warning(monkeypatch, tmp_path):
    """全部检查项正常(token/渠道/目录等) → 无 warning / error。"""
    monkeypatch.setattr(sc, "IS_PG", True)
    monkeypatch.setenv("SIDA_DB_URL", "postgresql://u:p@h:5432/sida")
    monkeypatch.setenv("THS_USERNAME", "ths_user")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # 通知渠道非零 —— 避免依赖真实 DB 状态
    monkeypatch.setattr(
        sc, "_check_notify_channels",
        lambda: CheckResult("notify_channels", "ok", "可用通知渠道: 1 个"),
    )

    results = run_startup_checks()
    bad = [r for r in results if r.level in ("warning", "error")]
    assert bad == [], f"期望全部正常, 却出现: {[(r.name, r.level) for r in bad]}"


def test_check_exception_does_not_crash(monkeypatch, tmp_path):
    """任一检查抛异常 → 不阻塞、不崩溃, 仍返回结果列表。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    def _boom():
        raise RuntimeError("检查内部故障")

    monkeypatch.setattr(sc, "_check_thsdk", _boom)

    # 不应抛出
    results = run_startup_checks()
    assert isinstance(results, list)
    assert all(isinstance(r, CheckResult) for r in results)
    # 其余检查仍执行完成
    names = {r.name for r in results}
    assert "thsdk" not in names  # 该检查被跳过
    assert "database.dialect" in names


def test_error_level_triggers_notification(monkeypatch, tmp_path):
    """error 级别检查 → 尝试发送站内 Notification(真正推送被 mock 掉, 不落库)。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    sent = []

    def _fake_push(title, body="", **kw):
        sent.append({"title": title, "level": kw.get("level"), "category": kw.get("category")})
        return 1

    # 直接把 error 级别自检的推送钩子替换掉, 验证会被触发
    monkeypatch.setattr(sc, "_push_error_notification", lambda r: sent.append(r.name))

    monkeypatch.setattr(
        sc, "_check_data_dir_writable",
        lambda: CheckResult("data_dir", "error", "数据目录不可写(/nonexistent)"),
    )
    run_startup_checks()
    assert "data_dir" in sent


def test_dataclass_shape():
    r = CheckResult(name="x", level="warning", message="m")
    assert r.level == "warning"
    assert r.name == "x"
