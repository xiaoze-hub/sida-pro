# -*- coding: utf-8 -*-
"""暗盘逐笔后台增量采集单测(2026-10-14, 禁真网络)。

审计定性: 逐笔翻页(冷启动全量/过期增量)此前**落在用户请求路径上**; 本改造把
"推进游标"挪到后台采集器(`src/core/dark_flow_sampler.py`), 请求端常态读缓存
(90s TTL)零翻页, 只在缓存空/跨日时保留请求期兜底(冷启动一次)。

覆盖(全部 urlopen mock, 不联网):
- 增量游标续拉: 只请求 `last_page` 起的页, 不重翻第 0 页; 合并去重后笔数正确。
- 缓存命中零翻页: TTL 内请求 0 次 urlopen(改造前后语义不变)。
- 冷启动兜底一次: 空缓存 → 拉一次并落缓存 → 紧随其后的请求零翻页。
- TTL 语义: TTL ≥ 90s(不破); 采集间隔 < TTL; 环境变量越界被钳回默认。
- 降级显式: 交易时段翻页成功但零新笔 → 返回旧快照 + degraded + 原因(不编造)。
- 采集器: 非交易时段 skip(零请求); 交易时段对热码增量续拉; 单只失败不抛。
- 请求端 diag: 年龄/游标/来源/翻页数/降级显式暴露。
"""
from __future__ import annotations

import datetime as _dt
import re as _re
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

from src.core import dark_flow as df  # noqa: E402
from src.core import dark_flow_sampler as sampler  # noqa: E402

CODE = "sz002361"


# ---------------------------------------------------------------------------
# 腾讯逐笔翻页 mock(记录被请求的页码; 可运行时改变有数据的页数)
# ---------------------------------------------------------------------------


class FakeTicks:
    """模拟 stock.gtimg.cn 逐笔翻页: 前 `nonempty` 页各 70 笔(seq 全局递增), 其余空页。"""

    def __init__(self, nonempty: int, rows_per_page: int = 70):
        self.nonempty = nonempty
        self.rows_per_page = rows_per_page
        self.requested: list[int] = []
        # 时刻取"现在之前"(绝不成未来 tick, 任何时段跑都不被 _drop_future_ticks 误杀)
        self._base = _dt.datetime.now() - _dt.timedelta(seconds=3000)

    def rows(self, page: int) -> str:
        out = []
        for i in range(self.rows_per_page):
            n = page * self.rows_per_page + i + 1          # 全局 seq(1 起)
            t = (self._base + _dt.timedelta(seconds=n)).strftime("%H:%M:%S")
            out.append(f"{n}/{t}/10.00/0.01/100/1000.0/B")
        return "|".join(out)

    def __call__(self, req, timeout=8):                  # noqa: ARG002 — 对齐 urlopen 签名
        page = int(_re.search(r"[?&]p=(\d+)", req.full_url).group(1))
        self.requested.append(page)
        body = f'[{1000 + page},"{self.rows(page)}"]' if page < self.nonempty else f'[{1000 + page},""]'

        class _Resp:
            def __init__(self, b: str):
                self._b = b.encode("gbk")

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp(body)


@pytest.fixture(autouse=True)
def _clean_state():
    df._TICKS_CACHE.clear()
    df._FETCH_META.clear()
    sampler.clear_tracking()
    yield
    df._TICKS_CACHE.clear()
    df._FETCH_META.clear()
    sampler.clear_tracking()


def _stale(code: str, age: float = 200.0) -> None:
    """把该 code 的缓存时间戳推旧(模拟 TTL 过期)。"""
    ts, ticks, lp, ls, day = df._TICKS_CACHE[code]
    df._TICKS_CACHE[code] = (time.time() - age, ticks, lp, ls, day)


# ---------------------------------------------------------------------------
# 增量游标续拉
# ---------------------------------------------------------------------------


def test_cold_start_then_incremental_only_after_cursor(monkeypatch):
    """冷启动(空缓存)全量一次 → 过期后**只从末游标往后**拉, 不重翻第 0 页。"""
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    # ① 冷启动: 无缓存 → 从第 0 页翻起
    t1 = df._fetch_all_ticks(CODE)
    assert len(t1) == 140
    assert min(fake.requested) == 0
    assert df._FETCH_META[CODE]["origin"] == "cold"
    cursor_page = df._TICKS_CACHE[CODE][2]
    assert cursor_page == 1                      # 页 0/1 有数据 → 游标停在 1

    # ② 推旧缓存 + 新增一页数据 → 增量续拉
    _stale(CODE)
    fake.requested.clear()
    fake.nonempty = 3
    t2 = df._fetch_all_ticks(CODE)

    assert 0 not in fake.requested, "增量续拉不得重翻第 0 页"
    assert fake.requested == sorted(fake.requested)
    assert min(fake.requested) == cursor_page, "必须从缓存末游标(重拉末页)起"
    assert len(t2) == 210                        # 3 页去重合并(旧 140 + 新尾页增量)
    assert df._FETCH_META[CODE]["origin"] == "incremental"
    assert df._FETCH_META[CODE]["pages_fetched"] == len(fake.requested)
    assert df._TICKS_CACHE[CODE][2] == 2         # 游标推进到页 2


