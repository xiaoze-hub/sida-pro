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
     参考源必须先**同市场**过滤/核验, 跨市场代码不得当缺口(避免误报);
  ③ 首写早于**生产经验下界** 17:16 → `early_freeze=True`, 只作**风险提示**
     (17:16 非官方发布时间), 不改变 status;
  ④ 只有"权威完整清单已覆盖"才允许 `status=complete`; `unverified`(无证据) 与
     `partial`(有证据) 用 `verification`/`severity`/`label` 显著区分, `degraded`
     只表示已证不可当完整使用;
  ⑤ 定性过程**只读**, 不写库、不回源(写入需另行授权);
  ⑥ 现有读库响应键(`count/items/source/data_origin/pending`)保持兼容, 只增不改;
     7. archive/events(`dragon_tiger_events`)有独立定性: 永不声明完整
        (`completeness_claimed=False`), 差异只作交叉提示。

全部禁真实网络(vendor 打桩, 库用内存 SQLite)。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
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


def _add_daily(eng, rows, trade_date: str = "20261019", market: str = "CN"):
    """rows: [(symbol, updated_at)] → 直插 dragon_tiger_daily(绕过 upsert 便于控首写时刻)。"""
    with eng.begin() as conn:
        for sym, ts in rows:
            conn.execute(
                text(
                    "INSERT INTO dragon_tiger_daily "
                    "(trade_date, symbol, market, name, reason, close, net_buy, source, updated_at) "
                    "VALUES (:d, :s, :mkt, '某票', '日涨幅偏离值达7%', 10.0, 1.0, "
                    "'em_ftshare', :ts)"
                ),
                {"d": trade_date, "s": sym, "mkt": market, "ts": ts},
            )


def _add_events(eng, rows, trade_date: str = "20261019"):
    """rows: [(symbol, market, created_at)] → 直插 dragon_tiger_events(可造跨市场/早写)。"""
    with eng.begin() as conn:
        for i, (sym, mkt, ts) in enumerate(rows):
            conn.execute(
                text(
                    "INSERT INTO dragon_tiger_events "
                    "(trade_date, symbol, market, name, reason, close, change_pct, source, created_at) "
                    "VALUES (:d, :s, :mkt, '某票', :reason, 10.0, 10.0, 'eastmoney', :ts)"
                ),
                {"d": trade_date, "s": sym, "mkt": mkt, "ts": ts, "reason": f"原因{i}"},
            )


def _add_seats(eng, symbols, trade_date: str = "20261019"):
    from uuid import uuid4

    with eng.begin() as conn:
        for sym in symbols:
            conn.execute(
                text(
                    "INSERT INTO lhb_seat_details "
                    "(row_uid, trade_date, symbol, name, reason, side, operate_dept_code, "
                    "operate_dept_name, buy_amt, "
                    "sell_amt, net_amt) "
                    "VALUES (:uid, :d, :s, '某票', '日涨幅偏离值达7%', 'buy', '10031935', '某营业部', "
                    "1000.0, 0.0, 1000.0)"
                ),
                {"uid": f"u-{uuid4().hex}", "d": trade_date, "s": sym},
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
    assert r["complete"] is False
    # 未核对 ≠ 已证残缺: 不许用 degraded=True 糊在一起
    assert r["degraded"] is False and r["severity"] == "caution"
    assert r["verification"] == "unverified"
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
    assert r["complete"] is False and r["degraded"] is False
    assert any("风险" in e for e in r["evidence"])


def test_首写晚于发布窗口_无早冻标记():
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001"],
        first_write_at=datetime(2026, 10, 19, 17, 31),
    )
    assert r["early_freeze"] is False and r["degraded"] is False


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
    assert r["status"] == "unverified" and r["degraded"] is False
    assert r["expected"]["authoritative"] is False


# ─────────────────── ⑤ 读库侧: 只读定性 ───────────────────

