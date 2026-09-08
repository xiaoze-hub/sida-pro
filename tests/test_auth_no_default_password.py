"""0.6 静态回归: 公开仓库不得再含固定管理员密码/开关(2026-09-08)。

覆盖:
1. src/ + scripts/ + 部署面(.github/docker-compose/Dockerfile) 无历史固定密码字面量
2. 无固定默认密码常量、无对应环境开关(符号名在本文件均拼接构造)
3. auth.py 兜底首启走 secrets.token_urlsafe 随机强密码
4. 自助改密端点存在(前端已接入, docs/KNOWN_ISSUES.md P1 已关闭)
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

# 字符串拆开拼接: 本测试文件自身不得成为明文密码/符号载体
OLD_FIXED_PW = "xz" ".170530"
CONST_NAME = "DEFAULT_" + "ADMIN_PASSWORD"
FLAG_NAME = "AUTH_ALLOW_" + "DEFAULT_ADMIN"

SCAN_DIRS = ("src", "scripts", ".github", "deploy")
SCAN_FILES = ("docker-compose.yml", "Dockerfile", ".env.example")


def _iter_targets():
    for d in SCAN_DIRS:
        base = PROJECT_ROOT / d
        if base.exists():
            for p in base.rglob("*.py"):
                yield p
            for p in base.rglob("*.yml"):
                yield p
            for p in base.rglob("*.sh"):
                yield p
    for f in SCAN_FILES:
        p = PROJECT_ROOT / f
        if p.exists():
            yield p


def test_no_fixed_admin_password_anywhere():
    """历史固定密码不得出现在任何源码/部署文件(CHANGELOG 历史记录除外)。"""
    hits = []
    for p in _iter_targets():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if OLD_FIXED_PW in text:
            hits.append(str(p.relative_to(PROJECT_ROOT)))
    assert hits == [], f"固定密码残留: {hits}"


def test_no_default_password_constant_or_flag():
    """固定默认密码常量与其环境开关必须彻底删除。"""
    hits = []
    for p in _iter_targets():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if CONST_NAME in text or FLAG_NAME in text:
            hits.append(str(p.relative_to(PROJECT_ROOT)))
    assert hits == [], f"默认密码常量/开关残留: {hits}"


def test_fallback_bootstrap_uses_random_secret():
    """兜底首启必须用 secrets.token_urlsafe 随机密码 + stderr 一次性打印。"""
    src = (PROJECT_ROOT / "src/web/api/auth.py").read_text(encoding="utf-8")
    assert "secrets.token_urlsafe" in src, "兜底首启必须生成随机强密码"
    assert "file=_sys.stderr" in src, "随机密码必须打印到 stderr(Docker logs 可见, 仅一次)"
    assert "改密" in src, "启动提示必须引导首登改密"


def test_change_password_endpoint_exists():
    """自助改密端点存在且校验旧密码(token_version 踢人)。"""
    src = (PROJECT_ROOT / "src/web/api/auth.py").read_text(encoding="utf-8")
    assert '@router.post("/change-password")' in src
    assert "old_password" in src, "改密必须先校验旧密码"
    assert "token_version += 1" in src, "改密必须踢掉旧 token"
