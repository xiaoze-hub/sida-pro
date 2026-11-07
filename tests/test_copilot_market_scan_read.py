# -*- coding: utf-8 -*-
"""数智参谋市场扫描榜 / DDE 榜只读适配层单测（2026-11-07，复审阻断项回归）。

## 本文件的「红→绿」对照（每条对应复审确认的一个阻断）

| # | 复审阻断 | 红测 |
| :-- | :--- | :--- |
| 1 | 外部 `as_of` 可让旧数据装新鲜 | `test_public_api_has_no_as_of_override` |
| 2 | 快照写入日≠数据日（交易日也一样） | `test_weekday_write_day_is_not_data_date` / `test_holiday_write_day_refuses_to_date_data` / `test_weekend_write_day_not_treated_fresh` |
| 3 | 日期不可解析被当成「没有时效信息」 | `test_unparseable_snapshot_date_is_unverified_but_readable` |
| 3b | 交易日历不可用仍判新鲜 | `test_calendar_unavailable_is_unknown_not_fresh` |
| 3c | unknown 时把整个榜单藏起来（另一个极端） | `test_unknown_data_date_still_returns_boards_with_labels` |
| 4 | 禁用账户对象绕过页面身份条件 | `test_disabled_account_denied` / `test_soft_deleted_account_denied` /
      `test_service_identity_denied` / `test_empty_identity_denied` |
| 5 | payload.available=False 被覆盖成 True | `test_payload_available_false_is_not_masked` |
| 6 | DDE 主力净流入被说成「暗盘拆单」 | `test_dde_render_separates_main_net_from_dark_split` |
| 7 | `.tck` 被说成「委托号级精确暗盘」（已证伪） | `test_tck_column_not_claimed_order_level_exact` |

## 测试隔离

每个用例独立 tmp_path SQLite；**不连仓库 data/ 或生产库**，也不触发 scan/refresh。
时钟一律经模块私有钩子 `tsr._now_cst` 注入（monkeypatch），生产调用方没有任何时间入参。
"""
from __future__ import annotations

import inspect
from datetime import date, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册全部 ORM 表
from src.agents.chat import tools_scan_read as tsr
from src.db.models import DarkFundTopSnapshot, HighValueApiLog, MarketScanRank
from src.web.database import Base
from tests.factories import make_user


# ────────────────────────────── fixtures ──────────────────────────────


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scan_read.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def set_now(monkeypatch):
    """注入「可信系统时间」。**只对私有钩子**，不新增任何公共时间入参。"""

    def _set(dt: datetime) -> None:
        monkeypatch.setattr(tsr, "_now_cst", lambda: dt)

    _set(datetime(2026, 11, 6, 16, 0))  # 默认: 2026-11-06(五) 收盘后, 当日快照应已落库
    return _set


def _user(db, role: str = "pro", **kw):
    u = make_user(role=role, **kw)
    db.add(u)
    db.commit()
    return u


def _add_rank(db, snap, payload, market: str = "CN") -> MarketScanRank:
    row = MarketScanRank(snapshot_date=snap, stock_market=market, payload=payload)
    db.add(row)
    db.commit()
    return row


def _add_dark(db, snap, payload: dict, market: str = "CN") -> DarkFundTopSnapshot:
    row = DarkFundTopSnapshot(snapshot_date=snap, stock_market=market, payload=payload)
    db.add(row)
    db.commit()
    return row


def _hv_count(db, api_name: str) -> int:
    return db.query(HighValueApiLog).filter(HighValueApiLog.api_name == api_name).count()


RANK_PAYLOAD = {
    "generated_at": "2026-11-06T15:30:00",
    "universe": 5000,
    "computed": 4990,
    "skipped": 10,
    "dark_days": 1,
    "new_g_points": [{"symbol": "000001", "new_g_status": "待确认"}],
    "dark_top": [
        {
            "symbol": "000002",
            "dark_net": 1234500.0,
            "dark_net_wan": 123.45,
            "approximation": True,
        }
    ],
    "activity_top": [{"symbol": "000003", "activity": 6.5, "level": "大牛线"}],
    "zljc": {"main_net_wan": 1.0},
}