def test_读库定性_残缺快照带缺口与早冻证据(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    # SQLite CURRENT_TIMESTAMP 是 UTC naive: 08:48Z == 北京 16:48(早于 17:16)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00"), ("000002", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001", "000002", "600000"])

    r = q.assess_date("20261019", engine=eng)
    assert r["status"] == "partial" and r["degraded"] is True
    assert r["actual_symbols"] == 2
    assert r["first_write_at"] == "2026-10-19 16:48:00"   # UTC → 北京
    assert r["early_freeze"] is True
    assert r["missing_symbols"] == ["600000"]
    assert r["references"][0]["reference"] == "lhb_seat_details"
    # 参考源缺席(event 表没建) 不得被当成"完整"
    assert [c["reference"] for c in r["references"]] == ["lhb_seat_details"]


def test_读库定性_只读不写():
    eng = _mk_engine(with_seats=True)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001", "600000"])

    class _ReadOnly:
        def connect(self):
            return eng.connect()

        def begin(self):
            raise AssertionError("完整性定性不得写库")

    r = q.assess_date("20261019", engine=_ReadOnly())
    assert r["status"] == "partial"


def test_空库定性返回absent而不抛(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    r = q.assess_date("20261019", engine=eng)
    assert r["status"] == "absent" and r["degraded"] is True
    assert r["references"] == []


def test_表缺失或不可读_如实标absent且说明读库失败(monkeypatch):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _use_engine(monkeypatch, eng)      # 不建表 → SELECT 抛错
    r = q.assess_date("20261019", engine=eng)
    assert r["status"] == "absent" and r["degraded"] is True
    assert "读库失败" in r["note"]
    assert any(e.startswith("db_error=") for e in r["evidence"])


def test_入口到只读SQL_注入engine即真实读库且只发SELECT():
    """B4.1 回归: 定性不得自己 import src.web 找库 —— 注入哪个库就读哪个库。

    且从入口(`assess_date`)到 SQL 只许 SELECT: 读不到要如实标读库失败,
    不能悄悄变出一份"无快照"。
    """
    eng = _mk_engine(with_seats=True)
    _add_daily(eng, [("000001", "2026-10-19 09:31:00")])
    _add_seats(eng, ["000001", "600000"])
    seen = _capture_sql(eng)

    r = q.assess_date("20261019", "CN", engine=eng)

    assert r["actual_symbols"] == 1 and r["sources"] == {"em_ftshare": 1}
    assert r["status"] == "partial" and r["missing_symbols"] == ["600000"]
    assert not any(e.startswith("db_error=") for e in r["evidence"])   # 无读库异常
    assert len(seen) >= 2, "快照与参考源各应读一次, 实际没发 SQL 说明没读注入库"
    verbs = {s.lstrip().split(None, 1)[0].upper() for s in seen}
    assert verbs == {"SELECT"}, f"定性是只读路径, 实际发出: {sorted(verbs)}"


def test_注入哪个engine就读哪个_不回落全局(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _add_daily(eng, [("000001", "2026-10-19 09:31:00")])
    empty = _mk_engine()

    class _Boom:
        def connect(self):
            raise AssertionError("不得读全局 engine, 只能用注入的那个")

    import src.web.database as database

    monkeypatch.setattr(database, "engine", _Boom())

    r1 = q.assess_date("20261019", engine=eng)
    assert r1["actual_symbols"] == 1 and r1["status"] != "absent"
    r2 = q.assess_date("20261019", engine=empty)
    assert r2["actual_symbols"] == 0 and r2["status"] == "absent"


def test_未注入engine_显式报错而非静默无快照():
    with pytest.raises(TypeError):                        # 参数必填, 忘了传直接炸
        q.assess_date("20261019")
    with pytest.raises(ValueError, match="显式注入"):
        q.assess_date("20261019", engine=None)
    with pytest.raises(TypeError, match="connect"):
        q.assess_date("20261019", engine=object())


def test_API入口把同一engine注入定性(monkeypatch):
    """真实调用必须注入 engine, 且就是本次载荷读的那个库; 否则定性会静默无数据。"""
    import src.core.lhb_quality as lq
    import src.web.database as database

    sentinel = object()
    seen: dict = {}

    def _fake(trade_date, market="CN", *, engine=None):
        seen.update(trade_date=trade_date, market=market, engine=engine)
        return {"status": "unverified", "degraded": False}

    monkeypatch.setattr(database, "engine", sentinel)
    monkeypatch.setattr(lq, "assess_date", _fake)

    out = mdm._lhb_snapshot_quality("20261019", "CN")

    assert seen["engine"] is sentinel, "定性必须和载荷读同一个库"
    assert seen["trade_date"] == "20261019" and seen["market"] == "CN"
    assert out["status"] == "unverified"


def test_API入口到SQL_只读不写(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("000001", "2026-10-19 09:31:00")])
    _add_seats(eng, ["000001"])
    seen = _capture_sql(eng)

    out = mdm._dragon_tiger_from_db("20261019", "CN")

    assert out is not None and out["count"] == 1 and out["source"] == "db"
    assert out["quality_status"] == "unverified"                # 定性真的读到库了
    writes = [s for s in seen if s.lstrip().split(None, 1)[0].upper() != "SELECT"]
    assert not writes, f"读库响应路径不得写库: {writes}"


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


# ═══════════════ ⑦ 审查回修: max([])、跨市场、早冻风险、degraded 语义 ═══════════════

def test_非权威预期清单有缺口_不因max空而崩且判残缺():
    """非权威 expected 有缺口且无 references 时, note 生成不得对空序列取 max([])。"""
    r = q.assess_daily_completeness(
        "20261019",
        actual_symbols=["000001"],
        expected_symbols=["000001", "600000"],
        expected_source="eastmoney_preview",
        expected_authoritative=False,
    )
    assert r["status"] == "partial"
    assert r["degraded"] is True and r["severity"] == "incomplete"
    assert r["missing_symbols"] == ["600000"] and r["missing_total"] == 1
    assert "非官方" in r["note"] and "残缺" in r["note"]


def test_unverified与partial语义显著区分():
    ok_ref = {"lhb_seat_details": ["000001"]}
    bad_ref = {"lhb_seat_details": ["000001", "600000"]}
    u = q.assess_daily_completeness("20261019", actual_symbols=["000001"], references=ok_ref)
    p = q.assess_daily_completeness("20261019", actual_symbols=["000001"], references=bad_ref)

    assert u["status"] == "unverified" and p["status"] == "partial"
    assert u["complete"] is False and p["complete"] is False     # 两者都不能当完整
    # 但"没证据"与"有证据残缺"必须能区分: 严重度/核验态/降级布尔
    assert u["verification"] == "unverified" and p["verification"] == "disproven"
    assert u["severity"] == "caution" and p["severity"] == "incomplete"
    assert u["label"] != p["label"]
    assert u["degraded"] is False and p["degraded"] is True
    # 文案不得互相冒充: 未核对不能说"残缺", 已证残缺要说"残缺"
    assert "残缺" not in u["note"] and "未经官方清单核对" in u["note"]
    assert "残缺" in p["note"]


def test_早冻只作风险_不冒充官方发布时间():
    r = q.assess_daily_completeness(
        "20261019", actual_symbols=["000001"], first_write_at=datetime(2026, 10, 19, 16, 48)
    )
    assert r["early_freeze"] is True and r["early_freeze_risk"] is True
    assert r["status"] == "unverified" and r["severity"] == "caution"   # 时间不是残缺结论
    assert "风险" in r["early_freeze_note"]
    assert "非官方发布时间" in r["early_freeze_note"]
    assert "不构成残缺结论" in r["early_freeze_note"]
    assert "非官方发布时间" in r["publish_ready_basis"]
    assert any("风险" in w for w in r["warnings"])
    assert r["note"].startswith("早写风险")          # 突出到 note 开头, 不埋在 evidence
    # 早冻不得把 status 抬成 partial
    assert r["missing_total"] == 0


def test_跨市场参考源_不被当成本市场缺口(monkeypatch):
    eng = _mk_engine(with_events=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("000001", "2026-10-19 09:31:00"), ("000002", "2026-10-19 09:31:00")])
    _add_events(
        eng,
        [("000001", "CN", "2026-10-19 09:31:00"), ("000002", "CN", "2026-10-19 09:31:00"),
         ("00700", "HK", "2026-10-19 09:31:00"), ("AAPL", "US", "2026-10-19 09:31:00")],
    )

    r = q.assess_date("20261019", "CN", engine=eng)
    assert r["status"] == "unverified"          # 同市场无缺口 → 未核对, 而非 partial
    assert r["missing_total"] == 0
    ref = r["references"][0]
    assert ref["reference"] == "dragon_tiger_events"
    assert ref["market"] == "CN" and ref["market_verified"] is True


def test_无market列参考源_符号非本市场形态则跳过比较(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("000001", "2026-10-19 09:31:00")])
    _add_seats(eng, ["000001", "00700"])        # 5 位 HK 码混进无 market 列的席位表

    r = q.assess_date("20261019", "CN", engine=eng)
    assert r["status"] == "unverified" and r["missing_total"] == 0
    assert "lhb_seat_details" in r["references_skipped"]
    assert "无法核验" in r["references_skipped"]["lhb_seat_details"]


