"""盘后复盘三池序列 + 连板晋级树 API 测试(创新 1, 2026-10-24)。

规格: specs/harness/replay_tree.md (REPTREE-001..006)。
不联网: 表数据直接写 ORM, 不触发任何东财/TQ 抓取。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.web.api import market_replay as api

PREFIX = "/api/market/replay"

# 2026-10-19(周一) ~ 10-23(周五): 连续 5 个交易日, 处于交易日历静态表覆盖范围内
D_MON, D_TUE, D_WED, D_THU, D_FRI = "20261019", "20261020", "20261021", "20261022", "20261023"


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return TestClient(app)


def _member(code: str, name: str, board: int, seal_wan: float = 3000.0) -> dict:
    return {
        "symbol": code,
        "ts_code": f"{code}.SZ",
        "name": name,
        "price": 10.0,
        "pct": 10.02,
        "amount_wan": 45000.0,
        "circ_mv_yi": 17.0,
        "turnover_pct": 2.6,
        "seal_amount_wan": seal_wan,
        "first_seal": "09:25:00" if board > 1 else "10:00:00",
        "last_seal": "15:00:00",
        "open_times": 0,
        "board_days": board,
        "industry": "软件服务",
        "limit_price": 11.0,
    }


def _payload(
    ladder: dict[str, int],
    rows: list[dict],
    *,
    total: int | None = None,
    promo: dict | None = None,
    prev_cmp: dict | None = None,
    emotion: dict | None = None,
    cross_check: dict | None = None,
) -> dict:
    total = total if total is not None else sum(ladder.values())
    return {
        "limit_up": {
            "caliber": "eastmoney_zt",
            "total": total,
            "returned": len(rows),
            "max_streak": max((int(k) for k in ladder), default=0),
            "ladder": ladder,
            "first_board": ladder.get("1", 0),
            "ge2": sum(v for k, v in ladder.items() if int(k) >= 2),
            "ge3": sum(v for k, v in ladder.items() if int(k) >= 3),
            "one_word": 3,
            "rows": rows,
        },
        "broken": {"caliber": "eastmoney_zt", "total": 12, "returned": 0},
        "limit_down": {"caliber": "eastmoney_zt", "total": 2, "returned": 0},
        "promotion": promo or {"available": False},
        "mainline": {"top_sectors": [], "leaders": []},
        "emotion": emotion or {"available": False},
        "prev_comparison": prev_cmp or {"available": False},
        "indices_flow": {"available": False},
        "cross_check": cross_check or {},
        "quality": {},
    }


def _row(
    trade_date: str,
    *,
    limit_up: int = 80,
    broken: int = 12,
    limit_down: int = 2,
    max_streak: int = 6,
    promo_rate: float | None = 31.0,
    phase: str | None = "main_up",
    phase_label: str | None = "主升",
    payload: dict | None = None,
) -> dict:
    return {
        "trade_date": trade_date,
        "market": "CN",
        "limit_up_count": limit_up,
        "limit_up_max_streak": max_streak,
        "first_board_count": 60,
        "ge2_count": 14,
        "ge3_count": 6,
        "one_word_count": 3,
        "broken_count": broken,
        "break_rate_pct": 13.04,
        "seal_rate_pct": 86.96,
        "limit_down_count": limit_down,
        "limit_down_max_days": 2,
        "promo_rate_pct": promo_rate,
        "phase": phase,
        "phase_label": phase_label,
        "mainline": {},
        "quality": "ok",
        "missing_dims": [],
        "source": "eastmoney_replay",
        "payload": payload,
    }


def _seed(rows: list[dict]) -> None:
    from src.db.models import MarketReplayDaily
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_replay_daily"))
        for r in rows:
            db.add(MarketReplayDaily(**r))
        db.commit()
    finally:
        db.close()


def _cleanup() -> None:
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_replay_daily"))
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def seeded():
    _seed([_row(D_MON), _row(D_TUE), _row(D_WED), _row(D_THU), _row(D_FRI)])
    yield
    _cleanup()


# ─────────────── REPTREE-001: 缺数据日显式 available=false, 绝不补 0 ───────────────


def test_series_fills_missing_trading_day_as_unavailable(client):
    """@spec REPTREE-001"""
    _seed([_row(D_MON), _row(D_WED)])  # 周二缺落库
    try:
        d = client.get(f"{PREFIX}/series", params={"days": 3}).json()
        assert d["count"] == 3, "3 个交易日都应出现"
        dates = [it["trade_date"] for it in d["items"]]
        assert dates == [D_MON, D_TUE, D_WED], "按交易日升序补齐"
        mid = d["items"][1]
        assert mid["available"] is False
        assert mid["limit_up_count"] is None, "缺失日禁止补 0"
        assert mid["broken_count"] is None and mid["limit_down_count"] is None
        assert mid["promo_rate_pct"] is None and mid["phase"] is None
        # 已落库行数值原样
        assert d["items"][0]["available"] is True and d["items"][0]["limit_up_count"] == 80
    finally:
        _cleanup()


def test_series_missing_day_skips_non_trading_days(client):
    """@spec REPTREE-001"""
    _seed([_row(D_FRI)])  # 往回 3 个交易日: 周五/周四/周三(周末不占位)
    try:
        d = client.get(f"{PREFIX}/series", params={"days": 3}).json()
        dates = [it["trade_date"] for it in d["items"]]
        assert dates == [D_WED, D_THU, D_FRI]
        assert all(it["available"] is False for it in d["items"][:-1])
    finally:
        _cleanup()


def test_series_empty_db_returns_zero_items(client):
    """@spec REPTREE-001"""
    _cleanup()
    d = client.get(f"{PREFIX}/series").json()
    assert d["count"] == 0 and d["items"] == []


# ─────────────── 序列基础: 升序 / 标量 / 环比差分 / 校验 ───────────────


def test_series_ascending_with_scalars_and_caliber(client, seeded):
    d = client.get(f"{PREFIX}/series", params={"days": 5}).json()
    assert d["caliber"] == "eastmoney_zt"
    assert "不混算" in d["caliber_note"]
    items = d["items"]
    assert [it["trade_date"] for it in items] == [D_MON, D_TUE, D_WED, D_THU, D_FRI]
    it = items[-1]
    assert it["limit_up_count"] == 80 and it["broken_count"] == 12
    assert it["break_rate_pct"] == 13.04 and it["seal_rate_pct"] == 86.96
    assert it["promo_rate_pct"] == 31.0
    assert it["max_streak"] == 6 and it["first_board_count"] == 60
    assert it["ge2_count"] == 14 and it["ge3_count"] == 6
    assert it["phase"] == "main_up" and it["phase_label"] == "主升"
    assert it["quality"] == "ok" and it["source"] == "eastmoney_replay"
    assert it["trade_date_iso"] == "2026-10-23"


def test_series_deltas_only_when_prev_row_is_prev_trading_day(client):
    """@spec REPTREE-001"""
    # 周一 80 家 → 周二 90 家(相邻 → 差分); 周四缺 → 周五不差分
    _seed([_row(D_MON, limit_up=80), _row(D_TUE, limit_up=90), _row(D_FRI, limit_up=70)])
    try:
        d = client.get(f"{PREFIX}/series", params={"days": 5}).json()
        by = {it["trade_date"]: it for it in d["items"]}
        tue = by[D_TUE]
        assert tue["delta_limit_up"] == 10
        assert tue["prev_date"] == D_MON
        assert tue["promo_prev_count"] == 80, "晋级率样本数 = 昨日涨停家数"
        fri = by[D_FRI]
        assert fri["delta_limit_up"] is None, "前一日(周四)缺落库 → 显式 null, 不硬算"
        assert fri["promo_prev_count"] is None
    finally:
        _cleanup()


def test_series_rejects_out_of_range_days(client):
    assert client.get(f"{PREFIX}/series", params={"days": 0}).status_code == 422
    assert client.get(f"{PREFIX}/series", params={"days": 251}).status_code == 422


def test_series_range_mode_with_validation(client, seeded):
    d = client.get(
        f"{PREFIX}/series", params={"from": "2026-10-20", "to": "20261022"}
    ).json()
    dates = [it["trade_date"] for it in d["items"]]
    assert dates == [D_TUE, D_WED, D_THU]
    assert client.get(f"{PREFIX}/series", params={"from": "2026/10/20"}).status_code == 400
    r = client.get(f"{PREFIX}/series", params={"from": "20261022", "to": "20261020"})
    assert r.status_code == 400


# ─────────────── REPTREE-003: 双口径标签 ───────────────


def test_tree_cross_check_carries_both_calibers(client):
    """@spec REPTREE-003"""
    _seed(
        [
            _row(
                D_FRI,
                payload=_payload(
                    {"1": 78, "6": 1},
                    [_member("002724", "海洋王", 6)],
                    total=82,
                    cross_check={
                        "eastmoney_zt": 82,
                        "tq_pool": 86,
                        "tq_max_streak": 6,
                        "diff": 4,
                        "note": "TQ 含北交所; 东财不含 → 家数天然有差, 两口径分别标注禁止混算。",
                        "tq_detail": {"available": True, "caliber": "tq_pool", "total": 86},
                    },
                ),
            )
        ]
    )
    try:
        d = client.get(f"{PREFIX}/tree").json()
        cc = d["cross_check"]
        assert cc["eastmoney_zt"] == 82 and cc["tq_pool"] == 86 and cc["diff"] == 4
        assert "北交所" in cc["note"] and "混算" in cc["note"]
        assert cc["tq_available"] is True
        assert d["caliber"] == "eastmoney_zt"
    finally:
        _cleanup()


# ─────────────── 晋级树: 层级 / 成员 / 三件套 ───────────────


def _tree_seed_payload() -> dict:
    ladder = {"1": 78, "3": 3, "6": 1}
    rows = [_member("300001", "高板龙头", 6), _member("300002", "三板甲", 3),
            _member("300003", "三板乙", 3)]
    return _payload(
        ladder,
        rows,
        total=82,
        promo={
            "available": True,
            "prev_date": D_THU,
            "prev_count": 86,
            "promoted": 27,
            "promo_rate_pct": 31.39,
        },
        prev_cmp={"available": True, "prev_date": D_THU, "delta_limit_up": -4},
        emotion={"available": True, "phase": "main_up", "phase_label": "主升", "basis_days": 12},
        cross_check={"eastmoney_zt": 82, "tq_pool": 86, "diff": 4, "note": "差北交所", "tq_detail": {"available": True}},
    )


def test_tree_latest_with_levels_and_promotion_three_piece(client):
    """@spec REPTREE-002"""
    _seed([_row(D_THU), _row(D_FRI, payload=_tree_seed_payload())])
    try:
        d = client.get(f"{PREFIX}/tree").json()
        assert d["available"] is True
        assert d["trade_date"] == D_FRI, "缺省取最新一期"
        # 层级: 高板在前
        boards = [lv["board"] for lv in d["levels"]]
        assert boards == [6, 3, 1]
        top = d["levels"][0]
        assert top["count"] == 1 and top["members"][0]["symbol"] == "300001"
        assert top["members_truncated"] is False
        # 晋级率三件套
        promo = d["promotion"]
        assert promo["available"] is True and promo["promo_rate_pct"] == 31.39
        assert promo["sample_count"] == 86, "样本数 = 昨日涨停池家数"
        assert promo["promoted"] == 27
        assert promo["baseline_available"] is False
        assert "口径缺口" in promo["baseline_note"], "同池基准表内未存 → 显式缺口"
        assert promo["excludes_one_word"] is False
        assert "一字" in promo["one_word_note"]
        assert promo["one_word_count"] == 3
        # 环比( payload 权威值) + 情绪
        assert d["prev_comparison"]["delta_limit_up"] == -4
        assert d["phase"] == "main_up" and d["emotion_available"] is True
    finally:
        _cleanup()


def test_tree_detail_partial_marks_truncation(client):
    """@spec REPTREE-004"""
    _seed([_row(D_FRI, payload=_tree_seed_payload())])
    try:
        d = client.get(f"{PREFIX}/tree", params={"date": D_FRI}).json()
        assert d["detail_partial"] is True, "82 家涨停但明细只落了 3 条 → 截断"
        assert d["detail_returned"] == 3
        assert "Top40" in d["detail_note"]
        low = d["levels"][2]  # 首板 78 家, 明细 0 条
        assert low["count"] == 78 and low["members_truncated"] is True
        assert low["members"] == []
        # 封单额/流通市值比: 表内未存 → 显式无数据
        assert d["seal_mv_ratio"] is None and "未存" in d["seal_mv_ratio_note"]
        # 成员里逐股封单额照实下发(原值, 不换算成比值)
        assert d["levels"][0]["members"][0]["seal_amount_wan"] == 3000.0
    finally:
        _cleanup()


def test_tree_per_level_promotion_unavailable(client):
    """@spec REPTREE-005"""
    _seed([_row(D_FRI, payload=_tree_seed_payload())])
    try:
        d = client.get(f"{PREFIX}/tree").json()
        promo = d["promotion"]
        assert promo["per_level_available"] is False
        assert "分层晋级率" in promo["per_level_note"]
        for lv in d["levels"]:
            assert "promo_rate_pct" not in lv, "层级上禁止出现现算晋级率"
    finally:
        _cleanup()


def test_tree_without_payload_keeps_scalars_marks_ladder_missing(client):
    """@spec REPTREE-004"""
    _seed([_row(D_FRI, payload=None)])
    try:
        d = client.get(f"{PREFIX}/tree").json()
        assert d["available"] is True
        assert d["ladder_available"] is False and d["levels"] == []
        assert d["limit_up_count"] == 80, "标量列照常下发, 不用标量反推梯队"
        assert d["max_streak"] == 6
    finally:
        _cleanup()


def test_tree_missing_date_returns_available_false_with_latest(client):
    """@spec REPTREE-006"""
    _seed([_row(D_FRI, payload=_tree_seed_payload())])
    try:
        d = client.get(f"{PREFIX}/tree", params={"date": "2026-10-24"}).json()
        assert d["available"] is False
        assert "无复盘快照" in d["note"]
        assert d["latest_date"] == D_FRI and d["latest_date_iso"] == "2026-10-23"

        d2 = client.get(f"{PREFIX}/tree").json()
        assert d2["available"] is True and d2["trade_date_iso"] == "2026-10-23"
    finally:
        _cleanup()


def test_tree_emotion_missing_is_explicit(client):
    """@spec REPTREE-006"""
    _seed([_row(D_FRI, phase=None, phase_label=None, payload=_tree_seed_payload())])
    try:
        d = client.get(f"{PREFIX}/tree").json()
        assert d["emotion_available"] is False
        assert d["phase"] is None and d["phase_label"] is None
    finally:
        _cleanup()
