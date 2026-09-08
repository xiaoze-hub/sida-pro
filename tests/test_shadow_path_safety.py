"""C1(2026-09-08 风险方案1.4)影子报告路径穿越回归。

原实现两处洞:
- 上传落盘名直接拼 file.filename("../../" 可写出 _UPLOAD_DIR);
- /report/{shadow_id} 无格式校验/路径包含校验/归属校验, 任意登录者可枚举读
  他人报告(甚至未登录 —— 端点原本没挂 get_current_user)。
"""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.web.api import shadow as shadow_api


@pytest.fixture()
def report_dir(tmp_path, monkeypatch):
    d = tmp_path / "shadow_reports"
    d.mkdir()
    monkeypatch.setattr(shadow_api, "_REPORT_DIR", d)
    return d


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    d = tmp_path / "shadow_uploads"
    d.mkdir()
    monkeypatch.setattr(shadow_api, "_UPLOAD_DIR", d)
    return d


def _user(shadow_id: str | None):
    return SimpleNamespace(
        id="u1", role="member",
        shadow_profile_json={"shadow_id": shadow_id} if shadow_id else None,
    )


# ── 上传: 落盘名与用户输入解耦 ────────────────────────────────────


@pytest.mark.parametrize("evil", ["../../etc/passwd.csv", "%2e%2e%2fetc%2fpasswd.csv",
                                  "..\\..\\win.ini.csv", "正常名.csv"])
def test_upload_filename_cannot_escape(upload_dir, evil):
    """恶意/正常文件名落盘都必须仍在 _UPLOAD_DIR 内(验收: is_relative_to 断言)。"""
    file = SimpleNamespace(filename=evil, file=io.BytesIO(b"garbage-not-a-journal"))
    with pytest.raises(HTTPException):
        shadow_api.analyze_journal(file=file, user=_user(None), db=SimpleNamespace())
    files = list(upload_dir.iterdir())
    assert len(files) == 1
    dest = files[0].resolve()
    assert dest.is_relative_to(upload_dir.resolve())
    # 用户名任何成分都不出现在落盘名里(uuid 解耦)
    assert "passwd" not in dest.name and "win" not in dest.name and "正常名" not in dest.name


# ── 报告读取: 格式/包含/归属三重校验 ──────────────────────────────


def test_report_owner_can_read(report_dir):
    sid = "shadow_abcd1234"
    (report_dir / f"{sid}.html").write_text("<html>ok</html>", encoding="utf-8")
    path = shadow_api._resolve_report(sid, ".html", _user(sid))
    assert path.is_relative_to(report_dir.resolve())


def test_report_other_user_denied(report_dir):
    """验收: demo 账号请求他人的 shadow_id → 404(不泄露存在性)。"""
    sid = "shadow_abcd1234"
    (report_dir / f"{sid}.html").write_text("<html>x</html>", encoding="utf-8")
    with pytest.raises(HTTPException) as ei:
        shadow_api._resolve_report(sid, ".html", _user("shadow_ffffffff"))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException):
        shadow_api._resolve_report(sid, ".html", _user(None))


def test_report_bad_format_denied(report_dir):
    """非 shadow_<8hex> 格式(含路径穿越/URL编码绕过写法)一律 404。"""
    for evil in ["../../etc/passwd", "%2e%2e%2f", "..", "shadow_", "x.html", ""]:
        with pytest.raises(HTTPException) as ei:
            shadow_api._resolve_report(evil, ".html", _user("shadow_abcd1234"))
        assert ei.value.status_code == 404


def test_report_missing_file_is_404_not_500(report_dir):
    with pytest.raises(HTTPException) as ei:
        shadow_api._resolve_report("shadow_abcd1234", ".html", _user("shadow_abcd1234"))
    assert ei.value.status_code == 404


def test_report_stays_inside_dir(report_dir, monkeypatch):
    """resolve 后路径包含校验: 即使将来引入符号链接也逃不出报告目录。"""
    sid = "shadow_abcd1234"
    outside = report_dir.parent / "outside.html"
    outside.write_text("secret", encoding="utf-8")
    link = report_dir / f"{sid}.html"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows 无符号链接权限")
    with pytest.raises(HTTPException) as ei:
        shadow_api._resolve_report(sid, ".html", _user(sid))
    assert ei.value.status_code == 404