def test_非CN市场_无market列参考源不比较(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("00700", "2026-10-19 09:31:00")], market="HK")
    _add_seats(eng, ["00700"])

    r = q.assess_date("20261019", "HK", engine=eng)
    assert r["status"] == "unverified" and r["references"] == []
    assert "lhb_seat_details" in r["references_skipped"]


# ─────────────── ⑧ archive/events 工作区的独立定性(不冒充完整) ───────────────

def _archive_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    import src.web.database as webdb
    from src.web.api import market_archive

    eng = _mk_engine(with_events=True, with_seats=False)
    _add_daily(eng, [("000001", "2026-10-19 09:31:00")])
    _add_events(
        eng,
        [("000001", "CN", "2026-10-19 08:50:00"), ("600000", "CN", "2026-10-19 08:50:00")],
    )
    monkeypatch.setattr(webdb, "SessionLocal", sessionmaker(bind=eng))
    app = FastAPI()
    app.include_router(market_archive.router, prefix="/api/archive")
    return TestClient(app), eng


def test_archive_events定性独立且不声明完整(monkeypatch):
    client, _ = _archive_client(monkeypatch)
    data = client.get("/api/archive/dragon-tiger", params={"date": "20261019"}).json()

    assert data["count"] == 2 and len(data["items"]) == 2      # 旧键不变
    assert "完整榜单" not in data["note"]                       # 不再冒充完整
    qual = data["quality"]
    assert qual["table"] == "dragon_tiger_events"
    assert qual["status"] == "unverified"
    assert qual["complete"] is False and qual["completeness_claimed"] is False
    assert qual["verified_against_official"] is False
    assert "不声明完整" in qual["note"]
    # 早写只作风险提示, 不下残缺结论
    assert qual["early_freeze"] is True and qual["status"] == "unverified"
    assert any("风险" in w for w in qual["warnings"])
    # 与 daily 的差异只作交叉提示, 不判任何一方完整
    assert qual["cross_check"]["only_in_events"] == 1
    assert qual["cross_check"]["only_in_daily"] == 0
    assert "不能据此判定" in qual["cross_check"]["note"]
    assert qual["note"].startswith("早写风险")


