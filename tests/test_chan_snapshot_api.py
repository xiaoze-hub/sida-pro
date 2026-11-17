"""只读缠论分型端点: 与图表响应严格绑定(2026-11-17)。

为什么需要这组契约
------------------
用户的图是 `GET /api/klines/{symbol}` 的一次响应, 分型是另一个请求算的。中间可能:
跨 worker(各自 60s 缓存)、跨一次数据源漂移、或前端把 symbol 记错。若结构端点只按
symbol/market/instrument 重新取数, 它算的就不是"这张图的缠论结构"。

本文件钉住:
  ① 图表日线响应必须带内容摘要(`chan_snapshot.digest`), 且摘要绑定
     symbol/market/instrument/interval + 每根 bar 的日期与**完整** OHLC + 顺序
     + 会影响结构/归属的元信息(每根 partial 标记、每根 source、响应 source/today_bar);
  ② 结构端点**必须**回传该摘要; 服务端用同一 `get_klines` 的返回重算, 不等 → 409;
  ③ instrument 是**必填** Query 参数(缺参 422, 不再默认 stock); 声明不算已验证身份:
     用响应 `is_index` 反查, 同码指数/个股(000001)不互证; 畸形(字符串/数字/None)
     → 409 而不是静默当 stock; 快照缺 `is_index` 的老响应记 instrument_basis=
     declared_path(依赖显式 stock 调用路径, 不是独立验证); auto / 非 CN / 非 1d 一律 422;
  ④ 重复或乱序日期 → 422(不自动去重); 当日 bar 不参与计算(沿用 adapter);
     未来日期 bar → 422;
  ⑤ 未登录 → 401, 账号禁用/注销 → 403; 上游异常不回显异常原文/DSN;
  ⑥ 响应只有分型(blocked/candidate/confirmed + 发生日/确认日 + 固定规则版本), 不称中枢;
  ⑦ 笔(pen)由 `chan_pen.pen_from_snapshot` 在同一份**已验摘要**的同一个 structure
     snapshot 上算出: 一次 `get_klines`、一次 `build_chan_structure`、同一个对象喂给分型
     与笔; 结果给端点位置(合并索引+日期+价格)/状态/起点终点/confirmed_on/规则版本,
     `pen_rules_version` 与每笔 `kernel_version` 同源; 笔候选与已确认都是**规则几何**,
     数据最终性/复权仍 unknown, 不是交易信号; 内核/自洽性校验任何不符 → 409 并整份失败
     (不返回半份结果, 不以空结果冒充成功)。

规则版本固定为 `chan-fractal-inclusive-sealed-v1`(包含式合并 + 右邻封口才确认),
响应、快照、文档三处必须同值 —— 改规则就改版本号, 不许同号换语义。
笔规则版本固定为 `chan-pen-v1`(间距 4 根合并K线 + 顶包络高于底包络 + 反向封口才冻结)。

时间用固定 NOW 钉住(monkeypatch `chan_snapshot._now`), 不随运行日期漂移。
DB 隔离: 用例只碰 conftest 建在临时 DATA_DIR 下的 SQLite, 见
`test_suite_uses_isolated_scratch_sqlite` —— 没有隔离就整轮变红, 不许"裸跑真库"。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.collectors.kline_collector import KlineData
from src.core.chan_pen import MIN_INDEX_GAP, PEN_KERNEL_VERSION
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


def _loaded_conftest():
    """取**已加载**的 conftest 模块(pytest 以顶层 `conftest` 名加载它)。

    为什么不用 ``from tests.conftest import purge_users``: 那会按另一个模块名**第二次
    执行** conftest 的模块体 —— 它内部无条件 ``DATA_DIR = mkdtemp(...)``, 于是同进程里
    DATA_DIR/SIDA_DATA_DIR 被改指到**另一个**临时目录, 而 ``SIDA_DB_URL`` 仍停在第一次
    setdefault 的值(库在旧目录), 表现为"DATA_DIR 说 A、库在 B"的隔离分裂。复用一个已
    加载的模块才能保证整轮只有一个临时目录。
    """
    for name in ("conftest", "tests.conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "purge_users"):
            return module
    raise RuntimeError("pytest 尚未加载 conftest, 无法复用 purge_users")


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
        _loaded_conftest().purge_users(db, ids=[USER_ID])
        db.commit()
    finally:
        db.close()


def _client(monkeypatch, feed, *, with_index: bool = False) -> TestClient:
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


def _structure(client: TestClient, headers: dict, digest: str, *, symbol: str = "600000", **overrides):
    params = {"market": "CN", "instrument": "stock", "interval": "1d", "days": 60,
              "snapshot": digest}
    params.update(overrides)
    return client.get(f"/api/klines/{symbol}/chan-structure", params=params, headers=headers)


# ── 笔样例: 干净锯齿, 每根原始K线各自成一根合并K线(合并索引 == 原始索引) ──────
#
# 底@1(01-02) → 顶@5(01-06) → 底@9(01-10) → 顶@13(01-14), 中心间距恰为 4
# (v1 的 MIN_INDEX_GAP)。四个分型都已确认, 于是:
#   笔① 上 已确认 (起点 01-02@4.0 → 终点 01-06@8.5), confirmed_on=01-12
#   笔② 下 已确认 (01-06@8.5 → 01-10@5.0),              confirmed_on=01-16
#   笔③ 上 候选   (01-10@5.0 → 01-14@9.0),              终点仍可漂移
# 全部在 NOW(2026-11-17) 之前 → 不会被当日 bar 截断, 也没有未来日期问题。
_PEN_START = date(2026, 1, 1)
_PEN_HIGHS_LOWS = [
    (6.0, 5.0),   # 0
    (5.0, 4.0),   # 1  底
    (5.5, 4.5),   # 2
    (6.5, 5.5),   # 3
    (7.5, 6.5),   # 4
    (8.5, 7.5),   # 5  顶
    (8.0, 7.0),   # 6
    (7.0, 6.0),   # 7
    (6.5, 5.5),   # 8
    (6.0, 5.0),   # 9  底
    (7.0, 6.0),   # 10
    (7.5, 6.5),   # 11
    (8.0, 7.0),   # 12
    (9.0, 8.0),   # 13 顶
    (8.5, 7.5),   # 14
    (9.5, 8.5),   # 15 封口 14 号合并K线
]
_PEN_DAYS = [(_PEN_START + timedelta(days=i)).isoformat() for i in range(len(_PEN_HIGHS_LOWS))]


def _pen_bars() -> list[KlineData]:
    """(high, low) → open=low / close=high, OHLC 合法且每根各自成合并K线。"""
    return [_k(day, (low, high, high, low)) for day, (high, low) in zip(_PEN_DAYS, _PEN_HIGHS_LOWS)]


def _pen_client(monkeypatch) -> TestClient:
    return _client(monkeypatch, _Feed(_pen_bars()))


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
    # 与结构无关的展示字段(量/额/时刻)不改变摘要 —— 摘要描述的是价格结构输入
    noisy = [dict(bars[0]), {**bars[1], "as_of": "x", "amount": 1.0, "volume": 999.0}]
    assert _digest(noisy) == ordered
    # int 与 float 同值必须同摘要(PG Decimal → JSON float 两条路)
    assert _digest([{**bars[0], "close": 10}]) == _digest([{**bars[0], "close": 10.0}])


def test_digest_binds_partial_and_source_because_they_change_meaning():
    """同样 OHLC 但 partial/source 变了 → 不是同一份快照: 一个"可能未定型/另一个来源"。

    旧 v1 摘要只看 OHLC, 于是"末根从已收盘变成盘中未定型"和"换了数据源"都能伪装成
    同一摘要, 结构端点会拿别人的数据算这张图的分型。v2 必须把它们编进字节流。
    """
    bars = [_bar("2026-11-16", 10.1)]
    base = _digest(bars)
    assert base != _digest([{**bars[0], "partial": True}])
    # partial=False 与"没有这个键"语义相同(adapter 只认 `is True`) → 同摘要
    assert base == _digest([{**bars[0], "partial": False}])
    assert base != _digest([{**bars[0], "source": "live"}])
    assert base != _digest(bars, source="pg_klines_hypertable")
    assert base != _digest(bars, today_bar="live")
    assert base != _digest(bars, today_bar="missing")


@pytest.mark.parametrize("bad", ["true", 1, 0, None, 1.0])
def test_malformed_partial_flag_is_rejected_not_ignored(bad):
    """`partial="true"` 在 adapter 里会 `is True` 判假 → 历史未定型 bar 被当已收盘。

    摘要层必须拒绝畸形值(ValueError → 端点 409), 不许猜成 False。
    """
    with pytest.raises(ValueError):
        _digest([{**_bar("2026-11-16", 10.1), "partial": bad}])


def test_malformed_source_is_rejected():
    with pytest.raises(ValueError):
        _digest([{**_bar("2026-11-16", 10.1), "source": 123}])
    with pytest.raises(ValueError):
        _digest([_bar("2026-11-16", 10.1)], source=123)


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
    assert body["instrument_basis"] == "response_flag"
    assert body["interval"] == "1d"
    # 规则版本: 文档固定值, 响应/快照同源(改规则必须换版本号)
    assert body["rules_version"] == chan.CHAN_FRACTAL_RULES_VERSION == "chan-fractal-inclusive-sealed-v1"
    assert chart["chan_snapshot"]["rules_version"] == body["rules_version"]
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

    # 响应只含分型/笔相关字段: 不许夹带合并K线/中枢之类"看起来更强"的结构
    assert set(body) == {
        "symbol", "market", "instrument", "instrument_basis", "interval", "rules_version",
        "snapshot_digest", "as_of_day", "omitted_latest_reason", "source", "adjustment",
        "finality", "fractals", "counts", "not_implemented", "disclaimer",
        # 笔(pen): 同一快照上的规则几何; 线段/中枢仍未实现(见 not_implemented)
        "pens", "pen_rules_version", "pen_counts", "pen_disclaimer",
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


# ── ⑦ 摘要/规则版本常量: 文档固定值, 不许同号换语义 ────────────────────────


def test_schema_and_rules_version_are_fixed_and_bumped():
    # 摘要字节流变了(OHLC → OHLC+partial+source)就必须换 schema 版本
    assert chan.DIGEST_SCHEMA == "sida-chan-snapshot-v2"
    assert chan.CHAN_FRACTAL_RULES_VERSION == "chan-fractal-inclusive-sealed-v1"


# ── ⑧ instrument 必须显式: 必填 Query 参数, 不再默认 stock ────────────────


def test_instrument_query_param_is_required(monkeypatch, auth_headers):
    """默认 stock 会让"忘了传"被当成"我说了是股票" —— 必须 422, 不许落默认分支。"""
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]
    r = client.get("/api/klines/600000/chan-structure",
                   params={"market": "CN", "interval": "1d", "days": 60, "snapshot": digest},
                   headers=auth_headers)
    assert r.status_code == 422, r.text
    assert "fractals" not in r.text


def test_openapi_marks_instrument_required_and_documents_allowed_values():
    from src.web.app import app

    op = app.openapi()["paths"]["/api/klines/{symbol}/chan-structure"]["get"]
    params = {p["name"]: p for p in op["parameters"]}
    assert params["instrument"]["required"] is True
    assert "default" not in params["instrument"]  # 没有隐含默认值
    assert params["snapshot"]["required"] is True


# ── ⑨ 身份标记: 必须布尔; 畸形拒绝; 缺字段如实记 declared_path ──────────────


def test_malformed_is_index_is_rejected_not_silently_stock():
    """`is_index="true"` / 1 这类值不能被猜成 stock(那等于把指数当个股算)。"""
    legacy_stock = {"symbol": "600000", "market": "CN", "interval": "1d", "klines": []}
    assert chan.response_instrument(legacy_stock) == "stock"  # 缺字段: 历史 stock 契约
    assert chan.instrument_identity_basis(legacy_stock) == "declared_path"
    for bad in ("true", "false", 1, 0, None, 1.0, [], {}):
        with pytest.raises(ValueError):
            chan.response_instrument({"is_index": bad, "symbol": "600000"})
    assert chan.response_instrument({"is_index": True}) == "index"
    assert chan.response_instrument({"is_index": False}) == "stock"
    assert chan.instrument_identity_basis({"is_index": False}) == "response_flag"


def test_chart_stock_response_declares_boolean_identity(monkeypatch, auth_headers):
    """个股响应必须显式 `is_index: false`; 缺字段只能留给历史外来响应。"""
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    body = _chart(client, auth_headers)
    assert body["is_index"] is False
    assert body["chan_snapshot"]["instrument"] == "stock"
    assert body["chan_snapshot"]["instrument_basis"] == "response_flag"


def test_attach_records_unverified_identity_without_inventing_a_digest():
    legacy = {"symbol": "600000", "market": "CN", "interval": "1d",
              "klines": [_bar("2026-11-16", 10.1)]}
    snap = chan.attach_snapshot_digest(dict(legacy))["chan_snapshot"]
    # 缺 is_index: 身份来自"显式 stock 调用路径", 如实标注, 摘要仍可用于内容绑定
    assert snap["instrument"] == "stock"
    assert snap["instrument_basis"] == "declared_path"
    assert len(snap["digest"]) == 64

    malformed = {**legacy, "is_index": "true"}
    snap_bad = chan.attach_snapshot_digest(dict(malformed))["chan_snapshot"]
    assert snap_bad["digest"] is None
    assert snap_bad["instrument"] is None
    assert snap_bad["unavailable_reason"] == "instrument_flag_unverified"


def test_structure_endpoint_rejects_malformed_response_identity(monkeypatch, auth_headers):
    from src.web.api import klines as kl

    client = _client(monkeypatch, _Feed(_bars_with_today()))
    body = {"symbol": "600000", "market": "CN", "interval": "1d", "is_index": "true",
            "klines": [_bar(day, ohlc[1]) for day, ohlc in zip(COMPLETED, _OHLC)]}
    monkeypatch.setattr(kl, "get_klines", lambda **kwargs: body)
    r = _structure(client, auth_headers, "0" * 64)
    assert r.status_code == 409, r.text
    assert "fractals" not in r.text


def test_structure_endpoint_records_declared_path_for_legacy_stock(monkeypatch, auth_headers):
    from src.web.api import klines as kl

    legacy_bars = [
        {"date": day, "open": o, "high": h, "low": lo, "close": c}
        for day, (o, c, h, lo) in zip(COMPLETED, _OHLC)
    ]
    digest = chan.compute_snapshot_digest(
        symbol="600000", market="CN", instrument="stock", interval="1d", bars=legacy_bars
    )
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    # 响应里没有 is_index 键(历史契约) → 200, 但必须如实记 declared_path
    monkeypatch.setattr(kl, "get_klines", lambda **kwargs: {
        "symbol": "600000", "market": "CN", "interval": "1d", "klines": [dict(b) for b in legacy_bars],
    })
    r = _structure(client, auth_headers, digest)
    assert r.status_code == 200, r.text
    assert r.json()["instrument_basis"] == "declared_path"
    assert r.json()["instrument"] == "stock"


def test_cross_symbol_response_is_rejected(monkeypatch, auth_headers):
    from src.web.api import klines as kl

    client = _client(monkeypatch, _Feed(_bars_with_today()))
    monkeypatch.setattr(kl, "get_klines", lambda **kwargs: {
        "symbol": "000001", "market": "CN", "interval": "1d", "is_index": False,
        "klines": [{"date": "2026-11-16", "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.1}],
    })
    r = _structure(client, auth_headers, "0" * 64)
    assert r.status_code == 409, r.text


# ── ⑩ partial/source 漂移: 相同 OHLC 不同含义 → 409 ───────────────────────


def test_partial_flag_drift_with_identical_ohlc_conflicts(monkeypatch, auth_headers):
    """同一批 OHLC, 末根从"已收盘"变成"盘中未定型" → 不是同一份快照。"""
    from src.web.api import klines as kl

    bars = [
        {"date": day, "open": o, "high": h, "low": lo, "close": c}
        for day, (o, c, h, lo) in zip(COMPLETED, _OHLC)
    ]
    digest = chan.compute_snapshot_digest(
        symbol="600000", market="CN", instrument="stock", interval="1d", bars=bars
    )
    drifted = [dict(b) for b in bars]
    drifted[-1] = {**drifted[-1], "partial": True}  # OHLC 一模一样, 只是被标未定型
    monkeypatch.setattr(kl, "get_klines", lambda **kwargs: {
        "symbol": "600000", "market": "CN", "interval": "1d", "is_index": False,
        "source": "pg_klines_hypertable", "today_bar": "live", "klines": drifted,
    })
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    r = _structure(client, auth_headers, digest)
    assert r.status_code == 409, r.text


def test_source_change_with_identical_ohlc_conflicts(monkeypatch, auth_headers):
    from src.web.api import klines as kl

    bars = [
        {"date": day, "open": o, "high": h, "low": lo, "close": c}
        for day, (o, c, h, lo) in zip(COMPLETED, _OHLC)
    ]
    digest = chan.compute_snapshot_digest(
        symbol="600000", market="CN", instrument="stock", interval="1d", bars=bars,
        source="pg_klines_hypertable",
    )
    monkeypatch.setattr(kl, "get_klines", lambda **kwargs: {
        "symbol": "600000", "market": "CN", "interval": "1d", "is_index": False,
        "source": "tencent_web", "klines": [dict(b) for b in bars],
    })
    client = _client(monkeypatch, _Feed(_bars_with_today()))
    r = _structure(client, auth_headers, digest)
    assert r.status_code == 409, r.text


def test_malformed_partial_flag_on_the_wire_conflicts(monkeypatch, auth_headers):
    """响应里 `partial="true"`(字符串) → 摘要不可校验 → 409, 不许当已收盘算分型。"""
    from src.web.api import klines as kl

    client = _client(monkeypatch, _Feed(_bars_with_today()))
    bars = [
        {"date": day, "open": o, "high": h, "low": lo, "close": c}
        for day, (o, c, h, lo) in zip(COMPLETED, _OHLC)
    ]
    dashed = [dict(b) for b in bars]
    dashed[-1] = {**dashed[-1], "partial": "true"}
    monkeypatch.setattr(kl, "get_klines", lambda **kwargs: {
        "symbol": "600000", "market": "CN", "interval": "1d", "is_index": False,
        "klines": dashed,
    })
    r = _structure(client, auth_headers, "0" * 64)
    assert r.status_code == 409, r.text
    assert "fractals" not in r.text

    # 图表侧同样不产出摘要, 并注明是"元信息畸形"而不是"没有K线"
    snap = chan.attach_snapshot_digest({
        "symbol": "600000", "market": "CN", "interval": "1d", "is_index": False,
        "klines": dashed,
    })["chan_snapshot"]
    assert snap["digest"] is None
    assert snap["unavailable_reason"] == "bar_metadata_invalid"


# ── ⑪ 时间安全: 未来 bar 必须 422, 不是算出一套分型 ───────────────────────


def test_future_bar_is_rejected(monkeypatch, auth_headers):
    future = _completed_bars() + [_k("2026-11-18", (10.0, 12.0, 12.5, 9.9))]
    client = _client(monkeypatch, _Feed(future))
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]
    r = _structure(client, auth_headers, digest)
    assert r.status_code == 422, r.text
    assert "未来" in r.json()["detail"]
    assert "fractals" not in r.json()


# ── ⑫ 403: 账号被禁用 → 403(不是 401, 也不是 200) ──────────────────────────


def test_disabled_account_is_403_and_leaks_nothing(monkeypatch, auth_headers):
    from src.web.database import SessionLocal
    from src.web.models import User

    client = _client(monkeypatch, _Feed(_bars_with_today()))

    def _set_active(flag: bool) -> None:
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == USER_ID).first()
            user.is_active = flag
            db.commit()
        finally:
            db.close()

    _set_active(False)
    try:
        r = _structure(client, auth_headers, "0" * 64)
        assert r.status_code == 403, r.text
        text = json.dumps(r.json(), ensure_ascii=False)
        assert "sqlite" not in text.lower() and "Traceback" not in text
        assert "fractals" not in text
        assert "pens" not in text
    finally:
        _set_active(True)


# ── ⑬ 笔(pen): 同一份已验快照上的规则几何 ────────────────────────────────
#
# 契约: 笔由 `chan_pen.pen_from_snapshot` 在**同一个** structure snapshot 上算出
# (一次取数、一次 build_chan_structure、同一个对象), 端点位置/价格/状态/确认日/
# 规则版本全部给全; 内核拒绝或自身不自洽一律 409 整份失败, 不返回半份结果。

_PEN_FIELDS = {"direction", "status", "confirmed_on", "index_gap", "kernel_version", "start", "end"}
_ENDPOINT_FIELDS = {
    "kind", "merged_index", "position_day", "price", "bar_high", "bar_low",
    "revisions", "drift_days", "status", "confirmed_on",
}


def _pen_response(monkeypatch, auth_headers) -> tuple[TestClient, dict, dict]:
    client = _pen_client(monkeypatch)
    chart = _chart(client, auth_headers)
    digest = chart["chan_snapshot"]["digest"]
    r = _structure(client, auth_headers, digest)
    assert r.status_code == 200, r.text
    return client, chart, r.json()


def test_pens_are_returned_with_positions_status_and_rule_version(monkeypatch, auth_headers):
    _, _, body = _pen_response(monkeypatch, auth_headers)

    # 规则版本: 与内核同源, 每笔自带同一版本(不许"响应一个版本/笔里另一个版本")
    assert body["pen_rules_version"] == PEN_KERNEL_VERSION == "chan-pen-v1"
    assert MIN_INDEX_GAP == 4
    assert body["pens"], "样例必须产出非空笔, 否则本用例什么都没验"
    assert all(p["kernel_version"] == body["pen_rules_version"] for p in body["pens"])

    # 手推口径钉死: 两笔已确认 + 一笔候选(候选端点仍可漂移)
    assert [
        (p["direction"], p["status"], p["start"]["position_day"], p["start"]["price"],
         p["end"]["position_day"], p["end"]["price"], p["confirmed_on"])
        for p in body["pens"]
    ] == [
        ("up", "confirmed", "2026-01-02", 4.0, "2026-01-06", 8.5, "2026-01-12"),
        ("down", "confirmed", "2026-01-06", 8.5, "2026-01-10", 5.0, "2026-01-16"),
        ("up", "candidate", "2026-01-10", 5.0, "2026-01-14", 9.0, None),
    ]

    for pen in body["pens"]:
        assert set(pen) == _PEN_FIELDS
        assert set(pen["start"]) == _ENDPOINT_FIELDS
        assert set(pen["end"]) == _ENDPOINT_FIELDS
        # 笔的两端必须异性, 方向由起点类型唯一决定
        assert pen["start"]["kind"] != pen["end"]["kind"]
        assert pen["direction"] == ("up" if pen["start"]["kind"] == "bottom" else "down")
        # 端点位置: 合并K线索引 + 发生日 + 用到的价格 + 该合并K线的包络
        assert pen["start"]["merged_index"] < pen["end"]["merged_index"]
        assert pen["index_gap"] == pen["end"]["merged_index"] - pen["start"]["merged_index"]
        assert pen["index_gap"] >= MIN_INDEX_GAP
        assert pen["start"]["bar_low"] <= pen["start"]["price"] <= pen["start"]["bar_high"]
        assert pen["end"]["bar_low"] <= pen["end"]["price"] <= pen["end"]["bar_high"]
        assert pen["start"]["status"] == "confirmed"  # 已冻结的起点不会回退
        assert pen["end"]["status"] in {"candidate", "confirmed"}
        # 状态与确认日必须互相自洽(候选没有确认日; 已确认必须有)
        if pen["status"] == "confirmed":
            assert pen["confirmed_on"] is not None
        else:
            assert pen["confirmed_on"] is None


def test_pen_counts_and_not_implemented_stay_honest(monkeypatch, auth_headers):
    _, _, body = _pen_response(monkeypatch, auth_headers)

    assert set(body["pen_counts"]) == {"total", "confirmed", "candidate", "blocked"}
    assert body["pen_counts"] == {"total": 3, "confirmed": 2, "candidate": 1, "blocked": 0}
    assert body["pen_counts"]["total"] == len(body["pens"])
    # 分型 counts 保持原语义(只数分型), 不被笔偷偷改写
    assert body["counts"]["total"] == len(body["fractals"]) == 5
    assert body["counts"]["confirmed"] == 4
    assert body["counts"]["candidate"] == 1

    # 笔已接入 → 不再挂在 not_implemented; 线段/中枢仍必须明写未实现
    assert "pen" not in body["not_implemented"]
    assert {"segment", "centre"} <= set(body["not_implemented"])

    # 口径诚实: 规则几何 + 候选可漂移 + 最终性/复权未知 + 非交易信号
    assert body["adjustment"] == "unknown"
    assert body["finality"] == "unverified"
    assert "规则几何" in body["pen_disclaimer"]
    assert "候选" in body["pen_disclaimer"]
    assert "复权" in body["pen_disclaimer"]
    assert "交易信号" in body["pen_disclaimer"]
    assert "笔" in body["disclaimer"]
    assert "不构成交易建议" in body["disclaimer"]


def test_pens_and_fractals_come_from_one_fetch_and_one_structure_build(monkeypatch, auth_headers):
    """同一个快照绑定: 一次取数 + 一次 build_chan_structure + 同一个对象喂给笔。"""
    from src.web.api import klines as kl

    client = _pen_client(monkeypatch)
    chart = _chart(client, auth_headers)
    digest = chart["chan_snapshot"]["digest"]

    fetches: list[dict] = []
    real_get = kl.get_klines

    def _counting_get(*args, **kwargs):
        fetches.append(kwargs)
        return real_get(*args, **kwargs)

    builds: list = []
    real_build = chan.build_chan_structure

    def _counting_build(bars):
        snapshot = real_build(bars)
        builds.append(snapshot)
        return snapshot

    received: list = []
    real_pen = chan.pen_from_snapshot

    def _counting_pen(snapshot, **kwargs):
        received.append(snapshot)
        return real_pen(snapshot, **kwargs)

    monkeypatch.setattr(kl, "get_klines", _counting_get)
    monkeypatch.setattr(chan, "build_chan_structure", _counting_build)
    monkeypatch.setattr(chan, "pen_from_snapshot", _counting_pen)

    r = _structure(client, auth_headers, digest)
    assert r.status_code == 200, r.text
    assert r.json()["pens"], "样例必须产出非空笔"

    assert len(fetches) == 1, "结构端点必须复用那次 get_klines, 不许为笔二次取数"
    assert len(builds) == 1, "分型与笔必须来自同一次 build_chan_structure"
    assert len(received) == 1, "笔只能算一次"
    # 同一对象, 不是"同参数再算一遍": 笔的输入就是产出 fractals 的那个快照
    assert received[0] is builds[0]


def test_pen_endpoints_belong_to_the_returned_fractals_and_never_past_as_of_day(
    monkeypatch, auth_headers
):
    """确认日期与快照截止: 端点/确认日必须落在这份响应的分型与 as_of_day 之内。"""
    _, _, body = _pen_response(monkeypatch, auth_headers)
    as_of_day = body["as_of_day"]
    assert as_of_day == "2026-01-16"
    confirmed_days = {f["confirmed_on"] for f in body["fractals"] if f["confirmed_on"]}
    by_position = {(f["position_day"], f["position_price"]): f for f in body["fractals"]}

    for pen in body["pens"]:
        for side in ("start", "end"):
            endpoint = pen[side]
            fractal = by_position.get((endpoint["position_day"], endpoint["price"]))
            assert fractal is not None, f"{side} 端点不在本响应分型里: {endpoint}"
            assert fractal["status"] == "confirmed"
            assert fractal["kind"] == endpoint["kind"]
            assert endpoint["position_day"] <= as_of_day
            assert endpoint["position_day"] != TODAY
        if pen["status"] == "confirmed":
            # confirmed_on 就是终点被冻结那天, 必落在快照截止日内, 且晚于起点发生日
            assert pen["confirmed_on"] in confirmed_days
            assert pen["confirmed_on"] <= as_of_day
            assert pen["confirmed_on"] > pen["start"]["position_day"]
        else:
            assert pen["status"] == "candidate"
            assert pen["confirmed_on"] is None
            assert pen["end"]["confirmed_on"] is None


def test_pen_kernel_rejection_fails_closed(monkeypatch, auth_headers):
    """内核拒绝(快照不自洽) → 409, 不返回半份结果, 更不返回空笔冒充成功。"""
    client = _pen_client(monkeypatch)
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]

    def _boom(snapshot, **kwargs):
        raise ValueError("fractal at merged index 5 claims confirmation on 2026-01-01")

    monkeypatch.setattr(chan, "pen_from_snapshot", _boom)
    r = _structure(client, auth_headers, digest)
    assert r.status_code == 409, r.text
    body = r.json()
    assert "pens" not in body and "fractals" not in body
    assert "笔" in body["detail"]
    text = json.dumps(body, ensure_ascii=False)
    assert "merged index" not in text  # 内核原文不外泄(细节只进日志)


def test_pen_result_from_a_different_snapshot_fails_closed(monkeypatch, auth_headers):
    """笔若来自另一批K线(不同截止日) → 409, 不许把别人的笔挂在这次的分型上。"""
    from src.core.chan_pen import pen_from_snapshot as real_pen
    from src.core.chan_structure import RawBar, build_chan_structure as real_build

    client = _pen_client(monkeypatch)
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]

    # 同样的锯齿但整体挪到 2025 年: 价格几何相同, 截止日/日期不同
    foreign_bars = [
        RawBar(day=f"2025{day[4:]}", open=low, high=high, low=low, close=high)
        for day, (high, low) in zip(_PEN_DAYS, _PEN_HIGHS_LOWS)
    ]
    foreign = real_pen(real_build(foreign_bars))
    monkeypatch.setattr(chan, "pen_from_snapshot", lambda snapshot, **kwargs: foreign)
    r = _structure(client, auth_headers, digest)
    assert r.status_code == 409, r.text
    assert "pens" not in r.json()


def test_pen_endpoint_not_in_this_snapshot_fails_closed(monkeypatch, auth_headers):
    """端点被改出一个本快照不存在的价格 → 409(自洽性校验, 不是"照抄内核")。"""
    from dataclasses import replace as dc_replace

    from src.core.chan_pen import pen_from_snapshot as real_pen

    client = _pen_client(monkeypatch)
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]

    def _doctored(snapshot, **kwargs):
        result = real_pen(snapshot, **kwargs)
        first = result.pens[0]
        moved_start = dc_replace(first.start, price=first.start.price + 0.01)
        moved_pen = dc_replace(first, start=moved_start)
        return dc_replace(result, pens=(moved_pen,) + result.pens[1:])

    monkeypatch.setattr(chan, "pen_from_snapshot", _doctored)
    r = _structure(client, auth_headers, digest)
    assert r.status_code == 409, r.text
    assert "pens" not in r.json()


def test_pen_without_any_completed_bar_never_pretends_success(monkeypatch, auth_headers):
    """没有已完成的K线 → 422; 只有一根 → 200 但笔数量为 0(如实空, 不是"算出来了")。"""
    empty_client = _client(monkeypatch, lambda symbol, market_code, days: ([], None))
    chart = _chart(empty_client, auth_headers)
    digest = chart["chan_snapshot"]["digest"]
    r = _structure(empty_client, auth_headers, digest)
    assert r.status_code == 422, r.text
    assert "pens" not in r.json() and "fractals" not in r.json()

    one_bar = _client(monkeypatch, lambda symbol, market_code, days: ([_pen_bars()[0]], _PEN_DAYS[0]))
    chart_one = _chart(one_bar, auth_headers)
    ok = _structure(one_bar, auth_headers, chart_one["chan_snapshot"]["digest"])
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["pens"] == [] and body["pen_counts"] == {
        "total": 0, "confirmed": 0, "candidate": 0, "blocked": 0
    }
    assert body["as_of_day"] == _PEN_DAYS[0]  # 截止日如实给出, 不冒充有结构


def test_pens_absent_from_conflict_and_auth_failures(monkeypatch, auth_headers):
    client = _pen_client(monkeypatch)
    digest = _chart(client, auth_headers)["chan_snapshot"]["digest"]

    tampered = ("0" if digest[0] != "0" else "1") + digest[1:]
    conflict = _structure(client, auth_headers, tampered)
    assert conflict.status_code == 409
    assert "pens" not in conflict.json() and "pen_rules_version" not in conflict.json()

    anon = client.get("/api/klines/600000/chan-structure", params={
        "market": "CN", "instrument": "stock", "interval": "1d", "days": 60, "snapshot": digest,
    })
    assert anon.status_code == 401, anon.text
    assert "pens" not in anon.json() and "pen_rules_version" not in anon.json()


# ── ⑭ 真实 app: 笔能穿过生产响应封装(前端读 data.*) ─────────────────────


def test_real_app_serves_pens_inside_the_response_envelope(monkeypatch, auth_headers):
    """真实 app 上跑一遍: 图表摘要 → 结构端点 → ``data.pens`` 可达且是有内容的笔。

    本文件其它用例用独立 FastAPI() 拿裸响应; 这里钉住**生产 app** 那层统一封装
    (``{success, data, ...}``): 笔必须出现在前端真正读取的位置, 而不是只在裸响应里。
    """
    from src.web.api import klines as kl
    from src.web.app import app

    monkeypatch.setattr(kl, "_pg_klines", _Feed(_pen_bars()))
    monkeypatch.setattr(kl, "_finalize_daily_bars", lambda bars, *a, **k: (bars, None))
    monkeypatch.setattr(kl, "_kl_cache", {})
    monkeypatch.setattr(chan, "_now", lambda: NOW)

    client = TestClient(app)
    chart = client.get(
        "/api/klines/600000",
        params={"market": "CN", "days": 60, "interval": "1d", "instrument": "stock"},
        headers=auth_headers,
    )
    assert chart.status_code == 200, chart.text
    digest = chart.json()["data"]["chan_snapshot"]["digest"]

    r = client.get(
        "/api/klines/600000/chan-structure",
        params={"market": "CN", "instrument": "stock", "interval": "1d",
                "days": 60, "snapshot": digest},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["pen_rules_version"] == "chan-pen-v1"
    assert body["pen_counts"] == {"total": 3, "confirmed": 2, "candidate": 1, "blocked": 0}
    assert [p["status"] for p in body["pens"]] == ["confirmed", "confirmed", "candidate"]
    assert body["pens"][-1]["confirmed_on"] is None
    assert body["pens"][-1]["end"]["status"] == "candidate"
    assert "pen" not in body["not_implemented"]


# ── ⑮ 测试隔离: 只碰 conftest 建在临时 DATA_DIR 下的 SQLite ────────────────


def test_suite_uses_isolated_scratch_sqlite(auth_headers):
    """裸跑真库必须变红: 本轮 pytest 的 DB 绑定必须落在临时目录(SQLite), 不是仓库/生产库。

    为什么单独钉一条: 若 shell 里残留 `SIDA_DB_URL`/`DATA_DIR` 指向真库, conftest 的
    setdefault 不会覆盖它, 整套用例就会往真库写用户/会话。这条断言在那种情况下直接红。
    """
    from src.web.database import SessionLocal

    data_dir = Path(os.environ["DATA_DIR"]).resolve()
    assert data_dir.name.startswith("sida_test_data_"), f"DATA_DIR 不是临时目录: {data_dir}"

    db = SessionLocal()
    try:
        url = str(db.get_bind().url)
    finally:
        db.close()

    assert url.startswith("sqlite"), f"测试库必须是隔离 SQLite, 实际: {url.split('://')[0]}"
    db_path = Path(url.split("sqlite:///", 1)[-1]).resolve()
    # 必须在某个 mkdtemp 出来的测试目录里, 且绝不是仓库 data/ 下的真库
    assert any(p.name.startswith("sida_test_data_") for p in db_path.parents), (
        f"测试库不在临时目录内: {db_path}"
    )
    repo_data = (Path(__file__).resolve().parent.parent / "data").resolve()
    assert repo_data not in db_path.parents, f"测试碰了仓库真库: {db_path}"