DARK_PAYLOAD = {
    "generated_at": "2026-11-06T15:31:00",
    "universe": 5000,
    "computed": 4800,
    "top": [
        {
            "symbol": "600000",
            "name": "浦发银行",
            "main_net_wan": 12345.6,
            "main_net_vol": 1.2,
            "total_amount_wan": 99999.9,
            "source": "thsdk_dde",
        }
    ],
    "caliber": "ths",
    "direction_semantics": "正值=净流入",
}


# ─────────────── 阻断 1: 调用方不得伪造新鲜度（无 as_of 入参） ───────────────


def test_public_api_has_no_as_of_override():
    """新鲜度只能由可信系统时间推导；公共签名里不许出现 as_of / now / today。"""
    for fn in (tsr.read_market_scan_ranks, tsr.read_dark_fund_top):
        params = set(inspect.signature(fn).parameters)
        assert "as_of" not in params, f"{fn.__name__} 仍允许调用方传 as_of"
        assert not params & {"now", "today", "date", "clock"}, f"{fn.__name__} 暴露了时间入参"


def test_caller_cannot_pass_as_of(db, set_now):
    """就算硬传 as_of 也必须 TypeError，不能让旧数据装新鲜。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 10, 30), RANK_PAYLOAD)
    set_now(datetime(2026, 11, 13, 16, 0))

    with pytest.raises(TypeError):
        tsr.read_market_scan_ranks(db, user, as_of=date(2026, 10, 30))  # type: ignore[call-arg]


# ─────────────── 阻断 2: 写入日≠数据日（节假日 / 周末补跑） ───────────────


def test_holiday_write_day_refuses_to_date_data(db, set_now):
    """国庆假期 2026-10-05 落库的快照：写入日不是交易日，数据日无从判定。

    旧实现拿 snapshot_date 直接与「最近应更新交易日」比，假期写入日 > 应更新日
    ⇒ stale=False，等于把节前旧数据说成新鲜。必须改成「数据日期未知、时效不可判断」。
    """
    user = _user(db, "pro")
    _add_rank(db, date(2026, 10, 5), RANK_PAYLOAD)  # 国庆休市日写入
    set_now(datetime(2026, 10, 6, 10, 0))

    data = tsr.read_market_scan_ranks(db, user)

    assert data["available"] is True, "数据日未知 ≠ 数据不可用：不得把可用榜单整个藏掉"
    assert data["data_date"] is None
    assert data["freshness"] == "unknown"
    assert data["stale"] is None, "日期不可判定时绝不允许给出 stale=False"
    assert data["data_date_status"] == "unverified"
    assert data["snapshot_date"] == "2026-10-05"  # 仅作「快照写入日」展示
    assert "2026-10-05" in data["freshness_note"]
    assert "未核实" in data["freshness_note"]
    assert data["dark_top"][0]["symbol"] == "000002"

    text = tsr.render_market_scan_ranks(data)
    assert "写入日 2026-10-05" in text
    assert "数据日期未核实" in text
    assert "今日" not in text and "实时" not in text
    assert "未过期" not in text and "新鲜" not in text


def test_weekend_write_day_not_treated_fresh(db, set_now):
    """周末补跑写入的快照（写入日=周六）同样不能算「最新交易日数据」。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 7), RANK_PAYLOAD)  # 周六写入
    set_now(datetime(2026, 11, 9, 10, 0))  # 周一盘前

    data = tsr.read_market_scan_ranks(db, user)

    assert data["freshness"] == "unknown"
    assert data["stale"] is None
    assert data["data_date"] is None
    assert data["available"] is True
    assert "写入日" in data["freshness_note"] and "未核实" in data["freshness_note"]


def test_weekday_write_day_is_not_data_date(db, set_now):
    """(本次最后一条阻断) 写入日**本身是交易日**也不等于数据日。

    盘后 cron 用 `datetime.now()` 落 `snapshot_date`；payload 里只有 `generated_at`
    （扫描生成时刻）。写入日恰好是交易日、但只要底层 TQ/thsdk 行情不是当日，
    就仍然是「快照写入日期 + 数据日期未核实」。旧实现 `return snap` 就是拿写入日冒数据日。
    """
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)  # 周五收盘后写入
    set_now(datetime(2026, 11, 6, 16, 0))

    data = tsr.read_market_scan_ranks(db, user)

    assert data["snapshot_date"] == "2026-11-06"
    assert data["expected_data_day"] == "2026-11-06"
    assert data["data_date"] is None, "写入日是交易日不构成数据日证据"
    assert data["data_date_status"] == "unverified"
    assert data["freshness"] == "unknown", "不得因写入日=交易日就标 fresh"
    assert data["stale"] is None
    assert data["age_days"] is None
    assert data["available"] is True
    assert "2026-11-06" in data["freshness_note"] and "写入" in data["freshness_note"]
    assert "未核实" in data["freshness_note"]

    text = tsr.render_market_scan_ranks(data)
    assert "写入日 2026-11-06（数据日期未核实）" in text
    assert "未过期" not in text and "已过期" not in text


