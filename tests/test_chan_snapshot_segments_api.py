"""只读接口接入线段(chan-segment-v2)与操作口径中枢(chan-centre-adapter-v1)。

为什么必须绑在**同一份**快照上
------------------------------
`GET /api/klines/{symbol}/chan-structure` 已经用内容摘要把分型/笔锁在"图表看到的
那份 bar"上。线段与中枢如果各自再取一次数, 就会出现"分型是这张图、中枢是另一张图"
的混合结论 —— 那种响应比"没算"更危险, 因为它看起来完整。本文件钉住:

  ① 线段与操作口径中枢**只**由同一个 ``ChanSnapshot`` → 同一个 ``PenSnapshot``
     推导(一次取数, 一次结构构建, 一次笔, 一次线段), 不另写摘要、不二次取数;
  ② ``segments`` 只把 **confirmed 前缀**当几何; 尾部 candidate 单列并标注"可漂移";
     pending / blocked 单列并保留内核原因; ``blocked`` **不携带价格几何**
     (否则会被读成一条线, 进而被喂成中枢);
  ③ 证据笔(特征序列)与几何跨度分开给出: 证据笔可以落在终点之后, 不许合并计数;
  ④ ``operational_centre`` 原样透传 ``basis='segment_a0'`` /
     ``strictness='operational_not_strict'`` / ``status`` / ``chain_complete`` /
     ``issues`` / ``limitations``, 绝不写成「中枢」或「严格中枢」;
     ``not_enough_confirmed_segments`` 不得显示成「无中枢」;
  ⑤ 继续沿用同一摘要复核与 409: 无摘要 / 摘要畸形 / 身份不明确 / as_of 不一致
     一律 fail-closed, 错误响应里不许夹带半份结构;
  ⑥ 旧响应兼容: 分型/笔字段原样不动, 新增字段各自带 schema + kernel 版本号;
  ⑦ 不得把 operational 中枢当买卖点/交易信号, 也不得为它新增 LLM 猜测。

fixture 是真实的 ``bars → 分型 → 笔 → 线段 → 适配器`` 链路(不是手写线段对象),
所以这里断言的数字是内核算出来的, 不是本文档编的。

DB 隔离: 只碰 conftest 建在临时 ``DATA_DIR`` 下的 SQLite; 本文件另有
``test_suite_uses_isolated_scratch_sqlite`` 直接对**真实 engine** 断言, 裸跑真库即红。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.collectors.kline_collector import KlineData
from src.core.chan_centre_adapter import (
    ADAPTER_VERSION,
    BASIS,
    LIMITATIONS,
    STRICTNESS,
    centre_from_segment_snapshot,
)
from src.core.chan_operational_centre import SEGMENTS_ENGINE
from src.core.chan_pen import PEN_KERNEL_VERSION, Pen, PenEndpoint, PenRules, PenSnapshot
from src.core.chan_segment import (
    SEGMENT_KERNEL_VERSION,
    SegmentBinding,
    segment_from_pens,
)
from src.web.api import chan_snapshot as chan
from src.web.api.auth import get_current_user

NOW = datetime(2026, 11, 17, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

USER_ID = "c0ffee00-2222-4333-8444-csegapi00001"
USER_NAME = "chan-seg-api-contract"
assert len(USER_ID) == 36, "users.id 是 VARCHAR(36), 测试种子必须同宽"

_START = date(2026, 1, 1)
_SYMBOL = "600000"
_OTHER = "000001"


# ── 真实 K 线 fixture: bars → 分型 → 笔 → 线段 ──────────────────────────


def _day(index: int) -> str:
    return (_START + timedelta(days=index)).isoformat()


def _zigzag_bars(pivots: list[float], *, step: int = 4, half: float = 0.5) -> list[KlineData]:
    """价格折线 → 逐日 K 线; 每个拐点独占一根合并K线(合并索引 == 原始索引)。

    与 ``tests/test_chan_centre_adapter.py`` 的 bar fixture 同源, 保证这里钉的
    线段/中枢数字与内核用例一致, 不是另造一套。
    """
    mids = [float(pivots[0]) + 1.0, float(pivots[0])]
    for a, b in zip(pivots, pivots[1:]):
        for k in range(1, step + 1):
            mids.append(round(a + (b - a) * k / step, 6))
    return [
        KlineData(date=_day(i), open=m, close=m, high=round(m + half, 6),
                  low=round(m - half, 6), volume=1000.0)
        for i, m in enumerate(mids)
    ]


#: 三条已确认线段 + 第四条被内核拒绝(characteristic_initial_containment)。
#: 操作口径中枢区间 [20.92, 24.96], 已知于第 3 条线段的确认日 2026-03-05。
_CENTRE_PIVOTS = [
    20.0, 22.72, 21.67, 24.32, 22.98, 24.46, 22.15, 24.44, 21.42, 23.61,
    22.68, 25.45, 23.4, 25.26, 23.12, 26.41, 23.45, 27.62, 25.35,
]
CENTRE_BARS = _zigzag_bars(_CENTRE_PIVOTS)
#: 前 64 根: 三条已确认线段 + 一条仍可漂移的候选线段(未触发 blocked)。
CENTRE_PREFIX_BARS = CENTRE_BARS[:64]
#: 特征序列首对就被包含且没有更早的对比对 → 线段内核在 pen 3 处 blocked, 一条线都没有。
_BLOCKED_PIVOTS = [20, 24, 20, 23, 21, 24.5, 22, 25]
BLOCKED_BARS = _zigzag_bars(_BLOCKED_PIVOTS)


def _copy(bar: KlineData) -> KlineData:
    return KlineData(date=bar.date, open=bar.open, close=bar.close,
                     high=bar.high, low=bar.low, volume=bar.volume)


def _mirror(bars: list[KlineData], axis: float = 60.0) -> list[KlineData]:
    """价格镜像: 同一时间轴上的另一只标的(区间也镜像)。"""
    return [
        KlineData(date=b.date, open=round(axis - b.open, 6), close=round(axis - b.close, 6),
                  high=round(axis - b.low, 6), low=round(axis - b.high, 6), volume=b.volume)
        for b in bars
    ]


class _Feed:
    """按 symbol 分派的可变 ``_pg_klines`` 替身(跨标的不串的对照组)。"""

    def __init__(self, series: dict[str, list[KlineData]]):
        self.series = {symbol: list(bars) for symbol, bars in series.items()}
        self.calls: list[str] = []

    def __call__(self, symbol, market_code, days):
        self.calls.append(symbol)
        bars = self.series[symbol]
        return ([_copy(bar) for bar in bars], bars[-1].date)


# ── 鉴权/客户端(复用既有只读端点的挂载口径) ────────────────────────────


def _loaded_conftest():
    import sys

    for name in ("conftest", "tests.conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "purge_users"):
            return module
    raise RuntimeError("pytest 尚未加载 conftest, 无法复用 purge_users")


@pytest.fixture()
def auth_headers():
    from src.core.auth_tokens import create_token
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == USER_ID).first()
        if user is None:
            user = User(
                id=USER_ID,
                username=USER_NAME,
                password_hash="not-a-real-password-hash",
                role="member",
                is_active=True,
                token_version=1,
            )
            db.add(user)
            db.commit()
        token = create_token(user).token
    finally:
        db.close()
    yield {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        _loaded_conftest().purge_users(db, ids=[USER_ID])
        db.commit()
    finally:
        db.close()


def _client(monkeypatch, feed: _Feed) -> TestClient:
    from src.web.api import klines as kl

    monkeypatch.setattr(kl, "_pg_klines", feed)
    monkeypatch.setattr(kl, "_finalize_daily_bars", lambda bars, *a, **k: (bars, None))
    monkeypatch.setattr(kl, "_kl_cache", {})
    monkeypatch.setattr(chan, "_now", lambda: NOW)
    app = FastAPI()
    app.include_router(kl.router, prefix="/api/klines", dependencies=[Depends(get_current_user)])
    return TestClient(app)


def _chart(client: TestClient, headers: dict, symbol: str = _SYMBOL,
           instrument: str = "stock", days: int = 120) -> dict:
    r = client.get(
        f"/api/klines/{symbol}",
        params={"market": "CN", "days": days, "interval": "1d", "instrument": instrument},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _structure(client: TestClient, headers: dict, digest: str, symbol: str = _SYMBOL,
               **overrides):
    params = {"market": "CN", "instrument": "stock", "interval": "1d", "days": 120,
              "snapshot": digest}
    params.update(overrides)
    return client.get(f"/api/klines/{symbol}/chan-structure", params=params, headers=headers)


def _body(monkeypatch, headers, bars: list[KlineData], symbol: str = _SYMBOL) -> dict:
    client = _client(monkeypatch, _Feed({symbol: bars}))
    digest = _chart(client, headers, symbol=symbol)["chan_snapshot"]["digest"]
    r = _structure(client, headers, digest, symbol=symbol)
    assert r.status_code == 200, r.text
    return r.json()


# ── 纯 payload 构建器的输入: 手写笔链(只为 no_centre 这一分支) ──────────


def _explicit_pens(prices: list[float]) -> PenSnapshot:
    """交替底/顶、全部已确认的笔链; 用作 payload 构建器的确定性输入。"""
    start = date(2026, 3, 2)
    kinds = ["bottom" if i % 2 == 0 else "top" for i in range(len(prices))]
    days = [(start + timedelta(days=4 * i)).isoformat() for i in range(len(prices))]
    pens: list[Pen] = []
    for k in range(len(prices) - 1):
        frozen_on = (date.fromisoformat(days[k + 1]) + timedelta(days=1)).isoformat()
        start_on = pens[-1].end.confirmed_on if pens else frozen_on
        pens.append(
            Pen(
                start=PenEndpoint(
                    kind=kinds[k], merged_index=4 * k, position_day=days[k],
                    price=float(prices[k]), bar_high=float(prices[k]) + 1.0,
                    bar_low=float(prices[k]) - 1.0, revisions=0, drift_days=(),
                    status="confirmed", confirmed_on=start_on,
                ),
                end=PenEndpoint(
                    kind=kinds[k + 1], merged_index=4 * (k + 1), position_day=days[k + 1],
                    price=float(prices[k + 1]), bar_high=float(prices[k + 1]) + 1.0,
                    bar_low=float(prices[k + 1]) - 1.0, revisions=0, drift_days=(),
                    status="confirmed", confirmed_on=frozen_on,
                ),
                direction="up" if kinds[k] == "bottom" else "down",
                status="confirmed",
                confirmed_on=frozen_on,
                kernel_version=PEN_KERNEL_VERSION,
            )
        )
    return PenSnapshot(
        as_of_day=max(p.confirmed_on for p in pens),
        kernel_version=PEN_KERNEL_VERSION,
        rules=PenRules(),
        endpoints=(),
        pens=tuple(pens),
        rejections=(),
        blocked_fractals=(),
        taint_indices=(),
    )


#: 三条已确认线段的区间无严格正宽度交集 (max low 20 > min high 19.58) → no_centre。
_NO_CENTRE_PIVOTS = [
    20.0, 20.97, 20.45, 23.95, 20.55, 21.13, 17.52, 19.24, 15.88, 18.96,
    15.96, 19.58, 18.68, 19.2, 15.41,
]


def _no_centre_result():
    pen = _explicit_pens(_NO_CENTRE_PIVOTS)
    segment = segment_from_pens(
        pen, binding=SegmentBinding(symbol="SZ.300000", adjust="unknown", as_of_day=pen.as_of_day)
    )
    return segment, centre_from_segment_snapshot(segment, pen_snapshot=pen)


# ── ① 契约: 同一快照上的线段 + 操作口径中枢 ─────────────────────────────


def test_segments_and_operational_centre_ride_the_same_snapshot(monkeypatch, auth_headers):
    body = _body(monkeypatch, auth_headers, CENTRE_BARS)

    segments = body["segments"]
    centre = body["operational_centre"]

    # 版本号: 各块自带 schema + 内核/适配器版本, 客户端不必猜
    assert segments["schema"] == chan.SEGMENTS_SCHEMA == "sida-chan-segments-v1"
    assert segments["kernel_version"] == SEGMENT_KERNEL_VERSION == "chan-segment-v2"
    assert segments["rules_version"] == segments["kernel_version"]
    assert segments["source_pen_kernel_version"] == PEN_KERNEL_VERSION == "chan-pen-v1"
    assert centre["schema"] == chan.OPERATIONAL_CENTRE_SCHEMA == "sida-chan-operational-centre-v1"
    assert centre["adapter_version"] == ADAPTER_VERSION == "chan-centre-adapter-v1"

    # 三条已确认线段(几何), 第四条被内核 blocked, 候选为空
    assert segments["counts"] == {"confirmed": 3, "candidate": 0, "blocked": 1, "pending": 0, "total": 4}
    assert segments["candidate_tail"] is None
    assert [
        (
            s["geometry"]["pen_start_index"], s["geometry"]["pen_end_index"],
            s["geometry"]["direction"], s["geometry"]["start"]["price"],
            s["geometry"]["end"]["price"], s["confirmed_on"], s["status"],
        )
        for s in segments["confirmed"]
    ] == [
        (0, 4, "up", 19.5, 24.96, "2026-02-09", "confirmed"),
        (5, 7, "down", 24.96, 20.92, "2026-02-21", "confirmed"),
        (8, 10, "up", 20.92, 25.95, "2026-03-05", "confirmed"),
    ]
    assert [s["status"] for s in segments["blocked"]] == ["blocked"]

    # 同一份快照: 线段截止日 == 笔截止日 == 适配器输入的 as_of
    assert body["as_of_day"] == segments["as_of_day"] == centre["as_of_day"] == "2026-03-15"

    # 操作口径中枢: 原样透传, 绝不升级成「中枢」/「严格中枢」
    assert centre["basis"] == BASIS == "segment_a0"
    assert centre["strictness"] == STRICTNESS == "operational_not_strict"
    assert centre["input_kind"] == "segment_kernel_confirmed_prefix"
    assert centre["segments_engine"] == SEGMENTS_ENGINE
    assert centre["status"] == "centre"
    assert centre["chain_complete"] is False  # 第 4 条被 blocked → 结构不完整
    assert centre["absence_reason"] is None
    assert centre["confirmed_prefix_length"] == 3
    assert centre["total_segments"] == 4
    assert centre["zone"] == {"zd": 20.92, "zg": 24.96, "width": pytest.approx(4.04)}
    assert centre["segment_indices"] == [0, 1, 2]
    assert centre["confirmed_on"] == "2026-03-05"
    assert centre["visible_on"] == "2026-03-05"
    assert centre["issues"] == [
        {
            "reason": "characteristic_initial_containment",
            "detail": centre["issues"][0]["detail"],
            "blocking": True,
            "segment_index": 3,
            "pen_index": 11,
        }
    ]
    assert list(centre["limitations"]) == list(LIMITATIONS)
    assert "case2_gap_segments_not_implemented_upstream" in centre["limitations"]
    # 同一个快照的第二个证明: 中枢的笔序列摘要就是线段块的 input_digest
    assert centre["source"] == {
        "symbol": _SYMBOL,
        "adjust": "unknown",
        "pen_digest": segments["input_digest"],
        "segment_kernel": SEGMENT_KERNEL_VERSION,
        "pen_kernel": PEN_KERNEL_VERSION,
    }
    # 命名诚实: operational 几何 + 明确否定语, 不是「中枢」也不是「严格中枢」
    assert "不是「中枢」" in centre["label_note"]
    assert "不是「严格中枢」" in centre["label_note"]
    assert "交易信号" in centre["label_note"]
    assert centre["status_note"]
    assert centre["strictness"] not in {"strict", "zhongshu"}


def test_blocked_segment_never_carries_geometry(monkeypatch, auth_headers):
    body = _body(monkeypatch, auth_headers, CENTRE_BARS)
    blocked = body["segments"]["blocked"]
    assert len(blocked) == 1
    entry = blocked[0]
    assert entry["status"] == "blocked"
    assert entry["reason"] == "characteristic_initial_containment"
    # 内核拒绝出版线段 → 价格区间不对外, 否则会被读成一条线 (并进而被喂成中枢)
    assert entry["geometry_withheld"] is True
    assert "geometry" not in entry and "evidence" not in entry
    flat = json.dumps(entry, ensure_ascii=False)
    for forbidden in ('"price"', '"high"', '"low"', '"start"', '"end"'):
        assert forbidden not in flat, (forbidden, flat)
    # 但审计位置必须保留: 哪段笔区间被拒绝, 决定了后面为什么没有几何
    assert entry["pen_start_index"] == 11 and entry["pen_end_index"] == 13
    assert entry["direction"] == "down"
    # 逐笔拒绝记录是 audit-only, 不许被当作可直接绘制的结构
    assert all(r["audit_only"] is True for r in body["segments"]["rejections"])


def test_evidence_pens_are_reported_apart_from_the_geometric_span(monkeypatch, auth_headers):
    body = _body(monkeypatch, auth_headers, CENTRE_BARS)
    first = body["segments"]["confirmed"][0]
    geometry = first["geometry"]
    evidence = first["evidence"]

    # 几何跨度: pen 0..4; 证据笔(特征序列) 可以落在终点之后
    assert (geometry["pen_start_index"], geometry["pen_end_index"]) == (0, 4)
    assert geometry["pens_used"] == 5
    assert [role["role"] for role in evidence["fractal_elements"]] == ["left", "middle", "right"]
    assert evidence["pen_indices"], "证据笔必须逐笔列出, 不能只给一个数"
    assert max(evidence["pen_indices"]) > geometry["pen_end_index"], (
        "本 fixture 的特征序列右元素本就在终点之后; 若相等, 本用例没验到分离"
    )
    # 证据日 == 线段确认日; 且证据笔不进入几何跨度计数
    assert evidence["confirmed_on"] == first["confirmed_on"] == "2026-02-09"
    assert all(0 <= i < len(body["pens"]) for i in evidence["pen_indices"])
    assert geometry["pens_used"] == geometry["pen_end_index"] - geometry["pen_start_index"] + 1
    assert evidence["note"]


def test_candidate_tail_is_driftable_and_not_geometry(monkeypatch, auth_headers):
    body = _body(monkeypatch, auth_headers, CENTRE_PREFIX_BARS)
    segments = body["segments"]
    tail = segments["candidate_tail"]

    assert segments["counts"] == {"confirmed": 3, "candidate": 1, "blocked": 0, "pending": 0, "total": 4}
    assert tail is not None
    assert tail["status"] == "candidate"
    assert tail["driftable"] is True
    assert tail["confirmed_on"] is None
    assert "漂移" in tail["note"]
    assert tail["geometry"]["end"]["status"] == "candidate"

    centre = body["operational_centre"]
    assert centre["status"] == "centre"
    assert centre["chain_complete"] is True  # 候选尾部不是阻断
    assert [i["reason"] for i in centre["issues"]] == ["candidate_segment_excluded"]
    assert all(i["blocking"] is False for i in centre["issues"])
    assert center_zone(centre) == (20.92, 24.96)


def center_zone(centre: dict):
    zone = centre["zone"] or {}
    return (zone.get("zd"), zone.get("zg"))


def test_not_enough_confirmed_segments_is_never_shown_as_no_centre(monkeypatch, auth_headers):
    # 旧的最小 fixture: 只有 5 个合并K线分型 / 3 笔, 远不够三段
    bars = [
        KlineData(date=d, open=o, close=c, high=h, low=lo, volume=1000.0)
        for d, (o, c, h, lo) in zip(
            ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"],
            [(9.2, 9.8, 10.0, 9.0), (9.8, 10.8, 11.0, 9.5), (10.8, 11.8, 12.0, 10.5),
             (11.0, 10.4, 11.2, 10.0), (10.4, 11.3, 11.5, 9.8), (10.7, 9.4, 10.8, 9.2)],
        )
    ]
    body = _body(monkeypatch, auth_headers, bars)
    centre = body["operational_centre"]
    segments = body["segments"]

    assert segments["counts"]["confirmed"] < 3
    assert centre["status"] == "not_enough_confirmed_segments"
    assert centre["status"] != "no_centre"
    assert centre["absence_reason"] == "fewer_than_three_confirmed_segments"
    assert centre["zone"] is None and centre["segment_indices"] is None
    assert centre["chain_complete"] is True
    # 措辞必须显式区分「不够」与「实测无中枢」
    assert "不等于" in centre["status_note"]
    assert "无中枢" in centre["status_note"]
    assert "实测" not in centre["status_note"]


def test_blocked_before_centre_is_distinct_from_not_enough(monkeypatch, auth_headers):
    body = _body(monkeypatch, auth_headers, BLOCKED_BARS)
    segments = body["segments"]
    centre = body["operational_centre"]

    assert segments["confirmed"] == []
    assert segments["blocked"] == []
    assert [p["status"] for p in segments["pending"]] == ["blocked"]
    pending = segments["pending"][0]
    assert pending["reason"] == "characteristic_initial_containment"
    assert pending["blocked_at_pen_index"] == 3
    assert pending["end_pen_index"] is None
    assert pending["pens_used"] == 0
    # pending 的位置信息只作审计, 不叫 start/end 几何
    assert "audit" in pending and "start_price" not in pending

    assert centre["status"] == "blocked_before_centre"
    assert centre["status"] != "not_enough_confirmed_segments"
    assert centre["absence_reason"] == "segment_kernel_blocked"
    assert centre["chain_complete"] is False
    assert centre["confirmed_prefix_length"] == 0
    assert centre["zone"] is None
    assert [i["reason"] for i in centre["issues"] if i["blocking"]] == [
        "characteristic_initial_containment"
    ]
    assert "不完整" in centre["status_note"]


# ── ② 摘要 / 身份 / as_of: 不满足即 fail-closed ─────────────────────────


def test_missing_or_malformed_digest_and_instrument_fail_closed(monkeypatch, auth_headers):
    client = _client(monkeypatch, _Feed({_SYMBOL: CENTRE_BARS}))
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]

    # 缺摘要 → 422 (Query 必填), 且错误响应不含任何结构字段
    missing = client.get(
        f"/api/klines/{_SYMBOL}/chan-structure",
        params={"market": "CN", "instrument": "stock", "interval": "1d", "days": 120},
        headers=auth_headers,
    )
    assert missing.status_code == 422, missing.text
    assert "segments" not in missing.text and "operational_centre" not in missing.text

    # 截短/大写/非十六进制 → 422, 不"补齐"
    for bad in (digest[:16], digest.upper(), "z" * 64, ""):
        r = _structure(client, auth_headers, bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)

    # 身份不明确: auto / 缺失
    assert _structure(client, auth_headers, digest, instrument="auto").status_code == 422
    no_instrument = client.get(
        f"/api/klines/{_SYMBOL}/chan-structure",
        params={"market": "CN", "interval": "1d", "days": 120, "snapshot": digest},
        headers=auth_headers,
    )
    assert no_instrument.status_code == 422, no_instrument.text


def test_declared_instrument_contradicting_the_snapshot_is_refused(monkeypatch, auth_headers):
    from src.web.api import klines as kl

    client = _client(monkeypatch, _Feed({_SYMBOL: CENTRE_BARS}))
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]

    # 调用方声明 index, 但服务端响应自述是 stock → 409 (同码指数/个股不互证)
    monkeypatch.setattr(
        kl, "get_klines",
        lambda **kwargs: {
            "symbol": _SYMBOL, "market": "CN", "interval": "1d",
            "klines": [{"date": "2026-01-02", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}],
            "is_index": False,
        },
    )
    r = _structure(client, auth_headers, digest, instrument="index")
    assert r.status_code == 409, r.text
    assert "segments" not in r.text and "operational_centre" not in r.text

    # 身份标记畸形(字符串 "true") → 409, 不猜成 stock/index
    monkeypatch.setattr(
        kl, "get_klines",
        lambda **kwargs: {
            "symbol": _SYMBOL, "market": "CN", "interval": "1d",
            "klines": [{"date": "2026-01-02", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}],
            "is_index": "true",
        },
    )
    broken = _structure(client, auth_headers, digest)
    assert broken.status_code == 409, broken.text
    assert "segments" not in broken.text and "operational_centre" not in broken.text


def test_snapshot_drift_after_digest_never_returns_half_a_structure(monkeypatch, auth_headers):
    from src.web.api import klines as kl

    feed = _Feed({_SYMBOL: CENTRE_BARS})
    client = _client(monkeypatch, feed)
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]

    # 同一 worker 内仍复用图表那份响应 → 200(摘要仍匹配, 不二次取数)
    drift_bars = list(CENTRE_BARS)
    last = drift_bars[-1]
    drift_bars[-1] = KlineData(date=last.date, open=last.open, close=last.close + 1e-6,
                               high=last.high, low=last.low, volume=last.volume)
    feed.series[_SYMBOL] = drift_bars
    ok = _structure(client, auth_headers, digest)
    assert ok.status_code == 200, ok.text
    assert ok.json()["segments"]["counts"]["confirmed"] == 3

    # 缓存过期 / 换 worker → 重算摘要不同 → 409, 且响应里没有半份线段/中枢
    kl._kl_cache.clear()
    drifted = _structure(client, auth_headers, digest)
    assert drifted.status_code == 409, drifted.text
    assert "刷新" in drifted.json()["detail"]
    assert "segments" not in drifted.text and "operational_centre" not in drifted.text


def test_cross_symbol_digest_is_refused_and_structures_do_not_mix(monkeypatch, auth_headers):
    feed = _Feed({_SYMBOL: CENTRE_BARS, _OTHER: _mirror(CENTRE_BARS)})
    client = _client(monkeypatch, feed)
    digest_a = _chart(client, auth_headers, symbol=_SYMBOL)["chan_snapshot"]["digest"]
    digest_b = _chart(client, auth_headers, symbol=_OTHER)["chan_snapshot"]["digest"]
    assert digest_a != digest_b

    a = _structure(client, auth_headers, digest_a, symbol=_SYMBOL)
    b = _structure(client, auth_headers, digest_b, symbol=_OTHER)
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    body_a, body_b = a.json(), b.json()
    assert body_a["symbol"] == _SYMBOL and body_b["symbol"] == _OTHER
    # 各自的中枢来自各自的快照: 镜像序列的区间也是镜像的
    assert center_zone(body_a["operational_centre"]) == (20.92, 24.96)
    assert center_zone(body_b["operational_centre"]) == (60.0 - 24.96, 60.0 - 20.92)
    assert body_a["segments"]["input_digest"] != body_b["segments"]["input_digest"]

    # A 的摘要打 B → 409(不跨标的互证), 错误里没有结构
    crossed = _structure(client, auth_headers, digest_a, symbol=_OTHER)
    assert crossed.status_code == 409, crossed.text
    assert "segments" not in crossed.text and "operational_centre" not in crossed.text


def test_insufficient_bars_are_reported_without_inventing_a_centre(monkeypatch, auth_headers):
    single = [KlineData(date="2026-11-16", open=10.0, close=10.5, high=11.0, low=9.5, volume=1000.0)]
    body = _body(monkeypatch, auth_headers, single)
    assert body["fractals"] == [] and body["pens"] == []
    assert body["segments"]["confirmed"] == []
    assert body["segments"]["counts"]["total"] == 0
    centre = body["operational_centre"]
    assert centre["status"] == "not_enough_confirmed_segments"
    assert centre["zone"] is None


# ── ③ payload 构建器: no_centre 是"实测无交集", 不是"数据不足" ───────────


def test_measured_absence_is_reported_as_no_centre_not_as_not_enough():
    segment, result = _no_centre_result()
    assert result.status == "no_centre"
    assert len(segment.confirmed_segments) == 3

    payload = chan.build_operational_centre_payload(result)
    assert payload["status"] == "no_centre"
    assert payload["absence_reason"] == "zero_width_intersection"
    assert payload["zone"] is None and payload["confirmed_on"] is None
    assert payload["confirmed_prefix_length"] == 3
    assert payload["chain_complete"] is True
    assert "实测" in payload["status_note"]
    assert "不是缺少数据" in payload["status_note"]
    # 与"不够"明确分开
    assert "不等于" not in payload["status_note"]


def test_payload_builders_pass_kernel_labels_through_verbatim(monkeypatch, auth_headers):
    body = _body(monkeypatch, auth_headers, CENTRE_BARS)
    centre = body["operational_centre"]

    _, result = _no_centre_result()
    rebuilt = chan.build_operational_centre_payload(result)
    assert rebuilt["basis"] == result.basis == BASIS
    assert rebuilt["strictness"] == result.strictness == STRICTNESS
    assert rebuilt["status"] == result.status
    assert rebuilt["chain_complete"] == result.chain_complete
    assert [i["reason"] for i in rebuilt["issues"]] == [i.reason for i in result.issues]
    assert [i["blocking"] for i in rebuilt["issues"]] == [i.blocking for i in result.issues]
    assert list(rebuilt["limitations"]) == list(result.limitations)

    # 标签绝不升级: 没有 strict/zhongshu 口径, 也没有买卖点/交易信号字段
    assert centre["strictness"] == "operational_not_strict"
    assert centre["basis"] == "segment_a0"
    for forbidden in ("zhongshu", "strict_centre", "buy_sell_point", "trading_signal", "signal"):
        assert forbidden not in body
        assert forbidden not in body["segments"]
        assert forbidden not in centre
    assert "buy_sell_point" in body["not_implemented"]
    assert "strict_centre_18th_lesson_definition" in body["not_implemented"]
    assert "segment_case2_gap" in body["not_implemented"]


# ── ④ 旧响应兼容 ────────────────────────────────────────────────────────


_OLD_KEYS = {
    "symbol", "market", "instrument", "instrument_basis", "interval", "rules_version",
    "snapshot_digest", "as_of_day", "omitted_latest_reason", "source", "adjustment",
    "finality", "fractals", "counts", "not_implemented", "disclaimer",
    "pens", "pen_rules_version", "pen_counts", "pen_disclaimer",
}
_NEW_KEYS = {"segments", "operational_centre", "segment_disclaimer"}


def test_old_fractal_and_pen_fields_are_untouched(monkeypatch, auth_headers):
    body = _body(monkeypatch, auth_headers, CENTRE_BARS)

    assert set(body) == _OLD_KEYS | _NEW_KEYS
    assert set(body["fractals"][0]) == {
        "kind", "status", "position_day", "position_price", "confirmed_on"
    }
    assert set(body["pens"][0]) == {
        "direction", "status", "confirmed_on", "index_gap", "kernel_version", "start", "end"
    }
    assert body["pen_rules_version"] == PEN_KERNEL_VERSION
    assert body["rules_version"] == chan.CHAN_FRACTAL_RULES_VERSION
    assert body["adjustment"] == "unknown" and body["finality"] == "unverified"
    # 新增字段都是"可选且带版本号": 旧客户端忽略它们不受影响
    assert body["segments"]["schema"] and body["operational_centre"]["schema"]
    # 线段/中枢不再谎称"未实现"
    assert "segment" not in body["not_implemented"]
    assert "centre" not in body["not_implemented"]


# ── ⑤ DB 隔离: 裸跑真库必须变红 ─────────────────────────────────────────


def test_suite_uses_isolated_scratch_sqlite(auth_headers):
    from src.web.database import SessionLocal

    # 进程环境里不该残留生产库变量(conftest 显式 pop + 指向 scratch)
    for name in ("DATABASE_URL", "DATABASE_URL_WRITE", "DATABASE_URL_READ",
                 "DATABASE_URL_MIGRATE"):
        assert name not in os.environ, f"{name} 仍在环境里, 可能连到生产库"

    data_dir = Path(os.environ["DATA_DIR"]).resolve()
    assert data_dir.name.startswith("sida_test_data_"), f"DATA_DIR 不是临时目录: {data_dir}"

    db = SessionLocal()
    try:
        url = str(db.get_bind().url)
    finally:
        db.close()
    assert url.startswith("sqlite"), f"测试库必须落在隔离 SQLite, 实际: {url.split('://')[0]}"
    db_path = Path(url.split("sqlite:///", 1)[-1]).resolve()
    assert any(p.name.startswith("sida_test_data_") for p in db_path.parents), (
        f"测试库不在临时目录内: {db_path}"
    )
    repo_data = (Path(__file__).resolve().parent.parent / "data").resolve()
    assert repo_data not in db_path.parents, f"测试碰了仓库真库: {db_path}"
