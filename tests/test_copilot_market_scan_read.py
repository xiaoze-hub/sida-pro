# -*- coding: utf-8 -*-
"""数智参谋市场扫描榜 / DDE 榜只读适配层单测（2026-11-07）。

覆盖:
  - 权限链与页面同源: view_opportunities / view_dark, 无权限 403、未登录 401
  - 高价值审计: 每次读取(含无快照)按页面同标签 opportunities / dark_fund 落库
  - 缺数据: available=False + note, 不编造榜单
  - 两个榜来源严格分离: 三榜 dark_top=OHLC 近似, 暗盘 DDE 榜=thsdk_dde
  - 过时快照: 明确返回并渲染快照日期; 周末读周五快照不算过时
  - 只读: 不触发 scan/refresh, 两榜业务表行数不变

DB 隔离: 每个用例独立 tmp_path SQLite 文件, 绝不碰仓库 data/ 或生产库。
"""
from __future__ import annotations

from datetime import date

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


def _user(db, role: str = "pro"):
    u = make_user(role=role)
    db.add(u)
    db.commit()
    return u


def _add_rank(db, snap, payload, market: str = "CN") -> MarketScanRank:
    row = MarketScanRank(snapshot_date=snap, stock_market=market, payload=payload)
    db.add(row)
    db.commit()
    return row


def _add_dark(db, snap: str, payload: dict, market: str = "CN") -> DarkFundTopSnapshot:
    row = DarkFundTopSnapshot(snapshot_date=snap, stock_market=market, payload=payload)
    db.add(row)
    db.commit()
    return row


def _hv_count(db, api_name: str) -> int:
    return (
        db.query(HighValueApiLog)
        .filter(HighValueApiLog.api_name == api_name)
        .count()
    )


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


# ────────────────────────────── 三榜读取 ──────────────────────────────


def test_ranks_pro_user_gets_latest_snapshot_and_audit(db):
    """带用户: pro 读到最新快照; 标签 opportunities 落一条审计。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 5), {**RANK_PAYLOAD, "generated_at": "old"})
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)

    data = tsr.read_market_scan_ranks(db, user, as_of=date(2026, 11, 7))

    assert data["available"] is True
    assert data["snapshot_date"] == "2026-11-06"
    assert data["market"] == "CN"
    assert data["source"] == "market_scan_ranks"
    # 三榜里的暗盘榜是 OHLC 近似, 必须在信封上显式标注
    assert data["dark_top_source"] == "ohlc_approx"
    assert data["dark_top"][0]["approximation"] is True
    assert data["new_g_points"][0]["symbol"] == "000001"
    assert data["activity_top"][0]["activity"] == 6.5
    assert data["stale"] is False
    assert _hv_count(db, "opportunities") == 1

    text = tsr.render_market_scan_ranks(data)
    assert "2026-11-06" in text
    assert "OHLC" in text
    # ZLJC 是 {代码: {...}} 的全市场映射, 只能摘要覆盖数, 不能把原 dict 倒进上下文
    assert "已覆盖 1 只" in text
    assert "main_net_wan" not in text


def test_ranks_missing_snapshot_is_honest_and_still_audited(db):
    """缺数据: available=False + 明确 note, 不编造; 审计时点仍在读取前(照落)。"""
    user = _user(db, "pro")

    data = tsr.read_market_scan_ranks(db, user, as_of=date(2026, 11, 7))

    assert data["available"] is False
    assert data["snapshot_date"] is None
    assert "暂无" in data["note"]
    assert "dark_top" not in data
    assert _hv_count(db, "opportunities") == 1

    text = tsr.render_market_scan_ranks(data)
    assert "暂无" in text
    assert "不猜" in text


def test_ranks_guest_denied_without_audit(db):
    """无权限: guest 被 view_opportunities 拒(403), 不落高价值审计。"""
    user = _user(db, "guest")

    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, user, as_of=date(2026, 11, 7))

    assert exc.value.status_code == 403
    assert _hv_count(db, "opportunities") == 0
    assert _hv_count(db, "dark_fund") == 0


def test_ranks_anonymous_denied_401(db):
    """未登录: 与页面一致 401, 不放行内部系统口径。"""
    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, None, as_of=date(2026, 11, 7))
    assert exc.value.status_code == 401


def test_ranks_member_trial_reuses_page_counter(db, monkeypatch):
    """member 试用: 每次调用消耗一次 view_opportunities 配额, 4 次被拒。"""
    from src.core import permissions

    # 钉死免费档口径, 免遭同进程其他用例改配置/30s 缓存影响而 flaky
    monkeypatch.setattr(
        permissions,
        "effective_trial_features",
        lambda db=None: {"view_opportunities": "机会", "view_dark": "暗盘资金"},
    )
    monkeypatch.setattr(permissions, "effective_trial_limit", lambda db=None: 3)
    user = _user(db, "member")

    for _ in range(3):
        data = tsr.read_market_scan_ranks(db, user, as_of=date(2026, 11, 7))
        assert data["available"] is False

    with pytest.raises(HTTPException) as exc:
        tsr.read_market_scan_ranks(db, user, as_of=date(2026, 11, 7))
    assert exc.value.status_code == 403
    # 3 次成功读取各落 1 条; 第 4 次在权限层被拦, 不再落审计
    assert _hv_count(db, "opportunities") == 3


# ────────────────────────────── DDE 榜读取 ──────────────────────────────


def test_dark_fund_pro_user_tags_thsdk_dde(db):
    """正常来源: DDE 榜标 thsdk_dde, 与三榜的 OHLC 近似榜不混用。"""
    user = _user(db, "pro")
    _add_dark(db, "2026-11-06", DARK_PAYLOAD)

    data = tsr.read_dark_fund_top(db, user, as_of=date(2026, 11, 7))

    assert data["available"] is True
    assert data["snapshot_date"] == "2026-11-06"
    assert data["source"] == "thsdk_dde"
    assert data["caliber"] == "ths"
    assert data["top"][0]["source"] == "thsdk_dde"
    assert "dark_top" not in data
    assert _hv_count(db, "dark_fund") == 1

    text = tsr.render_dark_fund_top(data)
    assert "DDE" in text
    assert "2026-11-06" in text
    assert "OHLC" in text  # 明确声明与 OHLC 近似榜不是同一口径


def test_dark_fund_missing_snapshot_and_denied(db):
    """缺数据不编造; guest 被 view_dark 拒且不落审计。"""
    pro = _user(db, "pro")
    data = tsr.read_dark_fund_top(db, pro, as_of=date(2026, 11, 7))
    assert data["available"] is False
    assert "暂无" in data["note"]
    assert _hv_count(db, "dark_fund") == 1

    guest = _user(db, "guest")
    with pytest.raises(HTTPException) as exc:
        tsr.read_dark_fund_top(db, guest, as_of=date(2026, 11, 7))
    assert exc.value.status_code == 403
    assert _hv_count(db, "dark_fund") == 1


# ────────────────────────────── 过时快照口径 ──────────────────────────────


def test_weekend_reads_friday_snapshot_as_fresh(db):
    """周六读周五快照: 最近应更新交易日=周五, 不算过时(不能用自然日硬判)。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)

    data = tsr.read_market_scan_ranks(db, user, as_of=date(2026, 11, 7))

    assert data["expected_data_day"] == "2026-11-06"
    assert data["stale"] is False
    assert data["age_days"] == 1
    assert "已过期" not in tsr.render_market_scan_ranks(data)


