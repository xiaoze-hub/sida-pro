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


class TestClearSession:
    """clear_session: 删凭证四键, 幂等(2026-09-05, v0.5.3)。"""

    def _patch_db(self, monkeypatch, store):
        import src.core.ths_auth as M
        import src.web.database as DB

        class _Q:
            def filter(self, *a):
                return self

            def delete(self, synchronize_session=False):
                n = 0
                for k in [M.K_ACCOUNT, M.K_PASSWORD, M.K_EXPIRES, M.K_USERID]:
                    if k in store:
                        del store[k]
                        n += 1
                return n

        class _FakeDB:
            def query(self, *a):
                return _Q()

            def commit(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(DB, "SessionLocal", lambda: _FakeDB())

    def test_clear_removes_keys(self, monkeypatch):
        import src.core.ths_auth as M

        store = {
            M.K_ACCOUNT: "mx_x", M.K_PASSWORD: "p",
            M.K_USERID: "u", M.K_EXPIRES: "2026-09-05T00:00:00",
            "other_key": "keep",
        }
        self._patch_db(monkeypatch, store)
        M.clear_session()
        assert "ths_account" not in store
        assert "ths_password" not in store
        assert store["other_key"] == "keep"

    def test_clear_idempotent_empty_db(self, monkeypatch):
        import src.core.ths_auth as M

        self._patch_db(monkeypatch, {})
        M.clear_session()  # 空库不抛