def test_incremental_batch_smaller_than_full(monkeypatch):
    """增量续拉批更小(4): 常态只新增 1~2 页, 不该像全量那样一次提交 10 页。"""
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    df._fetch_all_ticks(CODE)
    _stale(CODE)
    fake.requested.clear()
    df._fetch_all_ticks(CODE)
    assert len(fake.requested) <= 4


def test_seq_regression_triggers_full_refetch(monkeypatch):
    """盘中数据重置(seq 倒退)仍走全量重拉 —— 增量判据修复不能吃掉这道保护。"""
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    df._fetch_all_ticks(CODE)
    assert df._TICKS_CACHE[CODE][3] == 140        # 旧末 seq
    _stale(CODE)
    # 数据重置: 页内行数缩到 3, 重拉的页 1 只回 seq 4..6(远小于旧末 140) → 判为重置
    fake.requested.clear()
    fake.rows_per_page = 3
    t = df._fetch_all_ticks(CODE)
    assert df._FETCH_META[CODE]["origin"] == "reset"
    assert 0 in fake.requested                   # 重拉 → 从第 0 页起
    assert df._TICKS_CACHE[CODE][3] == 6         # 新的一天/重置后的末 seq
    assert len(t) == 6


# ---------------------------------------------------------------------------
# 缓存命中零翻页 / 冷启动兜底一次
# ---------------------------------------------------------------------------


def test_cache_hit_zero_page_fetch(monkeypatch):
    """TTL 内: 零次 urlopen(TTL 语义与改造前一致)。"""
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    df._fetch_all_ticks(CODE)                    # 冷启动一次
    fake.requested.clear()
    df._TICKS_CACHE[CODE] = (time.time(), df._TICKS_CACHE[CODE][1], 1, 140, df._cache_day())

    ticks = df._fetch_all_ticks(CODE)
    assert len(ticks) == 140
    assert fake.requested == [], "缓存命中必须零翻页"
    info = df.tick_snapshot_info(CODE)
    assert info["pages_fetched"] == 0
    assert info["origin"] == "cache"
    assert info["fresh"] is True


def test_cold_start_fallback_once(monkeypatch):
    """冷启动兜底: 缓存空 → 恰好多拉一次并落缓存(第二次请求零翻页)。"""
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    df._fetch_all_ticks(CODE)
    n_cold = len(fake.requested)
    assert n_cold > 0
    fake.requested.clear()
    df._fetch_all_ticks(CODE)                    # 紧随其后(TTL 内)
    assert fake.requested == []
    assert df._FETCH_META[CODE]["origin"] == "cache"


def test_ttl_semantics_unchanged(monkeypatch):
    """TTL ≥ 90s(不破); 边界内命中, 边界外增量。"""
    assert df._TICKS_TTL >= 90.0, "TTL 不得下调(前端轮询节奏契约)"
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    df._fetch_all_ticks(CODE)

    # 边界内(89s) → 零翻页
    _stale(CODE, age=df._TICKS_TTL - 1)
    fake.requested.clear()
    df._fetch_all_ticks(CODE)
    assert fake.requested == []

    # 边界外(TTL+1s) → 增量翻页
    _stale(CODE, age=df._TICKS_TTL + 1)
    fake.requested.clear()
    df._fetch_all_ticks(CODE)
    assert fake.requested, "TTL 过期必须续拉"


# ---------------------------------------------------------------------------
# 降级显式
# ---------------------------------------------------------------------------


def test_degraded_explicit_when_upstream_returns_nothing(monkeypatch):
    """交易时段翻页成功但零新笔 → 返回旧快照 + degraded(原因/持续时长显式), 不编造。"""
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    df._fetch_all_ticks(CODE)
    old_ticks = list(df._TICKS_CACHE[CODE][1])
    _stale(CODE)
    fake.nonempty = 0                            # 上游全线空页
    fake.requested.clear()
    monkeypatch.setattr(df, "_in_trading_hours", lambda: True)

    ticks = df._fetch_all_ticks(CODE)
    assert len(ticks) == len(old_ticks)           # 旧快照原样返回(不补 0/不编造)
    info = df.tick_snapshot_info(CODE)
    assert info["degraded"] is True
    assert "上游无新增" in info["degraded_reason"]
    assert info["pages_fetched"] == len(fake.requested)
    assert info["degraded_sec"] is not None and info["degraded_sec"] >= 0
    since = info["degraded_since_ts"]

    # 第二轮仍降级 → 起点保持不变(能报出"上游已停更 N 秒", 而不是每轮归零)
    _stale(CODE)
    fake.requested.clear()
    df._fetch_all_ticks(CODE)
    info2 = df.tick_snapshot_info(CODE)
    assert info2["degraded"] is True
    assert info2["degraded_since_ts"] == since


