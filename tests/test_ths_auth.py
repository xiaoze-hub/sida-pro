"""同花顺登录态模块测试(不依赖真实网络,测纯逻辑/加密/解析)。"""

from __future__ import annotations

import re

import pytest

from src.core.ths_auth import _rsa_encrypt, login, ThsSession


def test_rsa_encrypt_roundtrip():
    """RSA 加密产物可被同花顺公钥格式解析(无二次编码)。"""
    # 用测试公钥验证: 输出是 urlencoded base64,不含 %25(二次编码)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    import base64, urllib.parse

    # 生成临时 RSA 密钥对
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    enc = _rsa_encrypt(pub_pem, "mx_test_account")
    # 不能包含二次编码特征 %25
    assert "%25" not in enc
    # 解码还原
    dec = urllib.parse.unquote(enc)
    raw = key.decrypt(
        base64.b64decode(dec),
        padding.PKCS1v15(),
    )
    assert raw.decode("gbk") == "mx_test_account"


def test_ths_session_defaults():
    """ThsSession 默认未登录。"""
    s = ThsSession()
    assert s.logged_in is False
    assert s.account == ""


def test_session_status_unlogged():
    """无凭证时 session_status 需要扫码。"""
    from src.core.ths_auth import session_status
    # monkeypatch _load 返回空
    import src.core.ths_auth as m
    m._load = lambda: {}
    st = m.session_status()
    assert st["logged_in"] is False
    assert st["need_scan"] is True
