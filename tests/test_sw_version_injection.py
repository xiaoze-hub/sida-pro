# -*- coding: utf-8 -*-
"""sw.js 版本占位符注入回归 (2026-09-30)。

背景: `frontend/public/sw.js` 曾把 CACHE_NAME 硬编码成 `panwatch-v0.5.69-bust`,
而 `Dockerfile` 里仍用 `sed s/__SW_VERSION__/<VERSION>/` 去替换占位符 →
**空操作**, 发版并不刷新 SW 缓存名, "发版→用户拿到新版" 靠手改硬编码。

本测试钉住: sw.js 必须保留 `__SW_VERSION__` 占位符, Dockerfile 必须注入该占位符,
且占位符缺失时构建不得静默失败(至少要打 WARNING)。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SW = ROOT / "frontend" / "public" / "sw.js"
DOCKERFILE = ROOT / "Dockerfile"


def test_sw_js_defines_version_placeholder():
    text = SW.read_text(encoding="utf-8")
    assert "__SW_VERSION__" in text, (
        "sw.js 必须保留 __SW_VERSION__ 占位符, 否则 Dockerfile 的 sed 注入成空操作"
    )
    m = re.search(r"const CACHE_NAME = '([^']+)'", text)
    assert m, "未找到 CACHE_NAME 定义"
    assert "__SW_VERSION__" in m.group(1), "CACHE_NAME 必须含版本占位符, 由构建注入真实版本"


def test_sw_js_has_no_hardcoded_release_version():
    text = SW.read_text(encoding="utf-8")
    # 旧 bug: 硬编码 panwatch-v0.5.69-bust → sed 找不到占位符 → 发版不清缓存
    assert not re.search(r"panwatch-v\d+\.\d+", text), (
        "sw.js 不应硬编码版本号(应由构建时注入 __SW_VERSION__)"
    )


def test_dockerfile_injects_placeholder_and_warns_when_missing():
    text = DOCKERFILE.read_text(encoding="utf-8")
    # 注入行: sed 替换占位符为 ${VERSION_VAL}
    assert "s/__SW_VERSION__/${VERSION_VAL}/g" in text, "Dockerfile 必须 sed 注入 __SW_VERSION__"
    # 占位符缺失时不得静默失败: 分支里必须有显式 WARNING
    assert "WARNING" in text and "public/sw.js" in text, (
        "sw.js 无占位符时构建日志必须打 WARNING, 不得静默失败"
    )


def test_simulated_injection_replaces_placeholder():
    """模拟构建注入: 替换后缓存名带真实版本号且占位符消失。"""
    text = SW.read_text(encoding="utf-8")
    out = text.replace("__SW_VERSION__", "v0.13.40")
    assert "__SW_VERSION__" not in out
    assert "panwatch-v0.13.40-bust" in out


def test_index_html_registers_sw():
    """确认前端仍注册 /sw.js(否则占位符注入无从生效)。"""
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "serviceWorker.register('/sw.js')" in html