def test_archive_events空行如实absent(monkeypatch):
    client, _ = _archive_client(monkeypatch)
    data = client.get("/api/archive/dragon-tiger", params={"date": "20261020"}).json()
    assert data["count"] == 0
    qual = data["quality"]
    assert qual["status"] == "absent" and qual["degraded"] is True
    assert qual["complete"] is False and qual["completeness_claimed"] is False
    assert "无覆盖不等于无上榜" in qual["note"]


def test_API层unverified与partial字段可区分(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("000001", "2026-10-19 08:48:00")])
    _add_seats(eng, ["000001"])                       # 同股数同集合 → unverified

    out = mdm._dragon_tiger_from_db("20261019", "CN")
    assert out["count"] == 1 and out["source"] == "db"  # 旧键不动
    assert out["quality_status"] == "unverified"
    assert out["quality_severity"] == "caution"
    assert out["quality_label"] == "未经核对"
    assert out["degraded"] is False and out["verified_complete"] is False
    assert out["quality_note"] and "不得作为完整榜单展示" not in out["quality_note"]

    _add_seats(eng, ["600000"])                       # 参考源多出一只 → partial
    out2 = mdm._dragon_tiger_from_db("20261019", "CN")
    assert out2["quality_status"] == "partial"
    assert out2["quality_severity"] == "incomplete"
    assert out2["quality_label"] == "已证残缺"
    assert out2["degraded"] is True and out2["verified_complete"] is False
    assert "残缺" in out2["quality_note"]


