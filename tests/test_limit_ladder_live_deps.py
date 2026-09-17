"""2026-09-18 门禁回归: `_default_deps()` 的 `l2_fn` 绑定。

真 bug(ruff F821 抓到, 之前门禁红着没人看): `fetch_stock_l2_batch` 原先**只在 `scan_tick()`
函数内**局部 import, 而 `_default_deps()` 是另一个函数 —— 它引用同一名字时 Python 找不到
(局部 import 不跨函数), 走到 `l2_fn=` 那行必 NameError ⇒ **连板梯队实时化(60s 调度)在默认
依赖路径上直接崩**。

本文件钉住: `_default_deps()` 能建起来, 且 `l2_fn` 就是 `stock_l2.fetch_stock_l2_batch`
(不是 None / 不是别的东西)。去掉那行局部 import ⇒ 本用例必红。
"""
from __future__ import annotations


def test_default_deps_binds_l2_fn(monkeypatch):
    from src.core import ladder_live_state as rst

    # redis 客户端在测试环境不可达; `_default_deps` 只把它塞进返回值, 不连网。
    monkeypatch.setattr(rst, "client", lambda: object())

    from src.core.limit_ladder_live import _default_deps
    from src.core.stock_l2 import fetch_stock_l2_batch

    d = _default_deps()
    assert d.l2_fn is fetch_stock_l2_batch


def test_default_deps_has_no_unbound_names(monkeypatch):
    """默认依赖包必须七件齐全(缺件 = 调度器跑起来才发现)。"""
    from src.core import ladder_live_state as rst

    monkeypatch.setattr(rst, "client", lambda: object())

    from src.core.limit_ladder_live import _default_deps

    d = _default_deps()
    for name in (
        "cli",
        "quotes_fn",
        "universe_fn",
        "names_fn",
        "yesterday_boards_fn",
        "seal_amounts_fn",
        "l2_fn",
        "save_fn",
        "load_fn",
        "meta_fn",
        "save_day_fn",
        "load_day_fn",
    ):
        assert hasattr(d, name), f"默认依赖缺 {name}"
        assert getattr(d, name) is not None, f"默认依赖 {name} 为 None"
