"""市场扫描 CN-only 过滤 + 候选池只存 active 有信号 + stocks 去重迁移 测试。

覆盖 2026-08 数据噪音修复:
1. market_scan 自动扫描只跑 CN, 港美股不生成快照/候选(discovery 手动查询不受影响)。
2. entry_candidates 只落库 active 且有明确信号的记录, 无信号不占位。
3. stocks 表 (symbol, market, user_id) 去重迁移 + 唯一约束。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import src.core.entry_candidates as ec
from src.web import models  # noqa: F401  注册 ORM
from src.web.database import Base
from src.web.migrations import MIGRATIONS, _m121_dedupe_stocks_unique


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _make_temp_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


STRONG_KLINE = {
    "trend": "多头排列",
    "macd_cross": "金叉",
    "volume_ratio": 2.5,
    "last_close": 100.0,
    "support_m": 95.0,
    "resistance_m": 108.0,
    "rsi_status": "偏强",
    "kdj_status": "金叉",
}

WEAK_KLINE = {
    "trend": "均线交织",
    "macd_cross": "",
    "volume_ratio": 1.0,
    "last_close": 100.0,
}


def _fake_quote_row(symbol: str, price: float = 100.0):
    return SimpleNamespace(
        symbol=symbol,
        current_price=price,
        change_pct=2.0,
        turnover=5e9,
        volume=1e6,
        name=f"股票{symbol}",
    )


def _make_collector_fake(strong_symbols, weak_symbols, calls: list):
    """返回 (fetch_hot_stocks, quote_rows): 记录被请求的市场, CN 返回强/弱行。"""

    def _rows(market: str):
        out = []
        for i, sym in enumerate(strong_symbols):
            out.append(
                SimpleNamespace(
                    symbol=sym, market=market, name=f"股票{sym}",
                    price=100.0 + i, change_pct=2.0, turnover=5e9, volume=1e6,
                )
            )
        for i, sym in enumerate(weak_symbols):
            out.append(
                SimpleNamespace(
                    symbol=sym, market=market, name=f"股票{sym}",
                    price=50.0 + i, change_pct=0.2, turnover=1e7, volume=1e4,
                )
            )
        return out

    async def fake_fetch_hot_stocks(self, *, market: str, mode: str, limit: int):
        calls.append((market, mode))
        return _rows(market)

    return fake_fetch_hot_stocks


# ---------------------------------------------------------------------------
# 1) market_scan 只扫 CN
# ---------------------------------------------------------------------------

class TestMarketScanCnOnly:
    def test_load_market_scan_inputs_only_cn(self, tmp_path, monkeypatch):
        engine = _make_temp_engine(tmp_path)
        monkeypatch.setattr(ec, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setattr(ec, "get_global_proxy", lambda: None)

        calls: list[tuple[str, str]] = []
        strong = [f"600{100 + i:03d}" for i in range(12)]
        weak = [f"300{800 + i:03d}" for i in range(3)]
        fake = _make_collector_fake(strong, weak, calls)
        monkeypatch.setattr(ec.EastMoneyDiscoveryCollector, "fetch_hot_stocks", fake)
        monkeypatch.setattr(ec, "md_stock_data", lambda *a, **k: [])

        result = ec._load_market_scan_inputs(limit_per_market=60)

        # 只请求了 CN 市场
        assert calls, "collector 未被调用"
        assert {m for m, _ in calls} == {"CN"}, f"不应请求 HK/US, 实际: {set(m for m, _ in calls)}"
        # 结果里只有 CN: 前缀
        assert all(k.startswith("CN:") for k in result), [k for k in result if not k.startswith("CN:")]
        assert len(result) >= 12

    def test_refresh_entry_candidates_cn_only_and_active_only(self, tmp_path, monkeypatch):
        engine = _make_temp_engine(tmp_path)
        monkeypatch.setattr(ec, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setattr(ec, "get_global_proxy", lambda: None)

        calls: list[tuple[str, str]] = []
        strong = [f"600{100 + i:03d}" for i in range(12)]
        weak = [f"300{800 + i:03d}" for i in range(3)]
        fake = _make_collector_fake(strong, weak, calls)
        monkeypatch.setattr(ec.EastMoneyDiscoveryCollector, "fetch_hot_stocks", fake)
        monkeypatch.setattr(ec, "md_stock_data", lambda syms, mkt: [_fake_quote_row(s) for s in syms])

        class FakeKlineCollector:
            def __init__(self, market):
                self.market = market

            def get_kline_summary(self, symbol):
                if symbol in weak:
                    return dict(WEAK_KLINE)
                return dict(STRONG_KLINE)

        monkeypatch.setattr(ec, "KlineCollector", FakeKlineCollector)

        snapshot = "2026-01-15"
        result = ec.refresh_entry_candidates(
            snapshot_date=snapshot,
            market_scan_limit=20,
            max_kline_symbols=60,
        )

        assert {m for m, _ in calls} == {"CN"}

        db = sessionmaker(bind=engine)()
        try:
            cands = db.query(ec.EntryCandidate).filter(
                ec.EntryCandidate.snapshot_date == snapshot
            ).all()
            snaps = db.query(ec.MarketScanSnapshot).filter(
                ec.MarketScanSnapshot.snapshot_date == snapshot
            ).all()
        finally:
            db.close()

        # 快照全部是 CN
        assert snaps, "应有快照落库"
        assert all(s.stock_market == "CN" for s in snaps), [
            s.stock_market for s in snaps
        ]
        assert len(snaps) == 15

        # 候选全部是 CN + active + 有明确信号
        assert len(cands) == 12, f"应只落库 12 条 active 信号, 实际 {len(cands)}"
        assert all(c.stock_market == "CN" for c in cands)
        assert all(c.status == "active" for c in cands)
        assert all(ec._has_real_signal(c.signal) for c in cands), [
            c.signal for c in cands if not ec._has_real_signal(c.signal)
        ]
        assert result["count"] == 12
        assert result["filtered"] == 3

    def test_has_real_signal(self):
        assert ec._has_real_signal("趋势延续，MACD金叉") is True
        assert ec._has_real_signal("  ") is False
        assert ec._has_real_signal("") is False
        assert ec._has_real_signal("暂无明确信号") is False
        assert ec._has_real_signal(None) is False


# ---------------------------------------------------------------------------
# 3) stocks 去重迁移
# ---------------------------------------------------------------------------

class TestStocksDedupeMigration:
    def _seed(self, engine):
        """构造含重复自选股 + 关联引用的数据。"""
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, username, password_hash, role, is_active, token_version) "
                     "VALUES ('u1','user1','x','owner',1,1), ('u2','user2','x','member',1,1)")
            )
            # (id, symbol, name, market, user_id)
            stocks = [
                (1, "600519", "贵州茅台", "CN", "u1"),
                (2, "600519", "贵州茅台", "CN", "u1"),  # dup
                (3, "600519", "贵州茅台", "CN", "u1"),  # dup
                (4, "000815", "美利云", "CN", "u1"),
                (5, "000815", "美利云", "CN", "u1"),  # dup
                (6, "00700", "腾讯控股", "HK", "u2"),
                (7, "00700", "腾讯控股", "HK", "u1"),  # 不同用户, 非重复
                (8, "AAPL", "苹果", "US", None),  # legacy NULL user
                (9, "AAPL", "苹果", "US", None),  # dup(NULL 组)
            ]
            for sid, sym, name, mkt, uid in stocks:
                conn.execute(
                    text("INSERT INTO stocks (id, symbol, name, market, user_id) "
                         "VALUES (:id, :sym, :name, :mkt, :uid)"),
                    {"id": sid, "sym": sym, "name": name, "mkt": mkt, "uid": uid},
                )
            conn.execute(text("INSERT INTO accounts (id, name) VALUES (1, '默认账户')"))
            # 持仓: 一只挂在 dup 股票上, 一只挂在保留股票上, 一只 dup 且同账户冲突
            conn.execute(
                text("INSERT INTO positions (id, account_id, stock_id, cost_price, quantity) "
                     "VALUES (1, 1, 2, 100.0, 100), (2, 1, 4, 10.0, 500), (3, 1, 3, 99.0, 50)")
            )
            # Agent: 一个挂在 dup 股票上且与保留行 agent_name 冲突
            conn.execute(
                text("INSERT INTO stock_agents (id, stock_id, agent_name) "
                     "VALUES (1, 3, 'agentA'), (2, 1, 'agentB'), (3, 2, 'agentA')")
            )
            # 价格提醒: 引用 dup 股票
            conn.execute(
                text("INSERT INTO price_alert_rules (id, stock_id, name) VALUES (1, 2, 'r1')")
            )
            conn.execute(
                text("INSERT INTO price_alert_hits (id, rule_id, stock_id, trigger_bucket) "
                     "VALUES (1, 1, 2, '202601010000')")
            )

    def test_dedupe_and_unique_constraint(self, tmp_path):
        engine = _make_temp_engine(tmp_path)
        self._seed(engine)

        with engine.begin() as conn:
            _m121_dedupe_stocks_unique(conn)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, symbol, market, user_id FROM stocks ORDER BY id")).fetchall()
        # 保留最早一条: 600519→1, 000815→4, 00700(u2)→6, 00700(u1)→7, AAPL(NULL)→8
        assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
            (1, "600519", "CN", "u1"),
            (4, "000815", "CN", "u1"),
            (6, "00700", "HK", "u2"),
            (7, "00700", "HK", "u1"),
            (8, "AAPL", "US", None),
        ]

        with engine.connect() as conn:
            # positions: dup 引用已重指到保留行; 同账户冲突的已去重
            poss = conn.execute(text("SELECT id, stock_id FROM positions ORDER BY id")).fetchall()
            assert [(p[0], p[1]) for p in poss] == [(1, 1), (2, 4)]
            # stock_agents: 冲突的 agentA 行保留最早一条并重指到 keep
            ags = conn.execute(text("SELECT id, stock_id, agent_name FROM stock_agents ORDER BY id")).fetchall()
            assert [(a[0], a[1], a[2]) for a in ags] == [(1, 1, "agentA"), (2, 1, "agentB")]
            # 价格提醒引用重指
            rules = conn.execute(text("SELECT stock_id FROM price_alert_rules")).fetchall()
            assert [r[0] for r in rules] == [1]
            hits = conn.execute(text("SELECT stock_id FROM price_alert_hits")).fetchall()
            assert [h[0] for h in hits] == [1]
            # 唯一索引存在
            idx = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'uq_stocks_%'")
            ).fetchall()
            names = {r[0] for r in idx}
            assert "uq_stocks_symbol_market_user" in names
            assert "uq_stocks_symbol_market_nulluser" in names

        # 唯一约束生效: 再次插入重复自选应失败
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO stocks (symbol, name, market, user_id) "
                         "VALUES ('600519', '贵州茅台', 'CN', 'u1')")
                )
        # NULL user_id 重复也失败(部分唯一索引)
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO stocks (symbol, name, market, user_id) "
                         "VALUES ('AAPL', '苹果', 'US', NULL)")
                )

    def test_idempotent(self, tmp_path):
        engine = _make_temp_engine(tmp_path)
        self._seed(engine)
        with engine.begin() as conn:
            _m121_dedupe_stocks_unique(conn)
        with engine.begin() as conn:
            _m121_dedupe_stocks_unique(conn)  # 再跑一次不报错
        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM stocks")).scalar()
        assert n == 5

    def test_registered_in_migrations(self):
        versions = {m.version for m in MIGRATIONS}
        assert 121 in versions
        assert any(m.version == 121 and m.name == "dedupe_stocks_unique" for m in MIGRATIONS)

    def test_runs_on_real_dev_db_copy(self, tmp_path):
        """对当前 data/panwatch.db 的副本跑迁移, 确认可成功执行且不报错。"""
        import os
        import shutil
        src = "/tmp/PanWatch/data/panwatch.db"
        dst = tmp_path / "copy.db"
        if not os.path.exists(src):
            pytest.skip("开发库不存在")
        shutil.copy2(src, dst)
        engine = create_engine(f"sqlite:///{dst}")
        with engine.begin() as conn:
            _m121_dedupe_stocks_unique(conn)
        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM stocks")).scalar()
            assert isinstance(n, int) and n >= 0