def test_snapshot_in_future_is_unknown(db, set_now):
    """落库日期晚于当前日期（时钟/数据异常）→ 不敢判日期。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 20), RANK_PAYLOAD)
    set_now(datetime(2026, 11, 9, 16, 0))

    data = tsr.read_market_scan_ranks(db, user)
    assert data["freshness"] == "unknown"
    assert data["stale"] is None
    assert data["available"] is True
    assert "晚于当前系统日期" in data["freshness_note"]


# ─────────────── 阻断 3: 日期不可解析 / 日历不可用 ───────────────


def test_unparseable_snapshot_date_is_unverified_but_readable(db, set_now):
    """snapshot_date 不可解析：数据日未核实必须写明，但不把可用榜单藏起来。

    `dark_fund_top_snapshots.snapshot_date` 是 String 列（历史/手写/迁移可写入任意串），
    所以这个用例走 DDE 榜 —— 真实库里脏日期就长这样。
    """
    user = _user(db, "pro")
    _add_dark(db, "2026-13-45", DARK_PAYLOAD)
    set_now(datetime(2026, 11, 9, 16, 0))

    data = tsr.read_dark_fund_top(db, user)

    assert data["available"] is True
    assert data["data_date"] is None
    assert data["data_date_status"] == "unverified"
    assert data["freshness"] == "unknown"
    assert data["stale"] is None
    assert data["raw_snapshot_date"] == "2026-13-45"
    assert "无法解析" in data["freshness_note"]
    assert data["top"][0]["symbol"] == "600000"

    text = tsr.render_dark_fund_top(data)
    assert "数据日期未核实" in text
    assert "数据日 2026-11-06" not in text  # 不拿 generated_at 冒充数据日
    assert "扫描生成时间：2026-11-06" in text  # 只能当扫描时刻展示，且必须标明


def test_calendar_unavailable_is_unknown_not_fresh(db, set_now, monkeypatch):
    """交易日历故障时不许回落自然日判新鲜，必须显式「时效不可判断」。"""
    from src.core import trading_calendar

    def _boom(*_a, **_k):
        raise RuntimeError("交易日历未覆盖 2031 年")

    monkeypatch.setattr(trading_calendar, "is_trading_day", _boom)
    monkeypatch.setattr(trading_calendar, "trading_day_anchor", _boom)

    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    set_now(datetime(2026, 11, 6, 16, 0))

    data = tsr.read_market_scan_ranks(db, user)

    assert data["freshness"] == "unknown"
    assert data["stale"] is None
    assert data["data_date"] is None
    assert data["available"] is True, "日历缺口 → 时效 unknown，但不是数据不可用"
    assert data["expected_data_day"] is None
    assert "未核实" in data["freshness_note"]

    text = tsr.render_market_scan_ranks(data)
    assert "数据日期未核实" in text
    assert "未过期" not in text


def test_unknown_data_date_still_returns_boards_with_labels(db, set_now):
    """两个极端都要避免：把 unknown 当 fresh（旧 bug），或把 unknown 当不可用（藏数据）。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    _add_dark(db, "2026-11-06", DARK_PAYLOAD)
    set_now(datetime(2026, 11, 6, 16, 0))

    ranks = tsr.read_market_scan_ranks(db, user)
    dark = tsr.read_dark_fund_top(db, user)

    for data in (ranks, dark):
        assert data["available"] is True
        assert data["freshness"] == "unknown"
        assert data["data_date"] is None
        assert data["stale"] is None
        assert data["data_date_status"] == "unverified"
        assert "未核实" in data["freshness_note"]

    assert ranks["dark_top"] and ranks["activity_top"]
    assert dark["top"]


# ─────────────── 阻断 4: 身份条件（禁用/软删除/服务/空身份） ───────────────


