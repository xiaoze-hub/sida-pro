"""baostock socket 超时补丁的跨版本签名回归测试(2026-10-21 生产 502 修复)。

背景(生产事故):
  `forecast_lib.forecast_history.patch_baostock_timeout` 把
  `baostock.util.socketutil.SocketUtil.connect` 换成只收 `(self)` 的闭包, 而生产
  venv 装的是 baostock **0.9.40**, 其原签名是 `connect(self, api_key)`,
  `login.loginout` 调用 `mySocketUtil.connect(apiKey)` →
  `TypeError: _connect_with_timeout() takes 1 positional argument but 2 were given`
  → bs.login 失败 → `/predict` 整条链路 HTTP 502。

本测试**不触网络**: 用假的 baostock 模块树(socketutil/context/contants)注入
sys.modules, 让补丁作用于假模块, 断言:
  ① 0.9.40 签名 `(self, api_key)`: patch 后带参调用不抛 TypeError, 连到普通服务器
  ② api_key 以 "bs-" 开头 → 连到 VIP 服务器(保留 0.9.40 的原有分流行为)
  ③ 旧版 0.9.30 签名 `(self)`: 无参调用仍可用(向后兼容)
  ④ 两条路径都设了 socket 超时(补丁的原始目的: 防 send_msg 永久阻塞)
  ⑤ 重复调用 patch 幂等(不会把自己包成第二层)
"""
from __future__ import annotations

import socket
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import forecast_lib.forecast_history as fh  # noqa: E402


class _FakeSocket:
    """记录 connect 目标与超时的假 socket。"""

    instances: list["_FakeSocket"] = []

    def __init__(self, family, type_):  # noqa: ANN001
        self.family = family
        self.type = type_
        self.timeout = None
        self.connected_to = None
        self.closed = False
        _FakeSocket.instances.append(self)

    def settimeout(self, value):  # noqa: ANN001
        self.timeout = value

    def connect(self, addr):  # noqa: ANN001
        self.connected_to = addr

    def send(self, _data):  # noqa: ANN001
        pass

    def recv(self, _n):  # noqa: ANN001
        return b""

    def close(self):
        self.closed = True


def _install_fake_baostock(connect_src: str):
    """注入一棵最小的假 baostock 模块树; connect 方法源码由 connect_src 决定。

    返回的 `fake_socket_mod` 会被装到 sys.modules["socket"] 上 —— 补丁里的
    `import socket as _socket` 是**函数内局部 import**, 必须从 sys.modules 生效。
    """
    socketutil = types.ModuleType("baostock.util.socketutil")
    context = types.ModuleType("baostock.common.context")
    contants = types.ModuleType("baostock.common.contants")
    contants.BAOSTOCK_SERVER_IP = "public-api.baostock.com"
    contants.BAOSTOCK_VIP_SERVER_IP = "vip-api.baostock.com"
    contants.BAOSTOCK_SERVER_PORT = 10030

    ns: dict = {}
    code = "class SocketUtil(object):\n    def connect" + connect_src + ":\n        pass\n"
    exec(compile(code, "<fake-baostock>", "exec"), ns)  # noqa: S102 — 测试自带字面量源码
    socketutil.SocketUtil = ns["SocketUtil"]

    baostock = types.ModuleType("baostock")
    util = types.ModuleType("baostock.util")
    common = types.ModuleType("baostock.common")
    baostock.util = util
    baostock.common = common
    util.socketutil = socketutil
    common.context = context
    common.contants = contants

    # 假 socket 模块: 保留真实模块的常量, 只替换 socket 构造器
    fake_socket_mod = types.ModuleType("socket")
    fake_socket_mod.AF_INET = socket.AF_INET
    fake_socket_mod.SOCK_STREAM = socket.SOCK_STREAM
    fake_socket_mod.socket = _FakeSocket
    fake_socket_mod.timeout = socket.timeout

    mods = {
        "socket": fake_socket_mod,
        "baostock": baostock,
        "baostock.util": util,
        "baostock.util.socketutil": socketutil,
        "baostock.common": common,
        "baostock.common.context": context,
        "baostock.common.contants": contants,
    }
    return mods, socketutil, context, contants


@pytest.fixture
def fake_baostock(monkeypatch):
    def _factory(connect_src: str):
        mods, socketutil, context, contants = _install_fake_baostock(connect_src)
        for name, mod in mods.items():
            monkeypatch.setitem(sys.modules, name, mod)
        # patch 幂等标志挂在 socketutil 模块上, 每次用例必须是全新模块
        monkeypatch.setattr(socketutil, "_timeout_patched", False, raising=False)
        _FakeSocket.instances = []
        return socketutil, context, contants

    return _factory


def test_patch_accepts_0940_signature_with_api_key(fake_baostock):
    """① 生产签名 connect(self, api_key): patch 后带参调用不得 TypeError。"""
    socketutil, context, contants = fake_baostock("(self, api_key)")

    fh.patch_baostock_timeout()

    sock_util = socketutil.SocketUtil()
    sock_util.connect("")  # ← 生产 login.loginout 的真实调用形态(apiKey 为空串)

    sock = getattr(context, "default_socket")
    assert isinstance(sock, _FakeSocket)
    assert sock.connected_to == (contants.BAOSTOCK_SERVER_IP, contants.BAOSTOCK_SERVER_PORT)


def test_patch_routes_bs_key_to_vip_server(fake_baostock):
    """② api_key 以 bs- 开头 → VIP 服务器(保留 0.9.40 原分流行为)。"""
    socketutil, context, _contants = fake_baostock("(self, api_key)")

    fh.patch_baostock_timeout()
    socketutil.SocketUtil().connect("bs-abcdef123456")

    assert getattr(context, "default_socket").connected_to == (
        "vip-api.baostock.com",
        10030,
    )


def test_patch_still_supports_legacy_no_api_key_signature(fake_baostock):
    """③ 旧版 0.9.30 签名 connect(self): 无参调用仍可用。"""
    socketutil, context, contants = fake_baostock("(self)")

    fh.patch_baostock_timeout()
    socketutil.SocketUtil().connect()

    assert getattr(context, "default_socket").connected_to == (
        contants.BAOSTOCK_SERVER_IP,
        contants.BAOSTOCK_SERVER_PORT,
    )


def test_patch_sets_socket_timeout_both_paths(fake_baostock):
    """④ 补丁原始目的不能丢: socket 必须带超时(否则 send_msg 永久阻塞)。"""
    for src, args in ((("(self, api_key)"), ("",)), (("(self)"), ())):
        socketutil, context, _c = fake_baostock(src)
        fh.patch_baostock_timeout()
        socketutil.SocketUtil().connect(*args)
        assert getattr(context, "default_socket").timeout == fh._BAOSTOCK_TIMEOUT_SEC
        assert socketutil._timeout_patched is True


def test_patch_is_idempotent(fake_baostock):
    """⑤ 重复调用不得二次包装(否则 __wrapped__ 链与行为都会漂移)。"""
    socketutil, _context, _c = fake_baostock("(self, api_key)")

    fh.patch_baostock_timeout()
    first = socketutil.SocketUtil.connect
    fh.patch_baostock_timeout()

    assert socketutil.SocketUtil.connect is first
