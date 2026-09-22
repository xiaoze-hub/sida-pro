"""钉子: skill key 的盐必须**固定**(2026-09-20 生产实踩)。

## 事故
生产未设 `SKILL_KEY_SALT` ⇒ 代码回落"进程内随机盐"。而服务是多进程(uvicorn 多 worker +
调度子进程) ⇒ **每个进程一把不同的盐** ⇒ 同一把 key 的校验结果取决于请求落在哪个进程:

    实测(同一把 key, 同一端点, 连续 6 次):
      /api/usage      → 401 200 200 200 200 401
      /api/skills     → 401 401 401 200 401 200

用户侧看到的就是"skill 调用报错 401"——**间歇性的**, 而且重启后签发它的进程消失 ⇒ 永久 401。

## 为什么要钉两条
1. **盐不固定就必须拒绝签发** —— 否则会签出"出生即间歇性 401"的 key(而且很难查: 列表接口正常);
2. **每个签发点都要过守卫** —— 只堵一处, 以后新加的签发路径又会漏(本仓已有 3 处签发点)。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.web.api import skills_gateway as gw

SRC = Path(gw.__file__).read_text(encoding="utf-8")


def test_flag_is_derived_from_env():
    """临时盐标记必须由 env 推导(不许写成常量/永远 False)。"""
    assert re.search(r"_SALT_IS_EPHEMERAL\s*=\s*not\s+_KEY_SALT", SRC), "临时盐标记没有从 env 推导"


def test_hash_depends_on_salt_regression_narrative(monkeypatch):
    """**复现事故机理**: 换一把盐, 同一把 key 的哈希就完全不同 ⇒ 老进程签的 key 在新进程查不到。

    这不是"理论上会", 而是事故的直接原因: key_hash 存的是 sha256(盐:明文),
    盐一变, 库里那行就永远匹配不上(且明文不落库 ⇒ 无法重算, 只能重签)。
    """
    raw = "sk_" + "a" * 43
    monkeypatch.setattr(gw, "_KEY_SALT", "salt-A")
    h_a = gw._hash_key(raw)
    monkeypatch.setattr(gw, "_KEY_SALT", "salt-B")
    h_b = gw._hash_key(raw)
    assert h_a != h_b
    # 同一把盐下是可复现的(否则连正常校验都不成立)
    monkeypatch.setattr(gw, "_KEY_SALT", "salt-A")
    assert gw._hash_key(raw) == h_a
    assert hashlib.sha256(f"salt-A:{raw}".encode()).hexdigest() == h_a


def test_mint_is_refused_when_salt_is_ephemeral(monkeypatch):
    """盐不固定 → **拒绝签发**(503), 且文案必须点名 SKILL_KEY_SALT(否则运维不知道改什么)。"""
    monkeypatch.setattr(gw, "_SALT_IS_EPHEMERAL", True)
    with pytest.raises(HTTPException) as ei:
        gw._require_stable_salt()
    assert ei.value.status_code == 503
    assert "SKILL_KEY_SALT" in str(ei.value.detail)


def test_mint_allowed_when_salt_is_stable(monkeypatch):
    monkeypatch.setattr(gw, "_SALT_IS_EPHEMERAL", False)
    gw._require_stable_salt()  # 不抛即通过


def test_every_mint_call_site_is_guarded():
    """结构钉子: **每个** `_gen_key()` 调用点之前必须有 `_require_stable_salt()`。

    只堵一处的话, 以后新加的签发路径又会签出"间歇性 401"的 key —— 所以按调用点逐个钉。
    """
    lines = SRC.split("\n")
    mint_lines = [i for i, ln in enumerate(lines) if re.search(r"=\s*_gen_key\(\)", ln)]
    assert mint_lines, "没有找到签发点?(_gen_key 用法变了就改这条判据)"
    for i in mint_lines:
        # 往前找 3 行内必须有守卫
        window = "\n".join(lines[max(0, i - 3) : i])
        assert "_require_stable_salt()" in window, f"第 {i + 1} 行的签发点没有盐守卫: {lines[i].strip()}"