def test_archive_events跨市场行不混进CN定性(monkeypatch):
    client, eng = _archive_client(monkeypatch)
    _add_events(eng, [("00700", "HK", "2026-10-19 08:50:00")])

    data = client.get("/api/archive/dragon-tiger", params={"date": "20261019"}).json()
    assert data["count"] == 3                              # items 未过滤: 旧行为不变
    qual = data["quality"]
    assert qual["market"] == "CN" and qual["actual_symbols"] == 2   # 只定性 CN
    assert qual["rows_total"] == 3 and qual["rows_in_market"] == 2
    assert "2/3" in qual["note"]
    assert qual["status"] == "unverified" and qual["complete"] is False


# ═══════ ⑨ 单股过滤不得改变全日质量(archive: 全日范围定性 + 本股视角) ═══════

def test_纯函数_本股无记录_不等于全日无覆盖():
    s = q.assess_symbol_scope("600000", day_symbols=["000001", "000002"], day_status="unverified")
    assert s["recorded"] is False and s["day_symbols"] == 2
    assert s["day_status"] == "unverified"
    assert "本股" in s["note"] and "无记录" in s["note"]
    assert "不是全日无覆盖" in s["note"]
    assert "不等于未上榜" in s["note"]        # events 非官方清单, 只能说"该表无行"


def test_纯函数_全日无覆盖时本股不可判定():
    s = q.assess_symbol_scope("600000", day_symbols=[], day_status="absent")
    assert s["recorded"] is None             # 不可判定 ≠ 本股无记录
    assert "全日无覆盖" in s["note"] and "无法判定" in s["note"]


def test_纯函数_本股有记录_去后缀归一():
    s = q.assess_symbol_scope("600000.SH", day_symbols=["600000"], day_status="unverified")
    assert s["recorded"] is True and s["symbol"] == "600000"