def test_disabled_account_denied(db, set_now):
    """is_active=False 的账户对象不许靠 role=pro 绕过页面「账号已禁用」。"""
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    user = _user(db, "pro", is_active=False)

    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, user)
    assert exc.value.status_code == 403
    assert "禁用" in str(exc.value.detail)
    assert _hv_count(db, "opportunities") == 0


def test_soft_deleted_account_denied(db, set_now):
    """业务注销（is_deleted=True）同样拒绝，不落审计。"""
    _add_dark(db, "2026-11-06", DARK_PAYLOAD)
    user = _user(db, "pro", is_deleted=True)

    with pytest.raises(HTTPException) as exc:
        tsr.read_dark_fund_top(db, user)
    assert exc.value.status_code == 403
    assert "注销" in str(exc.value.detail)
    assert _hv_count(db, "dark_fund") == 0


def test_service_identity_denied(db, set_now):
    """服务身份（X-Service-Token / ServicePrincipal）不能读这两个榜 —— 页面端点只认登录用户。"""
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)

    class _Svc:
        id = "service"
        username = "service"
        role = "service"
        is_active = True
        is_service = True

    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, _Svc())  # type: ignore[arg-type]
    assert exc.value.status_code == 403
    assert "服务身份" in str(exc.value.detail)
    assert _hv_count(db, "opportunities") == 0


def test_empty_identity_denied(db, set_now):
    """空身份（无 id / 无 username）不许当成合法用户放行。"""
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    user = _user(db, "pro")
    user.id = ""
    user.username = ""

    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, user)
    assert exc.value.status_code == 401
    assert _hv_count(db, "opportunities") == 0


# ─────────────── 阻断 5: payload.available=False 不许被覆盖 ───────────────


def test_payload_available_false_is_not_masked(db, set_now):
    """扫描失败落库的 payload（available=False）必须原样认账，不返回榜单、不说可用。"""
    user = _user(db, "pro")
    _add_rank(
        db,
        date(2026, 11, 6),
        {"available": False, "note": "scan 失败: 行情源 502", "top": []},
    )

    data = tsr.read_market_scan_ranks(db, user)

    assert data["available"] is False
    assert "行情源 502" in data["note"]
    assert "payload" in data["note"]
    assert "top" not in data and "dark_top" not in data
    assert _hv_count(db, "opportunities") == 1

    text = tsr.render_market_scan_ranks(data)
    assert "502" in text
    assert "已过期" not in text  # 不是「过期」，是「不可用」，别混口径


def test_payload_cannot_forge_freshness_fields(db, set_now):
    """payload 里塞 stale=false / snapshot_date / source 不许覆盖信封元信息。"""
    user = _user(db, "pro")
    _add_rank(
        db,
        date(2026, 10, 30),
        {
            **RANK_PAYLOAD,
            "stale": False,
            "snapshot_date": "2026-11-06",
            "source": "thsdk_dde",
            "data_date": "2026-11-06",
        },
    )
    set_now(datetime(2026, 11, 13, 16, 0))

    data = tsr.read_market_scan_ranks(db, user)

    assert data["stale"] is None
    assert data["data_date"] is None, "payload.data_date 也不得采信"
    assert data["snapshot_date"] == "2026-10-30"  # 只有行上的写入日
    assert data["source"] == "market_scan_ranks"


# ─────────────── 阻断 6: DDE 主力净流入 ≠ 暗盘拆单 ───────────────


def test_dde_render_separates_main_net_from_dark_split(db, set_now):
    """标题必须叫「DDE 主力净流入」，并显式声明不是暗盘拆单。"""
    user = _user(db, "pro")
    _add_dark(db, "2026-11-06", DARK_PAYLOAD)

    data = tsr.read_dark_fund_top(db, user)
    text = tsr.render_dark_fund_top(data)

    assert "主力净流入" in text
    assert "暗盘拆单" in text and "非暗盘拆单" in text
    assert "真实暗盘" not in text  # 旧文案把 DDE 说成真实暗盘，已删
    assert text.splitlines()[0].find("暗盘资金 TOP") == -1  # 不许再用「暗盘资金」当主标题