def test_stale_snapshot_is_explicitly_dated(db):
    """过时快照: 返回 stale/落后交易日, 渲染写清快照日期, 不冒充当日。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 10, 30), RANK_PAYLOAD)

    data = tsr.read_market_scan_ranks(db, user, as_of=date(2026, 11, 13))

    assert data["stale"] is True
    assert data["snapshot_date"] == "2026-10-30"
    assert data["expected_data_day"] == "2026-11-13"
    assert data["age_days"] == 14
    assert data["age_trading_days"] == 10

    text = tsr.render_market_scan_ranks(data)
    assert "2026-10-30" in text
    assert "2026-11-13" in text
    assert "已过期" in text


def test_stale_dark_snapshot_is_explicitly_dated(db):
    user = _user(db, "pro")
    _add_dark(db, "2026-10-30", DARK_PAYLOAD)

    data = tsr.read_dark_fund_top(db, user, as_of=date(2026, 11, 13))

    assert data["stale"] is True
    assert data["snapshot_date"] == "2026-10-30"
    text = tsr.render_dark_fund_top(data)
    assert "2026-10-30" in text
    assert "已过期" in text


# ────────────────────────────── 只读保证 ──────────────────────────────


def test_reads_do_not_write_business_tables(db):
    """只读: 两个榜各读一次, 快照业务表行数不变, 只多出审计行。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD)
    _add_dark(db, "2026-11-06", DARK_PAYLOAD)
    before_ranks = db.query(MarketScanRank).count()
    before_dark = db.query(DarkFundTopSnapshot).count()

    tsr.read_market_scan_ranks(db, user, as_of=date(2026, 11, 7))
    tsr.read_dark_fund_top(db, user, as_of=date(2026, 11, 7))

    assert db.query(MarketScanRank).count() == before_ranks
    assert db.query(DarkFundTopSnapshot).count() == before_dark
    assert _hv_count(db, "opportunities") == 1
    assert _hv_count(db, "dark_fund") == 1


def test_market_filter_follows_page(db):
    """market 过滤与页面一致: 读 CN 不串到别的市场。"""
    user = _user(db, "pro")
    _add_rank(db, date(2026, 11, 6), RANK_PAYLOAD, market="CN")
    _add_rank(db, date(2026, 11, 6), {**RANK_PAYLOAD, "computed": 7}, market="US")

    data = tsr.read_market_scan_ranks(db, user, market="CN", as_of=date(2026, 11, 7))
    assert data["market"] == "CN"
    assert data["computed"] == 4990
