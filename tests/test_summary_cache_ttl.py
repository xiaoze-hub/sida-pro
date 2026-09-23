"""summary_cache TTL 口径钉子(2026-09-23)。

生产事故(用户报障): 数智决策显示 S区, 但 K线图最新标记仍是 G —— 查下来是
**summary_cache 缓存永不失效**, 导致 K 线图层(gs_signals/fund_flow/events/chips)冻结在写入时刻。

机制(实测):
- 列是 `timestamp without time zone`, PG 会话时区 Asia/Shanghai ⇒ 写入侧传 aware UTC 会被存成
  **CST 墙上时间**(05:50 UTC → 存 13:50);
- 读侧把 naive 当 UTC ⇒ age = 本地 - UTC = **-8 小时(负数)** ⇒ `age > ttl` 永远不成立
  ⇒ 那行(603629, 冻结在 09:42)被当作有效数据返回了 4 小时。

这组用例钉死: ① 未来时间戳(负 age)必须视为过期 ② 正常 UTC 行在 TTL 内命中 ③ 超 TTL 过期。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core import summary_cache as SC


class _Row(dict):
    pass


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        return _FakeResult(self._row)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def begin(self):
        return self


class _FakeEngine:
    def __init__(self, row):
        self._row = row

    def begin(self):
        return _FakeConn(self._row)


def _patch(monkeypatch, computed_at):
    row = {"computed_at": computed_at, "ttl_s": 300, "payload": '{"gs_signals": [1]}'}
    monkeypatch.setattr(SC, "_engine", lambda: _FakeEngine(row))


class TestSummaryCacheTtl:
    def test_正常_UTC_行_在_TTL_内命中(self, monkeypatch):
        _patch(monkeypatch, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10))
        assert SC.get_cached_summary("603629", "CN", ttl_s=300) == {"gs_signals": [1]}

    def test_超过_TTL_过期(self, monkeypatch):
        _patch(monkeypatch, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=600))
        assert SC.get_cached_summary("603629", "CN", ttl_s=300) is None

    def test_未来时间戳_视为过期_不许当有效(self, monkeypatch):
        """这就是生产事故那行: 存的是 CST 墙上时间(比 UTC now 大 8 小时) ⇒ 必须过期。

        修之前 age = -8h(负数), `age > ttl` 不成立 ⇒ 缓存永不失效(实测冻结 4 小时)。
        """
        future_cst_wall = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
        _patch(monkeypatch, future_cst_wall)
        assert SC.get_cached_summary("603629", "CN", ttl_s=300) is None

    def test_字符串时间戳也能解析(self, monkeypatch):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        _patch(monkeypatch, ts)
        assert SC.get_cached_summary("603629", "CN", ttl_s=300) == {"gs_signals": [1]}

    def test_坏时间戳视为过期不抛错(self, monkeypatch):
        _patch(monkeypatch, "not-a-timestamp")
        assert SC.get_cached_summary("603629", "CN", ttl_s=300) is None

    def test_无行返回_None(self, monkeypatch):
        monkeypatch.setattr(SC, "_engine", lambda: _FakeEngine(None))
        assert SC.get_cached_summary("603629", "CN", ttl_s=300) is None


class TestWriteSide:
    def test_写入存_naive_UTC(self, monkeypatch):
        """写入侧必须传 naive UTC —— 传 aware 会被 PG 转成 CST 墙上时间, 与读侧口径冲突。"""
        seen: dict = {}

        class _Conn:
            def execute(self, stmt, params=None, *a, **k):
                if params and "computed_at" in params:
                    seen.update(params)
                return _FakeResult(None)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Eng:
            def begin(self):
                return _Conn()

        monkeypatch.setattr(SC, "_engine", lambda: _Eng())
        SC.put_cached_summary("603629", "CN", {"a": 1}, ttl_s=300)
        got = seen.get("computed_at")
        assert got is not None
        assert got.tzinfo is None, "必须存 naive(否则 PG 会按会话时区转成 CST 墙上时间)"
        # 且应与 UTC now 相差在 5 秒内(证明存的是 UTC 而不是本地时间)
        delta = abs((datetime.now(timezone.utc).replace(tzinfo=None) - got).total_seconds())
        assert delta < 5
