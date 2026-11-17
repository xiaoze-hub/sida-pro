"""只读缠论分型端点: 与图表响应严格绑定(2026-11-17)。

为什么需要这组契约
------------------
用户的图是 `GET /api/klines/{symbol}` 的一次响应, 分型是另一个请求算的。中间可能:
跨 worker(各自 60s 缓存)、跨一次数据源漂移、或前端把 symbol 记错。若结构端点只按
symbol/market/instrument 重新取数, 它算的就不是"这张图的缠论结构"。

本文件钉住:
  ① 图表日线响应必须带内容摘要(`chan_snapshot.digest`), 且摘要绑定
     symbol/market/instrument/interval + 每根 bar 的日期与**完整** OHLC + 顺序;
  ② 结构端点**必须**回传该摘要; 服务端用同一 `get_klines` 的返回重算, 不等 → 409;
  ③ 声明 instrument 不算已验证身份: 强制显式传 instrument 并用响应 `is_index` 反查,
     同码指数/个股(000001)不互证; auto / 非 CN / 非 1d 一律 422;
  ④ 重复或乱序日期 → 422(不自动去重); 当日 bar 不参与计算(沿用 adapter);
  ⑤ 未登录 → 401; 上游异常不回显异常原文/DSN;
  ⑥ 响应只有分型(blocked/candidate/confirmed + 发生日/确认日 + 规则版本), 不称中枢。

时间用固定 NOW 钉住(monkeypatch `chan_snapshot._now`), 不随运行日期漂移。
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.collectors.kline_collector import KlineData
from src.web.api import chan_snapshot as chan
from src.web.api.auth import get_current_user

#: 2026-11-17 是周二(盘中), 前 6 根是已收盘的 11-09..11-16。
NOW = datetime(2026, 11, 17, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TODAY = "2026-11-17"
COMPLETED = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"]

USER_ID = "c0ffee00-1111-4222-8333-chanapi00001"
USER_NAME = "chan-api-contract"
assert len(USER_ID) == 36, "users.id 是 VARCHAR(36), 测试种子必须同宽"

#: (open, close, high, low) —— 单调上去再下来, 使 11-11 形成唯一确认顶分型。
_OHLC = [
    (9.2, 9.8, 10.0, 9.0),
    (9.8, 10.8, 11.0, 9.5),
    (10.8, 11.8, 12.0, 10.5),
    (11.0, 10.4, 11.2, 10.0),
    (10.4, 11.3, 11.5, 9.8),
    (10.7, 9.4, 10.8, 9.2),
]
_TODAY_OHLC = (9.4, 10.5, 11.0, 9.3)


def _k(day: str, ohlc: tuple[float, float, float, float]) -> KlineData:
    open_, close, high, low = ohlc
    return KlineData(date=day, open=open_, close=close, high=high, low=low, volume=1000.0)


def _copy(bar: KlineData) -> KlineData:
    return KlineData(date=bar.date, open=bar.open, close=bar.close,
                     high=bar.high, low=bar.low, volume=bar.volume)


def _completed_bars() -> list[KlineData]:
    return [_k(day, ohlc) for day, ohlc in zip(COMPLETED, _OHLC)]


def _bars_with_today() -> list[KlineData]:
    return _completed_bars() + [_k(TODAY, _TODAY_OHLC)]


class _Feed:
    """可变的 `_pg_klines` 替身: 改 ``bars`` 即模拟另一份数据源/另一个 worker。"""

    def __init__(self, bars: list[KlineData]):
        self.bars = bars

    def __call__(self, symbol, market_code, days):
        return ([_copy(bar) for bar in self.bars], self.bars[-1].date)


def _index_payload() -> dict:
    rows = [
        [day, f"{o}", f"{c}", f"{h}", f"{lo}", "1000"]
        for day, (o, c, h, lo) in zip(COMPLETED, _OHLC)
    ]
    return {"data": {"sh000001": {"day": rows}}}


@pytest.fixture()
def auth_headers():
    """真实 JWT + 真实 users 行(隔离 SQLite); 不覆盖 get_current_user 依赖。"""
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
        from tests.conftest import purge_users

        purge_users(db, ids=[USER_ID])
        db.commit()
    finally:
        db.close()


def _client(monkeypatch, feed: _Feed, *, with_index: bool = False) -> TestClient:
    from src.web.api import klines as kl

    monkeypatch.setattr(kl, "_pg_klines", feed)
    monkeypatch.setattr(kl, "_finalize_daily_bars", lambda bars, *a, **k: (bars, None))
    monkeypatch.setattr(kl, "_kl_cache", {})
    monkeypatch.setattr(chan, "_now", lambda: NOW)
    if with_index:
        monkeypatch.setattr(
            "src.collectors.market_http.market_get", lambda url, **kwargs: _index_payload()
        )
    app = FastAPI()
    # 与 app.py 的 klines 挂载同形: 路由级鉴权 + 端点自身依赖(双重门禁)。
    app.include_router(kl.router, prefix="/api/klines", dependencies=[Depends(get_current_user)])
    return TestClient(app)


def _chart(client: TestClient, headers: dict, *, symbol: str = "600000",
           instrument: str = "stock") -> dict:
    r = client.get(
        f"/api/klines/{symbol}",
        params={"market": "CN", "days": 60, "interval": "1d", "instrument": instrument},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _structure(client: TestClient, headers: dict, digest: str, **overrides):
    params = {"market": "CN", "instrument": "stock", "interval": "1d", "days": 60,
              "snapshot": digest}
    params.update(overrides)
    return client.get("/api/klines/600000/chan-structure", params=params, headers=headers)


# ── ① 图表响应带摘要 ────────────────────────────────────────────────────


def test_chart_daily_response_carries_full_length_digest(monkeypatch, auth_headers):
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    body = _chart(client, auth_headers)
    snap = body["chan_snapshot"]
    assert snap["schema"] == chan.DIGEST_SCHEMA
    assert snap["instrument"] == "stock"
    assert snap["interval"] == "1d"
    assert snap["bars"] == 7 and len(body["klines"]) == 7
    # 完整 sha256 十六进制, 不截短(截短会抬高碰撞概率)
    assert len(snap["digest"]) == 64
    int(snap["digest"], 16)
    assert snap["digest"] == snap["digest"].lower()
    # 摘要覆盖的是"图表看到的那份响应", 含当日 bar
    assert snap["as_of_day"] == TODAY


def test_weekly_response_has_no_digest(monkeypatch, auth_headers):
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    r = client.get("/api/klines/600000",
                   params={"market": "CN", "days": 60, "interval": "1w"},
                   headers=auth_headers)
    assert r.status_code == 200, r.text
    assert "chan_snapshot" not in r.json()


# ── ② 摘要本身: 精度 / 顺序 / 身份绑定 ──────────────────────────────────


def _digest(bars, **over):
    kwargs = dict(symbol="600000", market="CN", instrument="stock", interval="1d", bars=bars)
    kwargs.update(over)
    return chan.compute_snapshot_digest(**kwargs)


def _bar(day: str, close: float) -> dict:
    return {"date": day, "open": 10.0, "high": 10.5, "low": 9.5, "close": close}


def test_digest_distinguishes_fifth_decimal_place():
    """%.4f 截断会把这两根压成同一摘要 —— 那就是"换了一根K线"被当成"快照未变"。"""
    a = _digest([_bar("2026-11-16", 10.00001)])
    b = _digest([_bar("2026-11-16", 10.00002)])
    assert a != b


def test_digest_binds_order_identity_and_ignores_irrelevant_keys():
    bars = [_bar("2026-11-16", 10.1), _bar("2026-11-17", 10.2)]
    ordered = _digest(bars)
    assert ordered == _digest(bars)  # 稳定
    assert ordered != _digest(list(reversed(bars)))  # 顺序被打乱即不同
    assert ordered != _digest(bars, symbol="000001")
    assert ordered != _digest(bars, instrument="index")
    assert ordered != _digest(bars, interval="1w")
    assert ordered != _digest(bars, market="HK")
    # 图表盘中会给末根挂 partial/source/as_of/amount —— 不得改变摘要
    noisy = [dict(bars[0]), {**bars[1], "partial": True, "source": "live", "as_of": "x", "amount": 1.0}]
    assert _digest(noisy) == ordered
    # int 与 float 同值必须同摘要(PG Decimal → JSON float 两条路)
    assert _digest([{**bars[0], "close": 10}]) == _digest([{**bars[0], "close": 10.0}])


# ── ③ 正常路径 ──────────────────────────────────────────────────────────


def test_structure_matches_chart_snapshot_and_never_uses_current_bar(monkeypatch, auth_headers):
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    chart = _chart(client, auth_headers)
    digest = chart["chan_snapshot"]["digest"]
    # 图表响应里含当日 bar; 结构侧必须把它排除
    assert chart["klines"][-1]["date"] == TODAY

    r = _structure(client, auth_headers, digest)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["snapshot_digest"] == digest
    assert body["instrument"] == "stock"
    assert body["interval"] == "1d"
    assert body["rules_version"] == chan.CHAN_FRACTAL_RULES_VERSION == "chan-fractal-v1"
    assert body["as_of_day"] == "2026-11-16"  # 当日 bar 不参与
    assert body["omitted_latest_reason"] == "current_day_completion_unverified"
    assert all(f["position_day"] != TODAY for f in body["fractals"])
    assert all(f["confirmed_on"] != TODAY for f in body["fractals"])

    # 唯一确认顶分型在 11-11, 确认于 11-16(右邻被下一根封口那天)
    confirmed = [f for f in body["fractals"] if f["status"] == "confirmed"]
    assert [(f["kind"], f["position_day"], f["confirmed_on"]) for f in confirmed] == [
        ("top", "2026-11-11", "2026-11-16")
    ]
    # 若把当日 bar 算进来, 11-16 会出现一个 candidate bottom —— 必须没有
    assert body["counts"]["candidate"] == 0
    assert body["counts"]["confirmed"] == 1
    assert body["counts"]["total"] == len(body["fractals"])

    # 响应只含分型相关字段: 不许夹带合并K线/中枢之类"看起来更强"的结构
    assert set(body) == {
        "symbol", "market", "instrument", "interval", "rules_version", "snapshot_digest",
        "as_of_day", "omitted_latest_reason", "source", "adjustment", "finality",
        "fractals", "counts", "not_implemented", "disclaimer",
    }
    assert set(body["fractals"][0]) == {
        "kind", "status", "position_day", "position_price", "confirmed_on"
    }
    assert all(f["status"] in {"blocked", "candidate", "confirmed"} for f in body["fractals"])
    assert "centre" in body["not_implemented"]  # 中枢未实现, 明写而非暗示
    assert body["adjustment"] == "unknown"  # 复权口径无法证明 → 如实 unknown
    assert body["finality"] == "unverified"


def test_structure_reuses_exact_cached_chart_response_and_detects_drift(monkeypatch, auth_headers):
    feed = _Feed(_bars_with_today())
    client = _client(monkeypatch, feed)
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]

    # 数据源已漂移, 但进程内缓存还是图表那份 → 必须用同一份响应(200), 不是重新取数
    feed.bars = _bars_with_today()
    feed.bars[-2] = _k(COMPLETED[-1], (10.7, 9.400001, 10.8, 9.2))  # 第 6 位小数
    ok = _structure(client, auth_headers, digest)
    assert ok.status_code == 200, ok.text

    # 缓存过期/换到另一个 worker → 重算出的摘要不同 → 409, 不允许静默各算一套
    from src.web.api import klines as kl

    kl._kl_cache.clear()
    drift = _structure(client, auth_headers, digest)
    assert drift.status_code == 409
    assert "刷新" in drift.json()["detail"]


# ── ④ 快照不匹配 / 参数非法 ─────────────────────────────────────────────


def test_tampered_digest_conflicts(monkeypatch, auth_headers):
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]
    tampered = ("0" if digest[0] != "0" else "1") + digest[1:]
    r = _structure(client, auth_headers, tampered)
    assert r.status_code == 409, r.text


def test_truncated_or_malformed_digest_is_rejected_not_repaired(monkeypatch, auth_headers):
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]
    for bad in (digest[:16], digest.upper(), "z" * 64, ""):
        r = _structure(client, auth_headers, bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)


def test_auto_instrument_is_rejected(monkeypatch, auth_headers):
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]
    r = _structure(client, auth_headers, digest, instrument="auto")
    assert r.status_code == 422


@pytest.mark.parametrize("overrides", [
    {"market": "HK"},
    {"market": "US"},
    {"interval": "1w"},
    {"interval": "5m"},
])
def test_non_cn_or_non_daily_is_rejected(monkeypatch, auth_headers, overrides):
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]
    r = _structure(client, auth_headers, digest, **overrides)
    assert r.status_code == 422, r.text


def test_duplicate_and_unordered_dates_fail_safe(monkeypatch, auth_headers):
    for broken in (
        [_k(COMPLETED[0], _OHLC[0])] + [_k(COMPLETED[1], _OHLC[1])] * 2 + [_k(COMPLETED[2], _OHLC[2])],
        [_k(COMPLETED[1], _OHLC[1]), _k(COMPLETED[0], _OHLC[0]), _k(COMPLETED[2], _OHLC[2])],
    ):
        client = _client(monkeypatch, _Feed(broken))
        digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]
        r = _structure(client, auth_headers, digest)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert "重复" in detail or "乱序" in detail
        # 不许"帮客户去重/排序"后仍然算出一套分型
        assert "fractals" not in r.json()


# ── ⑤ 身份: 鉴权 / 同码指数 / 上游异常不外泄 ─────────────────────────────


def test_missing_token_is_401_on_the_independent_route(monkeypatch):
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    r = client.get("/api/klines/600000/chan-structure",
                   params={"market": "CN", "instrument": "stock", "interval": "1d",
                           "snapshot": "0" * 64})
    assert r.status_code == 401, r.text
    body = json.dumps(r.json())
    assert "Traceback" not in body and "sqlite" not in body.lower()


def test_index_and_stock_share_a_code_but_never_share_a_snapshot(monkeypatch, auth_headers):
    client = _client(monkeypatch, _Feed(_bars_with_today()), with_index=True)
    assert chan.compute_snapshot_digest(
        symbol="000001", market="CN", instrument="stock", interval="1d", bars=[_bar("2026-11-16", 10.0)]
    ) != chan.compute_snapshot_digest(
        symbol="000001", market="CN", instrument="index", interval="1d", bars=[_bar("2026-11-16", 10.0)]
    )

    stock_digest = _chart(client, auth_headers, symbol="000001", instrument="stock")["chan_snapshot"]["digest"]
    index_digest = _chart(client, auth_headers, symbol="000001", instrument="index")["chan_snapshot"]["digest"]
    assert stock_digest != index_digest

    # 真指数快照 → 指数结构 200(证明指数分支真的走通, 不是靠"都失败"通过)
    ok = client.get("/api/klines/000001/chan-structure",
                    params={"market": "CN", "instrument": "index", "interval": "1d",
                            "days": 60, "snapshot": index_digest},
                    headers=auth_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["instrument"] == "index"

    # 拿个股快照去算指数 → 409; 反向同样 409
    r1 = client.get("/api/klines/000001/chan-structure",
                    params={"market": "CN", "instrument": "index", "interval": "1d",
                            "days": 60, "snapshot": stock_digest},
                    headers=auth_headers)
    assert r1.status_code == 409, r1.text
    r2 = client.get("/api/klines/000001/chan-structure",
                    params={"market": "CN", "instrument": "stock", "interval": "1d",
                            "days": 60, "snapshot": index_digest},
                    headers=auth_headers)
    assert r2.status_code == 409, r2.text


def test_upstream_failure_does_not_leak_exception_or_credentials(monkeypatch, auth_headers):
    from src.web.api import klines as kl

    def _boom(*args, **kwargs):
        raise RuntimeError("postgresql://svc:s3cr3t-pw@10.1.2.3:5432/sida_prod")

    client = _client(monkeypatch, _Feed(_bars_with_today()))
    monkeypatch.setattr(kl, "_pg_klines", _boom)
    r = _structure(client, auth_headers, "0" * 64)
    assert r.status_code == 503, r.text
    text = json.dumps(r.json(), ensure_ascii=False)
    assert "s3cr3t-pw" not in text
    assert "10.1.2.3" not in text
    assert "Traceback" not in text
    assert "postgresql://" not in text


# ── ⑥ 真实 app: 路由确实挂在 /api/klines 下, 不被 /{symbol} 吞掉 ──────────


def test_route_is_registered_on_the_real_app_and_guarded():
    from src.web.app import app

    paths = app.openapi()["paths"]  # 仓库既有写法: 顺带验证路由签名能生成 OpenAPI
    assert "/api/klines/{symbol}/chan-structure" in paths
    assert "get" in {method.lower() for method in paths["/api/klines/{symbol}/chan-structure"]}
    # 影子路由检查: /{symbol} 只吃单段, 两段路径必须命中分型端点(401 而不是 404/405)
    client = TestClient(app)
    r = client.get("/api/klines/600000/chan-structure",
                   params={"market": "CN", "instrument": "stock", "interval": "1d",
                           "snapshot": "0" * 64})
    assert r.status_code == 401, r.text
    # 反证: 不存在的两段路径必须是 404 —— 证明上面的 401 来自本路由的鉴权,
    # 不是某种通配路由把任意两段路径都挡成 401。
    miss = client.get("/api/klines/600000/not-a-route")
    assert miss.status_code == 404, miss.text