def test_tck_column_not_claimed_order_level_exact(db, set_now):
    """`.tck` 的委托号级精确归属已在项目研究中证伪（时间指针归属 + 被动侧未落盘），
    渲染不得再把 `tck_dark_net_wan` 说成「委托号级精确暗盘」。"""
    user = _user(db, "pro")
    payload = dict(DARK_PAYLOAD)
    payload["top"] = [{**DARK_PAYLOAD["top"][0], "tck_dark_net_wan": 12.34, "tck_available": True}]
    _add_dark(db, "2026-11-06", payload)

    data = tsr.read_dark_fund_top(db, user)
    text = tsr.render_dark_fund_top(data)

    assert "12.34" in text
    assert "非委托号级" in text  # 显式否认精确归属
    assert "委托号级精确暗盘" not in text and "委托号级暗盘" not in text
    assert "已被项目研究证伪" in text
    assert "近似" in text


def test_boards_are_never_merged(db, set_now):
    """两榜独立：三榜信封里没有 top，DDE 信封里没有 dark_top，也不提供合并入口。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    _add_dark(db, "2026-11-06", DARK_PAYLOAD)

    ranks = tsr.read_market_scan_ranks(db, user)
    dark = tsr.read_dark_fund_top(db, user)

    assert "top" not in ranks
    assert "dark_top" not in dark
    assert not hasattr(tsr, "read_all") and not hasattr(tsr, "read_merged")
    assert "非暗盘拆单" in tsr.render_market_scan_ranks(ranks)


# ─────────────── 保留口径: 权限 / 审计 / 新鲜度 / 市场过滤 / 只读 ───────────────


def test_ranks_pro_user_gets_latest_snapshot_and_audit(db, set_now):
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 5), {**RANK_PAYLOAD, "generated_at": "old"})
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    set_now(datetime(2026, 11, 6, 16, 0))

    data = tsr.read_market_scan_ranks(db, user)

    assert data["available"] is True
    assert data["snapshot_date"] == "2026-11-06"
    assert data["data_date"] is None  # 写入日 ≠ 数据日
    assert data["data_date_status"] == "unverified"
    assert data["freshness"] == "unknown"
    assert data["market"] == "CN"
    assert data["source"] == "market_scan_ranks"
    assert data["dark_top_source"] == "ohlc_approx"
    assert data["dark_top"][0]["approximation"] is True
    assert data["new_g_points"][0]["symbol"] == "000001"
    assert data["activity_top"][0]["activity"] == 6.5
    assert data["stale"] is None
    assert data["time_source"] == "system_clock_cst"
    assert _hv_count(db, "opportunities") == 1

    text = tsr.render_market_scan_ranks(data)
    assert "2026-11-06" in text
    assert "OHLC" in text
    assert "已覆盖 1 只" in text
    assert "main_net_wan" not in text


def test_ranks_missing_snapshot_is_honest_and_still_audited(db, set_now):
    user = _user(db, "pro")
    set_now(datetime(2026, 11, 6, 16, 0))

    data = tsr.read_market_scan_ranks(db, user)

    assert data["available"] is False
    assert data["snapshot_date"] is None
    assert data["freshness"] == "unknown"
    assert "暂无" in data["note"]
    assert "dark_top" not in data
    assert _hv_count(db, "opportunities") == 1

    text = tsr.render_market_scan_ranks(data)
    assert "暂无" in text
    assert "不猜" in text


def test_ranks_guest_denied_without_audit(db, set_now):
    user = _user(db, "guest")

    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, user)

    assert exc.value.status_code == 403
    assert _hv_count(db, "opportunities") == 0
    assert _hv_count(db, "dark_fund") == 0


def test_ranks_anonymous_denied_401(db, set_now):
    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, None)
    assert exc.value.status_code == 401


def test_ranks_member_trial_reuses_page_counter(db, set_now, monkeypatch):
    from src.core import permissions

    monkeypatch.setattr(
        permissions,
        "effective_trial_features",
        lambda db=None: {"view_opportunities": "机会", "view_dark": "暗盘资金"},
    )
    monkeypatch.setattr(permissions, "effective_trial_limit", lambda db=None: 3)
    user = _user(db, "member")
    set_now(datetime(2026, 11, 6, 16, 0))

    for _ in range(3):
        data = tsr.read_market_scan_ranks(db, user)
        assert data["available"] is False

    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, user)
    assert exc.value.status_code == 403
    assert _hv_count(db, "opportunities") == 3


def test_dark_fund_pro_user_tags_thsdk_dde(db, set_now):
    user = _user(db, "pro")
    _add_dark(db, "2026-11-06", DARK_PAYLOAD)
    set_now(datetime(2026, 11, 6, 16, 0))

    data = tsr.read_dark_fund_top(db, user)

    assert data["available"] is True
    assert data["snapshot_date"] == "2026-11-06"
    assert data["source"] == "thsdk_dde"
    assert data["caliber"] == "ths"
    assert data["top"][0]["source"] == "thsdk_dde"
    assert "dark_top" not in data
    assert data["freshness"] == "unknown"
    assert data["data_date"] is None
    assert _hv_count(db, "dark_fund") == 1


def test_dark_fund_missing_snapshot_and_denied(db, set_now):
    set_now(datetime(2026, 11, 6, 16, 0))
    pro = _user(db, "pro")
    data = tsr.read_dark_fund_top(db, pro)
    assert data["available"] is False
    assert "暂无" in data["note"]
    assert _hv_count(db, "dark_fund") == 1

    guest = _user(db, "guest")
    with pytest.raises(HTTPException) as exc:
        tsr.read_dark_fund_top(db, guest)
    assert exc.value.status_code == 403
    assert _hv_count(db, "dark_fund") == 1


def test_weekend_reads_friday_snapshot_without_fresh_claim(db, set_now):
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    set_now(datetime(2026, 11, 7, 10, 0))  # 周六

    data = tsr.read_market_scan_ranks(db, user)

    assert data["expected_data_day"] == "2026-11-06"
    assert data["stale"] is None
    assert data["freshness"] == "unknown"
    assert data["age_days"] is None
    text = tsr.render_market_scan_ranks(data)
    assert "未核实" in text and "已过期" not in text


def test_intraday_trading_day_does_not_expect_today(db, set_now):
    """交易日 10:00 时今日快照本不该有（cron 15:30 才落库），昨日快照不算过期。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 5), RANK_PAYLOAD)
    set_now(datetime(2026, 11, 6, 10, 0))  # 周五盘中

    data = tsr.read_market_scan_ranks(db, user)
    assert data["expected_data_day"] == "2026-11-05"
    assert data["stale"] is None
    assert data["freshness"] == "unknown"