def _capture_sql(eng) -> list[str]:
    from sqlalchemy import event

    seen: list[str] = []

    def _cb(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(eng, "before_cursor_execute", _cb)
    return seen


def test_archive单股无记录_质量仍按全日范围而非无快照(monkeypatch):
    """date+symbol 过滤后明细为空, 但该日全榜有行 → 不得把全日质量误判为 absent。"""
    client, _ = _archive_client(monkeypatch)
    data = client.get(
        "/api/archive/dragon-tiger", params={"date": "20261019", "symbol": "000002"}
    ).json()

    assert data["count"] == 0 and data["items"] == []       # 旧键: 明细仍按 symbol 过滤
    qual = data["quality"]
    assert qual["status"] == "unverified"                    # 不再是 absent
    assert qual["degraded"] is False and qual["label"] != "无快照"
    assert qual["actual_symbols"] == 2                       # 全日实际市场范围
    assert qual["scope"] == "day_market"
    assert qual["subject"]["symbol"] == "000002"
    assert qual["subject"]["recorded"] is False and qual["subject"]["day_symbols"] == 2
    assert "不是全日无覆盖" in qual["subject"]["note"]
    assert "单股视角" in qual["note"] and "本股(000002)" in qual["note"]
    # 明细 note 要区分"本股无记录"与"全日无覆盖"
    assert data["note"].startswith("龙虎榜明细")
    assert "不声明完整" in data["note"]                       # 旧语义保留
    assert "不代表全日无覆盖" in data["note"]


def test_archive单股有记录_明细一支但质量仍是全日(monkeypatch):
    client, _ = _archive_client(monkeypatch)
    data = client.get(
        "/api/archive/dragon-tiger", params={"date": "20261019", "symbol": "600000"}
    ).json()
    assert data["count"] == 1
    qual = data["quality"]
    assert qual["actual_symbols"] == 2 and qual["status"] == "unverified"
    assert qual["subject"]["recorded"] is True


def test_archive全日无覆盖_单股查询如实区分且不臆断未上榜(monkeypatch):
    client, _ = _archive_client(monkeypatch)
    data = client.get(
        "/api/archive/dragon-tiger", params={"date": "20261021", "symbol": "600000"}
    ).json()
    qual = data["quality"]
    assert qual["status"] == "absent" and qual["degraded"] is True
    assert qual["subject"]["recorded"] is None               # 不可判定, 不谎称无记录
    assert "全日无覆盖" in qual["subject"]["note"]
    assert "无法判定" in qual["subject"]["note"]
    assert "无覆盖不等于无上榜" in qual["note"]


def test_archive单股查询只读且只补一次轻量全日范围(monkeypatch):
    client, eng = _archive_client(monkeypatch)
    seen = _capture_sql(eng)
    data = client.get(
        "/api/archive/dragon-tiger", params={"date": "20261019", "symbol": "000002"}
    ).json()
    assert data["quality"]["actual_symbols"] == 2
    assert all(s.lstrip().upper().startswith("SELECT") for s in seen)   # 只读, 无写/回源
    day_sql = [s for s in seen if "FROM dragon_tiger_events" in s]
    assert len(day_sql) == 2                       # 明细 1 次 + 全日范围 1 次
    scope = [s for s in day_sql if "SELECT symbol, market, created_at" in s]
    assert len(scope) == 1 and "SELECT *" not in scope[0]   # 不全表重扫


def test_archive按日查询不额外补查询(monkeypatch):
    client, eng = _archive_client(monkeypatch)
    seen = _capture_sql(eng)
    client.get("/api/archive/dragon-tiger", params={"date": "20261019"})
    assert len([s for s in seen if "FROM dragon_tiger_events" in s]) == 1


def test_全日范围读取失败_不拿单股结果冒充全日(monkeypatch):
    client, _ = _archive_client(monkeypatch)
    from src.web.api import market_archive as ma

    monkeypatch.setattr(ma, "_day_event_rows", lambda d, mkt=None: None)
    data = client.get(
        "/api/archive/dragon-tiger", params={"date": "20261019", "symbol": "000002"}
    ).json()
    qual = data["quality"]
    assert qual["status"] == "unknown" and qual["status"] != "absent"
    assert qual["complete"] is False and qual["completeness_claimed"] is False
    assert qual["subject"]["recorded"] is None
    assert "不可判定" in qual["note"]


def test_全日范围查询走trade_date索引而非全表扫(monkeypatch):
    """单股查询只补一次轻量全日范围, 且按 trade_date 命中索引(不整表扫)。"""
    client, eng = _archive_client(monkeypatch)
    seen = _capture_sql(eng)
    client.get("/api/archive/dragon-tiger", params={"date": "20261019", "symbol": "000002"})
    stmt = next(
        s for s in seen
        if "FROM dragon_tiger_events" in s and "SELECT symbol, market, created_at" in s
    )
    with eng.connect() as c:
        # 监听器拿到的是编译后 SQL(占位符 ?), 走 driver 层按位置传参
        plan = " | ".join(
            str(tuple(r))
            for r in c.exec_driver_sql("EXPLAIN QUERY PLAN " + stmt, ("20261019",))
        )
    assert "SCAN dragon_tiger_events" not in plan
    assert "trade_date=?" in plan


def test_archive单股查询带market_筛选_全日范围同市场(monkeypatch):
    client, eng = _archive_client(monkeypatch)
    _add_events(eng, [("00700", "HK", "2026-10-19 08:50:00")])
    data = client.get(
        "/api/archive/dragon-tiger",
        params={"date": "20261019", "symbol": "000001", "market": "CN"},
    ).json()
    qual = data["quality"]
    assert qual["actual_symbols"] == 2 and "rows_total" not in qual   # 只取 CN 全日范围
    assert qual["subject"]["recorded"] is True and qual["subject"]["day_symbols"] == 2


def test_archive仅按symbol查询_不加全日定性且旧行为不变(monkeypatch):
    client, _ = _archive_client(monkeypatch)
    data = client.get("/api/archive/dragon-tiger", params={"symbol": "600000"}).json()
    assert data["count"] == 1 and "quality" not in data
    assert "在该区间无记录" in data["note"]

    empty = client.get("/api/archive/dragon-tiger", params={"symbol": "000002"}).json()
    assert empty["count"] == 0 and "quality" not in empty
    assert "不代表全日无覆盖" in empty["note"]
