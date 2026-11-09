"""龙虎榜日快照完整性定性(`dragon_tiger_daily` 提前冻结残缺, 2026-11-09)。

缺陷(代码路径已核):
  `/api/market-data/dragon-tiger/{date}` 在库里**没有**该日行时才回源东财并 upsert
  (`market_data.dragon_tiger_proxy` → `_lhb_upsert_rows`); 一旦该日有了行, 读侧就
  走 `_dragon_tiger_from_db()` 的短路 —— **历史日永不回源**。东财当日完整榜单最早
  约 17:16、稳定 17:30 才齐, 而请求可以发生在 16:26–16:51 —— 那一刻落库的就是尚未
  发布完的残缺榜单, 之后被"已落库 → 读库"规则**永久冻结**, 且对外仍然报
  `count=N / source=db`, 看不出残缺。

本文件锁定的契约:
  ① 不得凭**股数一致**宣称完整 —— `dragon_tiger_events` / `lhb_seat_details` 是独立
     来源, 不是官方完整清单, 只能当下界(能证伪, 不能证明);
  ② 缺口必须**可证明**: 参考源里有、快照里没有的 symbol 要逐个列出;
  ③ 首写落在发布就绪窗口之前 → `early_freeze=True`(一维证据, 不等于已证明完整);
  ④ 只有"权威完整清单已覆盖"才允许 `status=complete`, 其余一律 `degraded=True`;
  ⑤ 定性过程**只读**, 不写库、不回源(写入需另行授权);
  ⑥ 现有读库响应键(`count/items/source/data_origin/pending`)保持兼容, 只增不改。

全部禁真实网络(vendor 打桩, 库用内存 SQLite)。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.web.api.market_data as mdm
from src.core import lhb_quality as q
from src.web.migrations import _m160_lhb_detail_tables, _m192_dragon_tiger_daily_table


# ─────────────────────────── 夹具 ───────────────────────────


def _mk_engine(with_seats: bool = False, with_events: bool = False):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m192_dragon_tiger_daily_table(conn)
    if with_seats:
        with eng.begin() as conn:
            _m160_lhb_detail_tables(conn)
    if with_events:
        from src.web.migrations import _m156_dragon_tiger_events_table

        with eng.begin() as conn:
            _m156_dragon_tiger_events_table(conn)
    return eng


def _use_engine(monkeypatch, eng):
    import src.web.database as database

    monkeypatch.setattr(database, "engine", eng)


def _add_daily(eng, rows, trade_date: str = "20261019"):
    """rows: [(symbol, updated_at)] → 直插 dragon_tiger_daily(绕过 upsert 便于控首写时刻)。"""
    with eng.begin() as conn:
        for sym, ts in rows:
            conn.execute(
                text(
                    "INSERT INTO dragon_tiger_daily "
                    "(trade_date, symbol, market, name, reason, close, net_buy, source, updated_at) "
                    "VALUES (:d, :s, 'CN', '某票', '日涨幅偏离值达7%', 10.0, 1.0, "
                    "'em_ftshare', :ts)"
                ),
                {"d": trade_date, "s": sym, "ts": ts},
            )


def _add_seats(eng, symbols, trade_date: str = "20261019"):
    with eng.begin() as conn:
        for i, sym in enumerate(symbols):
            conn.execute(
                text(
                    "INSERT INTO lhb_seat_details "
                    "(row_uid, trade_date, symbol, name, reason, side, operate_dept_code, "
                    "operate_dept_name, buy_amt, "
                    "sell_amt, net_amt) "
                    "VALUES (:uid, :d, :s, '某票', '日涨幅偏离值达7%', 'buy', '10031935', '某营业部', "
                    "1000.0, 0.0, 1000.0)"
                ),
                {"uid": f"u{i}", "d": trade_date, "s": sym},
            )


# ─────────────────── ① 纯函数: 不得凭股数一致宣称完整 ───────────────────

def test_无快照判absent():
    r = q.assess_daily_completeness("20261019", actual_symbols=[])
    assert r["status"] == "absent" and r["degraded"] is True
    assert r["complete"] is False and r["actual_symbols"] == 0


def test_仅与独立源股数相同_不得宣称完整():
    syms = ["000001", "000002", "600000"]
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=syms,
        references={"lhb_seat_details": syms, "dragon_tiger_events": syms},
    )
    # 股数完全一致, 但参考源不是官方完整清单 → 只能"未核对"
    assert r["status"] == "unverified"
    assert r["complete"] is False and r["degraded"] is True
    assert r["missing_total"] == 0
    assert r["references"][0]["relation"] == "equal"
    assert r["references"][0]["authoritative"] is False
    assert "股数一致" in r["note"]
    assert all(c["missing"] == 0 for c in r["references"])


def test_同股数但集合不同_判残缺():
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001", "000002"],
        references={"lhb_seat_details": ["000001", "600000"]},
    )
    assert r["status"] == "partial"
    assert r["missing_symbols"] == ["600000"]
    assert r["missing_total"] == 1


# ─────────────────── ② 可证明的缺口 ───────────────────

def test_独立源多出标的_判残缺并列出缺口():
    actual = [f"60{i:04d}" for i in range(39)]
    ref = actual + [f"00{i:04d}" for i in range(28)]
    r = q.assess_daily_completeness(
        "20261019", actual_symbols=actual, references={"lhb_seat_details": ref}
    )
    assert r["status"] == "partial" and r["degraded"] is True
    assert r["actual_symbols"] == 39
    assert r["missing_total"] == 28 and len(r["missing_symbols"]) == 20  # 样本截断
    chk = r["references"][0]
    assert chk["reference"] == "lhb_seat_details"
    assert chk["reference_symbols"] == 67 and chk["missing"] == 28
    assert chk["relation"] == "subset"
    assert "非官方完整清单" in chk["note"]
    assert any("lhb_seat_details" in e for e in r["evidence"])


def test_缺口计数全量_不因样本截断而缩水():
    actual = {"600000"}
    ref = {f"60{i:04d}" for i in range(50)}
    r = q.assess_daily_completeness(
        "20261019", actual_symbols=actual, references={"lhb_seat_details": ref},
        missing_sample=5,
    )
    assert r["missing_total"] == 49
    assert len(r["missing_symbols"]) == 5


def test_带后缀或空白的标的按同码归一():
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001.SZ", " 600000 "],
        references={"lhb_seat_details": ["000001", "600000"]},
    )
    assert r["actual_symbols"] == 2 and r["missing_total"] == 0
    assert r["status"] == "unverified"


# ─────────────────── ③ 提前冻结证据 ───────────────────

def test_首写早于发布窗口_标记提前冻结():
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001"],
        first_write_at=datetime(2026, 10, 19, 16, 48),
    )
    assert r["early_freeze"] is True
    assert r["first_write_at"] == "2026-10-19 16:48:00"
    assert r["publish_ready_after"] == "17:16"
    assert r["status"] == "unverified"          # 时间只是证据, 不足以下"残缺"结论
    assert r["degraded"] is True and r["complete"] is False
    assert any("提前冻结" in e for e in r["evidence"])


def test_首写晚于发布窗口_无早冻标记():
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001"],
        first_write_at=datetime(2026, 10, 19, 17, 31),
    )
    assert r["early_freeze"] is False and r["degraded"] is True


def test_首写不在交易当日_不算提前冻结():
    # 次日才补写(修复性写入) → 不是"发布未完就冻结"
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001"],
        first_write_at=datetime(2026, 10, 20, 9, 10),
    )
    assert r["early_freeze"] is False


def test_发布窗口边界_恰好就绪不算提前冻结():
    at_ready = q.assess_daily_completeness(
        "20261019", actual_symbols=["000001"], first_write_at=datetime(2026, 10, 19, 17, 16)
    )
    just_before = q.assess_daily_completeness(
        "20261019", actual_symbols=["000001"], first_write_at=datetime(2026, 10, 19, 17, 15)
    )
    assert at_ready["early_freeze"] is False
    assert just_before["early_freeze"] is True


def test_带时区的首写时刻按北京时间归一():
    from datetime import timezone

    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001"],
        first_write_at=datetime(2026, 10, 19, 8, 48, tzinfo=timezone.utc),
    )
    assert r["first_write_at"] == "2026-10-19 16:48:00"
    assert r["early_freeze"] is True


# ─────────────────── ④ 只有权威清单覆盖才可 complete ───────────────────

def test_权威完整清单覆盖_才可判完整():
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001", "600000", "600001"],
        expected_symbols=["000001", "600000"],
        expected_source="exchange_official",
        expected_authoritative=True,
        first_write_at=datetime(2026, 10, 19, 16, 30),   # 即便早写, 覆盖已证明
    )
    assert r["status"] == "complete"
    assert r["complete"] is True and r["degraded"] is False
    assert r["expected"]["covered"] is True
    assert r["expected"]["source"] == "exchange_official"


def test_权威清单未覆盖_仍判残缺():
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001"],
        expected_symbols=["000001", "600000"],
        expected_source="exchange_official",
        expected_authoritative=True,
    )
    assert r["status"] == "partial" and r["complete"] is False
    assert r["missing_total"] == 1


def test_非权威预期清单不判完整():
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001", "600000"],
        expected_symbols=["000001", "600000"],
        expected_source="dragon_tiger_events",
        expected_authoritative=False,
    )
    assert r["status"] == "unverified" and r["degraded"] is True
    assert r["expected"]["authoritative"] is False


# ─────────────────── ⑤ 读库侧: 只读定性 ───────────────────

def test_读库定性_残缺快照带缺口与早冻证据(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    # SQLite CURRENT_TIMESTAMP 是 UTC naive: 08:48Z == 北京 16:48(早于 17:16)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00"), ("000002", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001", "000002", "600000"])

    r = q.assess_date("20261019")
    assert r["status"] == "partial" and r["degraded"] is True
    assert r["actual_symbols"] == 2
    assert r["first_write_at"] == "2026-10-19 16:48:00"   # UTC → 北京
    assert r["early_freeze"] is True
    assert r["missing_symbols"] == ["600000"]
    assert r["references"][0]["reference"] == "lhb_seat_details"
    # 参考源缺席(event 表没建) 不得被当成"完整"
    assert [c["reference"] for c in r["references"]] == ["lhb_seat_details"]


def test_读库定性_只读不写(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001", "600000"])

    class _ReadOnly:
        def connect(self):
            return eng.connect()

        def begin(self):
            raise AssertionError("完整性定性不得写库")

    import src.web.database as database

    monkeypatch.setattr(database, "engine", _ReadOnly())
    r = q.assess_date("20261019")
    assert r["status"] == "partial"


def test_空库定性返回absent而不抛(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    r = q.assess_date("20261019")
    assert r["status"] == "absent" and r["degraded"] is True
    assert r["references"] == []


def test_表缺失或不可读_如实标absent且说明读库失败(monkeypatch):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _use_engine(monkeypatch, eng)      # 不建表 → SELECT 抛错
    r = q.assess_date("20261019")
    assert r["status"] == "absent" and r["degraded"] is True
    assert "读库失败" in r["note"]
    assert any(e.startswith("db_error=") for e in r["evidence"])


# ─────────────────── ⑥ API 兼容: 只增字段, 不改旧键 ───────────────────

def test_按日读库响应带完整性且旧键不变(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00"), ("000002", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001", "000002", "600000"])

    out = mdm._dragon_tiger_from_db("20261019", "CN")
    assert out is not None
    # 旧契约原样
    assert out["trade_date"] == "20261019" and out["market"] == "CN"
    assert out["count"] == 2 and len(out["items"]) == 2
    assert out["source"] == "db" and out["data_origin"] == "dragon_tiger_daily"
    assert out["pending"] is False
    assert {i["symbol"] for i in out["items"]} == {"000001", "000002"}
    # 新增: 残缺不再冒充完整
    assert out["degraded"] is True
    assert out["quality"]["status"] == "partial"
    assert out["quality"]["missing_symbols"] == ["600000"]
    assert out["quality_note"]


def test_按日端点读残库_不回源且带定性(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001", "600000"])
    store: dict = {}
    monkeypatch.setattr(mdm.biz_cache, "set_json", lambda k, v, ttl=None: store.__setitem__(k, v))
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))

    class _MD:
        def dragon_tiger(self, **k):
            raise AssertionError("已落库日默认不回源(回源修复需单独授权)")

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    out = asyncio.run(mdm.dragon_tiger_proxy("20261019", market="CN", wait=0))
    assert out["source"] == "db" and out["count"] == 1
    assert out["quality"]["status"] == "partial" and out["degraded"] is True


def test_旧缓存载荷补定性后才返回(monkeypatch):
    """上线前的 biz_cache 载荷没有 quality 键; 不能因此把残缺照旧当完整。"""
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001", "600000"])
    legacy = {
        "trade_date": "20261019", "market": "CN", "count": 1,
        "items": [{"symbol": "000001"}], "pending": False,
        "source": "db", "data_origin": "dragon_tiger_daily",
    }
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: dict(legacy))
    monkeypatch.setattr(mdm.biz_cache, "set_json", lambda k, v, ttl=None: None)

    out = asyncio.run(mdm.dragon_tiger_proxy("20261019", market="CN", wait=0))
    assert out["count"] == 1 and out["source"] == "db"      # 旧键原样
    assert out["quality"]["status"] == "partial"            # 补齐定性
    assert out["degraded"] is True


def test_定性本身失效时显式标degraded(monkeypatch):
    """定性不可用 ≠ 完整: 字段缺席不能让调用方读成"没问题"。"""
    payload = {"trade_date": "20261019", "count": 1, "items": [], "pending": False,
               "source": "db", "data_origin": "dragon_tiger_daily"}
    monkeypatch.setattr(mdm, "_lhb_snapshot_quality", lambda *a, **k: {})
    out = mdm._with_lhb_quality(payload, "20261019", "CN")
    assert out["count"] == 1 and out["source"] == "db"       # 旧键不动
    assert out["degraded"] is True
    assert "定性不可用" in out["quality_note"]


def test_quality端点_只读返回定性(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001", "600000"])

    out = asyncio.run(mdm.dragon_tiger_quality("20261019", market="CN"))
    assert out["status"] == "partial"
    assert out["trade_date"] == "20261019"
    assert out["missing_total"] == 1


def test_quality端点已注册():
    from src.web.api.market_data import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/dragon-tiger/{trade_date}/quality" in paths
    assert "/dragon-tiger/{trade_date}" in paths          # 旧路由不动
    assert "/dragon-tiger/range/status" in paths


def test_range输出带每日完整性定性(monkeypatch):
    """`_compute_lhb_range` 里是同一个"已落库不回源"短路, 也要带定性。"""
    from datetime import date as _date

    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    d = "20261016"  # 周五
    _add_daily(eng, [("000001", "2026-10-16 08:48:00")], trade_date=d)   # 该日残: 缺 600000
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lhb_seat_details (row_uid, trade_date, side, symbol, "
                "operate_dept_code, operate_dept_name) "
                "VALUES ('r1', '20261016', 'buy', '600000', '10031935', '某营业部')"
            )
        )
    monkeypatch.setattr(
        "datetime.date", type("MockDate", (), {"today": staticmethod(lambda: _date(2026, 10, 16))})
    )

    class _MD:
        def dragon_tiger(self, *, date=None, market="CN"):
            return []

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    out = mdm._compute_lhb_range("CN", 2)
    assert out["by_date"][d][0]["symbol"] == "000001"          # 旧键不变
    qual = out["quality_by_date"][d]
    assert qual["status"] == "partial" and qual["missing_symbols"] == ["600000"]
    assert qual["early_freeze"] is True