def test_old_snapshot_not_dated_by_write_day(db, set_now):
    """两周前的写入日也不能被当成「数据日」, 更不能据此断言过期/新鲜。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 10, 30), RANK_PAYLOAD)
    set_now(datetime(2026, 11, 13, 16, 0))

    data = tsr.read_market_scan_ranks(db, user)

    assert data["stale"] is None
    assert data["freshness"] == "unknown"
    assert data["snapshot_date"] == "2026-10-30"
    assert data["data_date"] is None
    assert data["age_days"] is None
    assert data["expected_data_day"] == "2026-11-13"

    text = tsr.render_market_scan_ranks(data)
    assert "2026-10-30" in text
    assert "未核实" in text
    assert "已过期" not in text


def test_old_dark_snapshot_not_dated_by_write_day(db, set_now):
    user = _user(db, "pro")
    _add_dark(db, "2026-10-30", DARK_PAYLOAD)
    set_now(datetime(2026, 11, 13, 16, 0))

    data = tsr.read_dark_fund_top(db, user)

    assert data["stale"] is None
    assert data["data_date"] is None
    assert data["snapshot_date"] == "2026-10-30"
    text = tsr.render_dark_fund_top(data)
    assert "2026-10-30" in text and "未核实" in text
    assert "已过期" not in text


def test_reads_do_not_write_business_tables(db, set_now):
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    _add_dark(db, "2026-11-06", DARK_PAYLOAD)
    set_now(datetime(2026, 11, 6, 16, 0))
    before_ranks = db.query(MarketScanRank).count()
    before_dark = db.query(DarkFundTopSnapshot).count()

    tsr.read_market_scan_ranks(db, user)
    tsr.read_dark_fund_top(db, user)

    assert db.query(MarketScanRank).count() == before_ranks
    assert db.query(DarkFundTopSnapshot).count() == before_dark
    assert _hv_count(db, "opportunities") == 1
    assert _hv_count(db, "dark_fund") == 1


def test_market_filter_follows_page(db, set_now):
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD, market="CN")
    _add_rank(db, date(2026, 11, 6), {**RANK_PAYLOAD, "computed": 7}, market="US")
    set_now(datetime(2026, 11, 6, 16, 0))

    data = tsr.read_market_scan_ranks(db, user, market="CN")
    assert data["market"] == "CN"
    assert data["computed"] == 4990