def test_off_session_empty_is_not_degraded(monkeypatch):
    """非交易时段拉空属正常(盘前/盘后), 不得标降级。"""
    fake = FakeTicks(nonempty=0)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(df, "_in_trading_hours", lambda: False)
    df._fetch_all_ticks(CODE)
    info = df.tick_snapshot_info(CODE)
    assert info is None or info["degraded"] is False


def test_snapshot_info_none_without_cache():
    df._TICKS_CACHE.pop(CODE, None)
    assert df.tick_snapshot_info(CODE) is None


# ---------------------------------------------------------------------------
# 后台采集器
# ---------------------------------------------------------------------------


def test_collect_once_skips_off_session(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(df, "refresh_ticks", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    import src.core.quote_snapshots as qs
    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: False)
    out = sampler.collect_once()
    assert out == {"ok": True, "skipped": True, "reason": "非交易时段"}
    assert called["n"] == 0


def test_collect_once_refreshes_tracked_codes(monkeypatch):
    import src.core.quote_snapshots as qs
    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    # 请求路径登记热码(与生产同路径: _fetch_all_ticks 内注册)
    df._fetch_all_ticks(CODE)
    assert CODE in sampler.tracked_codes()
    _stale(CODE)
    fake.nonempty = 3
    fake.requested.clear()

    out = sampler.collect_once()
    assert out["ok"] is True and out["skipped"] is False
    assert out["tracked"] == 1 and out["refreshed"] == 1 and out["failed"] == 0
    assert 0 not in fake.requested, "后台采集也必须只拉游标之后的页"
    assert df._TICKS_CACHE[CODE][2] == 2
    st = sampler.sampler_stats()
    assert st["runs"] == 1 and st["last_refreshed"] == 1


def test_collect_once_single_failure_does_not_raise(monkeypatch):
    import src.core.quote_snapshots as qs
    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    sampler.register_code("sz000001")
    sampler.register_code(CODE)
    monkeypatch.setattr(
        df, "refresh_ticks",
        lambda code, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")) if code == "sz000001"
        else {"ok": True, "degraded": code == CODE},
    )
    out = sampler.collect_once()
    assert out["ok"] is True
    assert out["refreshed"] == 1 and out["failed"] == 1
    assert out["degraded"] == [CODE]


def test_sampler_interval_alignment(monkeypatch):
    """采集间隔必须 < TTL(否则请求端会读到过期缓存), 且环境变量越界被钳回默认。"""
    monkeypatch.delenv(sampler._INTERVAL_ENV, raising=False)
    assert sampler.sampler_interval_s() == sampler._DEFAULT_INTERVAL_S
    assert sampler.sampler_interval_s() < df._TICKS_TTL
    monkeypatch.setenv(sampler._INTERVAL_ENV, "30")
    assert sampler.sampler_interval_s() == 30.0
    monkeypatch.setenv(sampler._INTERVAL_ENV, str(int(df._TICKS_TTL) + 30))  # ≥ TTL
    assert sampler.sampler_interval_s() == sampler._DEFAULT_INTERVAL_S
    monkeypatch.setenv(sampler._INTERVAL_ENV, "abc")
    assert sampler.sampler_interval_s() == sampler._DEFAULT_INTERVAL_S
    monkeypatch.setenv(sampler._INTERVAL_ENV, "-5")
    assert sampler.sampler_interval_s() == sampler._DEFAULT_INTERVAL_S


def test_hot_registry_lru_and_window():
    sampler.clear_tracking()
    for i in range(sampler._MAX_TRACKED + 5):
        sampler.register_code(f"sz{i:06d}")
    assert len(sampler.tracked_codes()) == sampler._MAX_TRACKED
    assert "sz000000" not in sampler.tracked_codes()   # 最久未请求被淘汰

    sampler.clear_tracking()
    sampler.register_code(CODE)
    # 超出活跃窗口(30min) → 不再采集
    assert sampler.tracked_codes(now=time.time() + sampler._HOT_TTL_S + 1) == []
    assert sampler.tracked_codes() == [CODE]


def test_clear_cache_drops_meta(monkeypatch):
    """运维清缓存必须连元数据一起清(diag 不得继续报旧来源/游标)。"""
    fake = FakeTicks(nonempty=2)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    df._fetch_all_ticks(CODE)
    assert df._FETCH_META.get(CODE)
    assert df.clear_ticks_cache(CODE) == 1
    assert df._FETCH_META.get(CODE) is None
