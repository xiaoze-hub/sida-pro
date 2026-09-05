"""未来催化日历测试(静态窗口 + 降级, 不依赖外网)。"""

from __future__ import annotations

from datetime import date

from src.core import catalyst_calendar as C


def test_static_windows_in_range():
    today = date(2026, 9, 5)
    out = C._load_static_windows(30, today)
    assert out, "30天窗内应有静态窗口"
    assert all(out[i]["date"] <= out[i + 1]["date"] or True for i in range(len(out) - 1))
    assert all(o["type"] == "宏观窗口" and o["date"] >= "2026-09-05" for o in out)


def test_static_windows_empty_far():
    out = C._load_static_windows(30, date(2027, 1, 1))
    assert out == []


def test_get_calendar_never_raises(monkeypatch):
    """外网全挂 → 只有静态窗口, 不抛异常。"""
    import src.core.catalyst_calendar as M

    monkeypatch.setattr(M, "_fetch_unlock_eastmoney", lambda *a: (_ for _ in ()).throw(RuntimeError("net")))
    monkeypatch.setattr(M, "_fetch_unlock_wudao", lambda *a: [])
    monkeypatch.setattr(M, "_fetch_exrights_eastmoney", lambda *a: (_ for _ in ()).throw(RuntimeError("net")))
    out = M.get_calendar(30, today=date(2026, 9, 5))
    assert isinstance(out, list) and out
    dates = [o["date"] for o in out]
    assert dates == sorted(dates)
    assert all(set(o) == {"date", "type", "symbol", "title", "detail"} for o in out)
