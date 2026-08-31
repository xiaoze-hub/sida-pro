"""数据质量哨兵单元测试(2026-08-21)。

覆盖: NULL 检查触发 / 逐笔总额对账 fail / 全 ok 不写通知 / 有 warn 写通知。
用内存 SQLite(create_all) + monkeypatch compute_dark_flow / TencentQuoteVendor,
不触网。
"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core import data_quality_sentinel as dq
from src.core.data_quality_sentinel import run_dq_checks
from src.web.database import Base
from src.web.models import Notification, Stock


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def afternoon(monkeypatch):
    """把哨兵时钟锁在盘后 16:00(触发逐笔对账)。"""
    monkeypatch.setattr(dq, "_now", lambda: datetime(2026, 8, 21, 16, 0, 0))


def _add_stock(db, symbol="002361", market="CN"):
    db.add(Stock(symbol=symbol, name=symbol, market=market))
    db.commit()


class _FakeVendor:
    """替身 TencentQuoteVendor: 只回传指定 turnover。

    哨兵以 `TencentQuoteVendor()` 无参实例化后调 fetch, 故这里用类属性缺省值。
    """

    turnover = 10_000_000

    def __init__(self):
        pass

    def fetch(self, symbols, opts):
        return [SimpleNamespace(turnover=self.turnover)]


def _fake_dark_flow(total):
    """替身 compute_dark_flow: 返回 buy_amt+sell_amt=total, 各分一半。"""

    def _f(sym):
        return {"buy_amt": total / 2, "sell_amt": total / 2, "tick_count": 100,
                "data_status": "ok"}

    return _f


class TestNullCreatedAt:
    def test_null_triggers_warn_and_notify(self, db, afternoon, monkeypatch):
        """三表皆置 created_at=NULL, 应 warn 并调用 push_notification。

        注意: ORM 传 None 会被 server_default=func.now() 覆盖成当前时间,
        必须用原始 SQL 显式写 NULL 才能模拟真实问题数据。
        """
        db.execute(text(
            "INSERT INTO stock_suggestions (stock_symbol, stock_market, action,"
            " action_label, agent_name, created_at)"
            " VALUES ('002361','CN','buy','建仓','daily_report', NULL)"
        ))
        db.execute(text(
            "INSERT INTO notifications (category, level, title, created_at)"
            " VALUES ('system','info','x', NULL)"
        ))
        db.execute(text(
            "INSERT INTO agent_runs (agent_name, status, created_at)"
            " VALUES ('a','success', NULL)"
        ))
        db.commit()

        pushed = []
        monkeypatch.setattr(
            "src.core.notify_center.push_notification",
            lambda **kw: pushed.append(kw) or 1,
        )

        res = run_dq_checks(db)

        null_check = next(c for c in res["checks"] if c["check"] == "null_created_at")
        assert null_check["status"] == "warn"
        assert null_check["value"]["stock_suggestions"] == 1
        assert null_check["value"]["notifications"] == 1
        assert null_check["value"]["agent_runs"] == 1
        # 三表 NULL>0 触发 warn → 聚合 warn → 写 warning
        assert res["overall"] == "warn"
        assert len(pushed) == 1
        assert pushed[0]["level"] == "warning"
        assert pushed[0]["category"] == "system"
        assert pushed[0]["source"] == "data_quality_sentinel"


class TestTickReconciliation:
    def test_fail_when_total_huge(self, db, afternoon, monkeypatch):
        """逐笔总额 = 1600万, 实际成交额 = 1000万 → 160% > 130% → fail。"""
        _add_stock(db, "002361")
        monkeypatch.setattr(dq, "compute_dark_flow", _fake_dark_flow(16_000_000))
        monkeypatch.setattr(dq, "TencentQuoteVendor", _FakeVendor)

        pushed = []
        monkeypatch.setattr(
            "src.core.notify_center.push_notification",
            lambda **kw: pushed.append(kw) or 1,
        )

        res = run_dq_checks(db)

        tick = next(c for c in res["checks"] if c["check"] == "tick_reconciliation")
        assert tick["status"] == "fail"
        assert res["overall"] == "fail"
        assert len(pushed) == 1
        assert pushed[0]["level"] == "error"

    def test_skipped_before_eod(self, db, monkeypatch):
        """盘中(14:00)不做逐笔对账, 该项为 ok。"""
        monkeypatch.setattr(dq, "_now", lambda: datetime(2026, 8, 21, 14, 0, 0))
        _add_stock(db, "002361")
        res = run_dq_checks(db)
        tick = next(c for c in res["checks"] if c["check"] == "tick_reconciliation")
        assert tick["status"] == "ok"
        assert "跳过" in tick["detail"]


class TestNoNotificationWhenAllOk:
    def test_all_ok_no_notify(self, db, monkeypatch):
        """全 ok(空表 + 盘中跳过对账) → 不写 notification。"""
        monkeypatch.setattr(dq, "_now", lambda: datetime(2026, 8, 21, 10, 0, 0))
        res = run_dq_checks(db)
        assert res["overall"] == "ok"
        assert all(c["status"] == "ok" for c in res["checks"])
        notif = db.query(Notification).filter(
            Notification.source == "data_quality_sentinel"
        ).all()
        assert notif == []


class TestWarnWritesNotification:
    def test_warn_writes_warning_notify(self, db, afternoon, monkeypatch):
        """逐笔总额 120% → warn → 调用 push_notification(level=warning)。"""
        _add_stock(db, "002361")
        monkeypatch.setattr(dq, "compute_dark_flow", _fake_dark_flow(12_000_000))
        monkeypatch.setattr(dq, "TencentQuoteVendor", _FakeVendor)

        pushed = []
        monkeypatch.setattr(
            "src.core.notify_center.push_notification",
            lambda **kw: pushed.append(kw) or 1,
        )

        res = run_dq_checks(db)

        tick = next(c for c in res["checks"] if c["check"] == "tick_reconciliation")
        assert tick["status"] == "warn"
        assert res["overall"] == "warn"
        assert len(pushed) == 1
        assert pushed[0]["level"] == "warning"
        assert pushed[0]["category"] == "system"


class TestFailureNotifications:
    def test_many_failures_warn(self, db, afternoon):
        """近24h 含 '失败' 的通知 >10 → warn。"""
        base = datetime(2026, 8, 21, 16, 0, 0)
        for i in range(12):
            db.add(Notification(
                category="report", level="error",
                title=f"获取失败 report {i}",
                created_at=base - __import__("datetime").timedelta(hours=i),
            ))
        db.commit()
        res = run_dq_checks(db)
        fc = next(c for c in res["checks"] if c["check"] == "failure_notifications")
        assert fc["status"] == "warn"
        assert fc["value"] >= 12
