"""真实持仓防守位全覆盖（src/harness/portfolio_risk.py）定向门禁（2026-11-21 · B4.1 首批 B）。

守的是**结论的可信度**，不是措辞：

1. 分母必须是「权限校验后的实际持仓」，不是自选、不是 8 只上限、不是别人的仓；
2. 三值结论（已跌破 / 未跌破 / 无法核验）必须由数据确定性产出：
   缺位、缺价、过期、时点证不出、取数失败，一律落「无法核验」，**绝不落安全**；
3. 覆盖不完整时，文本里不允许出现「安全」这种整体断言；
4. 阈值口径与 `evaluate_defense_breach` 同源：现价 == 防守价即视为已跌破；
5. 同券异账户逐笔独立判定（合并的只有取数，不是结论）；
6. 识别收窄：单股问价 / 持仓诊断 / 写防线 / 全市场扫描都必须返回 None。

本文件只跑定向路径，不含全量回归（任务硬约束：禁全量 pytest）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Account, Base, PaperTradingPosition, Position, Stock, User
from src.harness.portfolio_risk import (
    REASON_NO_LINE,
    REASON_QUOTE_ERROR,
    REASON_STALE_CLOSE,
    REASON_STALE_INTRADAY,
    VERDICT_BREACHED,
    VERDICT_SAFE,
    VERDICT_UNKNOWN,
    answer,
    evaluate_holdings,
    load_holdings,
    render,
)
from src.harness.portfolio_risk import HoldingRow

CST = ZoneInfo("Asia/Shanghai")

# 2026-11-20 是周五（交易日），2026-11-21 是周六（休市）。
TRADING_NOW = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
CLOSED_NOW = datetime(2026, 11, 21, 13, 14, tzinfo=CST)  # 周六午后
LAST_TRADING_DAY = "2026-11-20"


# ------------------------------------------------------------------ 夹具


@pytest.fixture(autouse=True)
def portfolio_clock(monkeypatch):
    """直查与固定行情共享时间线；仍走真实时段、时效和三态判定。"""
    from src.harness import portfolio_risk

    clock = [CLOSED_NOW]

    def evaluate_on_fixture_clock(rows, quotes, *, now=None, **kwargs):
        return evaluate_holdings(rows, quotes, now=clock[0] if now is None else now, **kwargs)

    monkeypatch.setattr(portfolio_risk, "evaluate_holdings", evaluate_on_fixture_clock)
    return clock


@pytest.fixture()
def db(tmp_path):
    """独立临时库（不与 conftest 的共享库互污染）。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'portfolio_risk.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, name: str) -> User:
    user = User(
        id=f"user-{name}",
        username=name,
        password_hash="x",
        role="member",
        is_active=True,
        token_version=1,
    )
    db.add(user)
    db.flush()
    return user


def _account(db, name: str, user_id: str | None, *, enabled: bool = True) -> Account:
    acc = Account(name=name, available_funds=0.0, user_id=user_id, enabled=enabled)
    db.add(acc)
    db.flush()
    return acc


def _stock(db, symbol: str, name: str = "", market: str = "CN") -> Stock:
    st = Stock(symbol=symbol, name=name or f"票{symbol}", market=market)
    db.add(st)
    db.flush()
    return st


def _position(db, account, stock, *, user_id, defense_price, cost=10.0, qty=100) -> Position:
    pos = Position(
        account_id=account.id,
        stock_id=stock.id,
        cost_price=cost,
        quantity=qty,
        user_id=user_id,
        defense_price=defense_price,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def _quote(symbol: str, price, *, quote_date=LAST_TRADING_DAY, clock: str | None = "15:00:00",
           status="ok", market_tz="+08:00") -> dict:
    return {
        "symbol": symbol,
        "name": f"票{symbol}",
        "market": "CN",
        "current_price": price,
        "quote_date": quote_date,
        "quote_time": f"{quote_date}T{clock}{market_tz}" if clock else None,
        "quote_time_kind": "snapshot_refresh",
        "quote_date_basis": "daily_bar_close",
        "status": status,
    }


def _patch_quotes(monkeypatch, mapping: dict[str, dict], *, fail_markets=()):
    """替换行情批量取数：按代码返回，记录调用次数（证明合并取数、不是逐笔拉）。"""
    calls: list[tuple[tuple[str, ...], str]] = []

    def _fake(symbols, market):
        calls.append((tuple(symbols), market))
        if market in fail_markets:
            raise RuntimeError("vendor down")
        return [mapping[s] for s in symbols if s in mapping]

    monkeypatch.setattr("src.core.marketdata_client.md_quote_rows", _fake)
    return calls


def _rows(*specs) -> list[HoldingRow]:
    out = []
    for i, (sym, market, dp, cost) in enumerate(specs, start=1):
        out.append(
            HoldingRow(
                position_id=i,
                account_id=1,
                account_name="主账户",
                symbol=sym,
                market=market,
                name=f"票{sym}",
                quantity=100.0,
                cost_price=cost,
                defense_price=dp,
            )
        )
    return out


# ------------------------------------------------------------------ ① >8 笔全覆盖


def test_more_than_eight_positions_are_all_judged(db, monkeypatch, portfolio_clock):
    """12 笔持仓全部进分母 —— 发布版按自选展开的上限是 8，这里必须不被它卡住。"""
    portfolio_clock[0] = TRADING_NOW
    user = _user(db, "heavy")
    acc = _account(db, "主账户", user.id)
    quotes = {}
    defend_lines = []
    for i in range(12):
        sym = f"60000{i}" if i < 10 else f"6000{i}"
        st = _stock(db, sym)
        # 偶数笔跌破、奇数笔安全
        dp = 10.0
        price = 9.0 if i % 2 == 0 else 11.0
        _position(db, acc, st, user_id=user.id, defense_price=dp)
        quotes[sym] = _quote(sym, price, clock="09:58:00")  # 盘中 10:00 前 2 分钟
        defend_lines.append((sym, dp))

    _patch_quotes(monkeypatch, quotes)
    text = asyncio.run(answer(db, "我的持仓防守位破了吗？", None, user))

    assert text is not None
    rows, _un, _z = load_holdings(db, user.id)
    assert len(rows) == 12  # 不是 8

    verdicts, cov = evaluate_holdings(rows, {(r.market, r.symbol): quotes[r.symbol] for r in rows},
                                      now=TRADING_NOW)
    assert cov.total == 12 and cov.verified == 12 and cov.complete is True
    assert cov.breached == 6 and cov.safe == 6
    assert "覆盖 12/12" in text
    # 每笔均为盘中两分钟前的快照，不能降级成休市口径。
    assert sum(1 for v in verdicts if v.status == VERDICT_UNKNOWN) == 0


def test_symbol_expansion_cap_is_not_used_by_this_path():
    """口径声明：本路径不吃 pipeline 的 8 只上限（回归时若有人把它接上去会先红）。"""
    from src.harness import pipeline_router

    assert pipeline_router.MAX_WATCHLIST == 8  # 发布版口径没变
    from src.harness import portfolio_risk

    assert portfolio_risk.MAX_DETAIL_ROWS > 8


def test_quotes_are_fetched_in_one_batch_per_market(db, monkeypatch):
    """行情按 市场+代码 合并取一次；判定仍逐笔（取数合并 ≠ 结论合并）。"""
    user = _user(db, "batch")
    acc = _account(db, "主账户", user.id)
    quotes = {}
    for sym in ("600519", "000001", "600036"):
        st = _stock(db, sym)
        _position(db, acc, st, user_id=user.id, defense_price=10.0)
        quotes[sym] = _quote(sym, 10.5)
    calls = _patch_quotes(monkeypatch, quotes)

    rows, _u, _z = load_holdings(db, user.id)
    from src.harness.portfolio_risk import _fetch_quotes

    got, failed = asyncio.run(_fetch_quotes(rows))
    assert len(calls) == 1 and calls[0][1] == "CN"
    assert set(calls[0][0]) == {"600519", "000001", "600036"}
    assert set(got) == {("CN", "600519"), ("CN", "000001"), ("CN", "600036")}
    assert not failed


# ------------------------------------------------------------------ ② 同券异账户 + 阈值


def test_same_symbol_in_two_accounts_is_judged_independently(db, monkeypatch):
    """同一只票两个账户、两条不同防守价 → 一个已跌破、一个未跌破。"""
    user = _user(db, "multi")
    acc_a = _account(db, "券商A", user.id)
    acc_b = _account(db, "券商B", user.id)
    st = _stock(db, "600519")
    _position(db, acc_a, st, user_id=user.id, defense_price=92.0)   # 现价 91.5 → 破
    _position(db, acc_b, st, user_id=user.id, defense_price=90.0)   # 现价 91.5 → 未破
    _patch_quotes(monkeypatch, {"600519": _quote("600519", 91.5, quote_date="2026-11-20")})

    text = asyncio.run(answer(db, "我的持仓有没有破防守位", None, user))
    assert text is not None
    assert "1 笔**已跌破**防守位，1 笔未跌破" in text

    rows, _u, _z = load_holdings(db, user.id)
    assert {r.account_id for r in rows} == {acc_a.id, acc_b.id}
    quotes = {("CN", "600519"): _quote("600519", 91.5, quote_date="2026-11-20")}
    verdicts, cov = evaluate_holdings(rows, quotes, now=CLOSED_NOW)
    by_defense = {v.row.defense_price: v for v in verdicts}
    assert by_defense[92.0].status == VERDICT_BREACHED
    assert by_defense[90.0].status == VERDICT_SAFE
    assert cov.breached == 1 and cov.safe == 1


def test_exact_equality_counts_as_breached():
    """现价 == 防守价即已跌破（与 evaluate_defense_breach 的 inclusive 口径同源）。"""
    rows = _rows(("600519", "CN", 10.0, 10.0))
    quotes = {("CN", "600519"): _quote("600519", 10.0)}
    verdicts, cov = evaluate_holdings(rows, quotes, now=CLOSED_NOW)
    assert verdicts[0].status == VERDICT_BREACHED
    assert verdicts[0].breach_pct == pytest.approx(0.0)
    assert cov.breached == 1 and cov.verified == 1


def test_threshold_contract_matches_intraday_monitor():
    """同一组输入的 breach 判定必须与 intraday_monitor 完全一致（无第二套阈值）。"""
    from src.agents.intraday_monitor import evaluate_defense_breach

    for price, defense in ((9.99, 10.0), (10.0, 10.0), (10.01, 10.0), (9.5, 12.3)):
        rows = _rows(("600519", "CN", defense, 10.0))
        verdicts, _cov = evaluate_holdings(
            rows, {("CN", "600519"): _quote("600519", price)}, now=CLOSED_NOW
        )
        expected = evaluate_defense_breach(defense_price=defense, current_price=price)["breached"]
        assert (verdicts[0].status == VERDICT_BREACHED) is expected


# ------------------------------------------------------------------ ③ 无法核验的每一类


def test_missing_defense_line_is_unknown_never_safe():
    rows = _rows(("600519", "CN", None, 10.0))
    verdicts, cov = evaluate_holdings(
        rows, {("CN", "600519"): _quote("600519", 100.0)}, now=CLOSED_NOW
    )
    assert verdicts[0].status == VERDICT_UNKNOWN
    assert verdicts[0].reason == REASON_NO_LINE
    assert cov.safe == 0 and cov.verified == 0 and cov.complete is False
    # 现价仍要尽量给出（便于用户对照该在哪设防线），但结论不能是安全
    assert verdicts[0].price == pytest.approx(100.0)


def test_missing_quote_is_unknown_and_coverage_incomplete():
    rows = _rows(("600519", "CN", 10.0, 10.0))
    verdicts, cov = evaluate_holdings(rows, {}, now=CLOSED_NOW)
    assert verdicts[0].status == VERDICT_UNKNOWN
    assert cov.unknown_reasons  # 有原因
    assert cov.complete is False


@pytest.mark.parametrize("bad", [None, 0, 0.0, -1.0, "abc", float("nan"), float("inf")])
def test_invalid_price_is_unknown(bad):
    rows = _rows(("600519", "CN", 10.0, 10.0))
    quotes = {("CN", "600519"): {"symbol": "600519", "current_price": bad,
                                 "quote_date": LAST_TRADING_DAY, "status": "ok"}}
    verdicts, cov = evaluate_holdings(rows, quotes, now=CLOSED_NOW)
    assert verdicts[0].status == VERDICT_UNKNOWN
    assert cov.safe == 0


def test_stale_close_before_last_session_is_unknown():
    """休市时只接受最近一个开盘交易日；停牌多日的旧价不能拿来判安全。"""
    rows = _rows(("600519", "CN", 10.0, 10.0))
    quotes = {("CN", "600519"): _quote("600519", 12.0, quote_date="2026-11-13")}
    verdicts, cov = evaluate_holdings(rows, quotes, now=CLOSED_NOW)
    assert verdicts[0].status == VERDICT_UNKNOWN
    assert verdicts[0].reason == REASON_STALE_CLOSE
    assert cov.safe == 0


def test_previous_session_close_is_accepted_when_market_closed():
    """周六用周五收盘价判定是合法的（这是当日已知的最后一个价）。"""
    rows = _rows(("600519", "CN", 10.0, 10.0))
    quotes = {("CN", "600519"): _quote("600519", 12.0, quote_date=LAST_TRADING_DAY)}
    verdicts, cov = evaluate_holdings(rows, quotes, now=CLOSED_NOW)
    assert verdicts[0].status == VERDICT_SAFE
    assert verdicts[0].basis == "close"
    assert cov.complete is True


def test_intraday_requires_today_and_fresh_stamp():
    """盘中：非当日 → 过期；当日但快照太旧 → 过期；新鲜 → 可判。"""
    fresh = TRADING_NOW - timedelta(seconds=30)
    cases = [
        (_quote("600519", 12.0, quote_date="2026-11-19"), REASON_STALE_INTRADAY),
        (_quote("600519", 12.0, quote_date="2026-11-20", clock="09:40:00"), REASON_STALE_INTRADAY),
        (_quote("600519", 12.0, quote_date="2026-11-20",
                clock=fresh.strftime("%H:%M:%S")), None),
    ]
    for quote, expected_reason in cases:
        rows = _rows(("600519", "CN", 10.0, 10.0))
        verdicts, _cov = evaluate_holdings(
            rows, {("CN", "600519"): quote}, now=TRADING_NOW
        )
        if expected_reason is None:
            assert verdicts[0].status == VERDICT_SAFE
            assert verdicts[0].basis == "intraday"
        else:
            assert verdicts[0].status == VERDICT_UNKNOWN
            assert verdicts[0].reason == expected_reason


def test_intraday_without_time_stamp_is_unknown():
    """日期只能证到「哪一天」，盘中还需要「是不是刚刚」—— 缺时刻就不判。"""
    rows = _rows(("600519", "CN", 10.0, 10.0))
    quotes = {("CN", "600519"): _quote("600519", 12.0, quote_date="2026-11-20", clock=None)}
    verdicts, _cov = evaluate_holdings(rows, quotes, now=TRADING_NOW)
    assert verdicts[0].status == VERDICT_UNKNOWN
    assert verdicts[0].reason == "unknown_quote_time"


def test_naive_time_stamp_is_read_as_market_local():
    """源报文没带时区时按市场时区理解（与 vendor 同口径），而不是直接判未知。"""
    rows = _rows(("600519", "CN", 10.0, 10.0))
    quotes = {("CN", "600519"): {
        "symbol": "600519", "current_price": 12.0,
        "quote_date": "2026-11-20", "quote_time": "2026-11-20T09:58:00",
    }}
    verdicts, _cov = evaluate_holdings(rows, quotes, now=TRADING_NOW)
    assert verdicts[0].status == VERDICT_SAFE
    assert verdicts[0].basis == "intraday"


def test_conflicting_time_and_date_are_rejected():
    """时刻与日期自相矛盾（上游串源）→ 两个证据都不用。"""
    rows = _rows(("600519", "CN", 10.0, 10.0))
    quotes = {("CN", "600519"): {
        "symbol": "600519", "current_price": 12.0,
        "quote_date": "2026-11-20", "quote_time": "2026-11-19T15:00:00+08:00",
    }}
    verdicts, _cov = evaluate_holdings(rows, quotes, now=CLOSED_NOW)
    assert verdicts[0].status == VERDICT_UNKNOWN
    assert verdicts[0].reason == "unknown_quote_time"


def test_no_time_evidence_at_all_is_unknown():
    rows = _rows(("600519", "CN", 10.0, 10.0))
    quotes = {("CN", "600519"): {"symbol": "600519", "current_price": 12.0}}
    verdicts, _cov = evaluate_holdings(rows, quotes, now=CLOSED_NOW)
    assert verdicts[0].status == VERDICT_UNKNOWN


def test_quote_source_failure_is_unknown_not_safe():
    rows = _rows(("600519", "CN", 10.0, 10.0), ("000001", "CN", 10.0, 10.0))
    verdicts, cov = evaluate_holdings(rows, {}, failed_markets={"CN"}, now=CLOSED_NOW)
    assert all(v.status == VERDICT_UNKNOWN for v in verdicts)
    assert all(v.reason == REASON_QUOTE_ERROR for v in verdicts)
    assert cov.safe == 0 and cov.complete is False


def test_unknown_market_is_unknown():
    rows = _rows(("600519", "XX", 10.0, 10.0))
    verdicts, _cov = evaluate_holdings(rows, {}, now=CLOSED_NOW)
    assert verdicts[0].status == VERDICT_UNKNOWN


# ------------------------------------------------------------------ ④ 文本不许许诺安全


def test_incomplete_coverage_text_never_claims_safety():
    """覆盖 1/2 时, 文本里不许出现「全部核验完毕/安全」这类整体断言。"""
    verdicts, cov = evaluate_holdings(
        [
            HoldingRow(1, 1, "主账户", "600519", "CN", "票", 100.0, 10.0, 10.0),
            HoldingRow(2, 1, "主账户", "000001", "CN", "票", 100.0, 10.0, None),
        ],
        {("CN", "600519"): _quote("600519", 12.0)},
        now=CLOSED_NOW,
    )
    text = render(verdicts, cov, unattributed=1)
    assert "覆盖 1/2" in text
    assert "不能据此认定其余持仓安全" in text
    assert "全部核验完毕" not in text
    assert "未披露" not in text


def test_complete_coverage_can_conclude_all_clear():
    verdicts, cov = evaluate_holdings(
        _rows(("600519", "CN", 10.0, 10.0), ("000001", "CN", 10.0, 10.0)),
        {("CN", "600519"): _quote("600519", 12.0),
         ("CN", "000001"): _quote("000001", 11.0)},
        now=CLOSED_NOW,
    )
    text = render(verdicts, cov)
    assert cov.complete is True
    assert "全部核验完毕" in text and "未跌破" in text
    assert "不能据此认定" not in text


def test_render_marks_every_undefended_position():
    verdicts, cov = evaluate_holdings(
        _rows(("600519", "CN", None, 10.0), ("000001", "CN", 10.0, 10.0)),
        {("CN", "600519"): _quote("600519", 12.0),
         ("CN", "000001"): _quote("000001", 12.0)},
        now=CLOSED_NOW,
    )
    text = render(verdicts, cov)
    assert "未设防守位" in text
    assert "没设防守位" in text  # 末尾提示


# ------------------------------------------------------------------ ⑤ 越权 / 串号


def test_other_users_positions_never_enter_denominator(db, monkeypatch):
    owner = _user(db, "owner")
    intruder = _user(db, "intruder")
    owner_acc = _account(db, "owner-账户", owner.id)
    intruder_acc = _account(db, "intruder-账户", intruder.id)
    st1 = _stock(db, "600519")
    st2 = _stock(db, "000001")
    _position(db, owner_acc, st1, user_id=owner.id, defense_price=10.0)
    _position(db, intruder_acc, st2, user_id=intruder.id, defense_price=10.0)

    rows, _u, _z = load_holdings(db, intruder.id)
    assert [r.symbol for r in rows] == ["000001"]

    _patch_quotes(monkeypatch, {"600519": _quote("600519", 1.0),
                                "000001": _quote("000001", 12.0)})
    text = asyncio.run(answer(db, "我的持仓防守位破了吗", None, intruder))
    assert text is not None
    assert "600519" not in text
    assert "覆盖 1/1" in text and "未跌破" in text


def test_shared_account_with_others_position_is_excluded(db, monkeypatch):
    """共享账户(account.user_id=NULL)里别人建的持仓不能算成当前用户的仓位。"""
    owner = _user(db, "owner2")
    viewer = _user(db, "viewer2")
    shared = _account(db, "历史共享账户", None)
    st = _stock(db, "600519")
    _position(db, shared, st, user_id=owner.id, defense_price=10.0)  # 别人的
    _patch_quotes(monkeypatch, {"600519": _quote("600519", 1.0)})

    rows, _u, _z = load_holdings(db, viewer.id)
    assert rows == []
    text = asyncio.run(answer(db, "我的持仓防守位破了吗", None, viewer))
    assert text is not None and "没有可核验的持仓" in text


def test_unattributed_rows_are_excluded_and_reported(db, monkeypatch):
    """账户与持仓两侧都 NULL 的无主历史行：不纳入分母，但报条数。"""
    user = _user(db, "legacy")
    acc = _account(db, "无主账户", None)
    st = _stock(db, "600519")
    _position(db, acc, st, user_id=None, defense_price=10.0)
    _patch_quotes(monkeypatch, {"600519": _quote("600519", 1.0)})

    rows, unattributed, _z = load_holdings(db, user.id)
    assert rows == [] and unattributed == 1

    text = asyncio.run(answer(db, "我的持仓防守位破了吗", None, user))
    assert text is not None
    assert "归属未标注" in text


def test_owned_position_inside_shared_account_is_kept(db):
    """账户历史 NULL、持仓是本人的 → 仍算本人持仓（与 portfolio_scope 同口径）。"""
    user = _user(db, "mixed")
    acc = _account(db, "历史共享账户", None)
    st = _stock(db, "600519")
    _position(db, acc, st, user_id=user.id, defense_price=10.0)
    rows, unattributed, _z = load_holdings(db, user.id)
    assert [r.symbol for r in rows] == ["600519"] and unattributed == 0


def test_no_user_session_fails_closed(db):
    """无会话（服务令牌/后台线程）一律 None —— 私有数据路径不许退化成全表。"""
    _user(db, "someone")
    assert asyncio.run(answer(db, "我的持仓防守位破了吗", None, None)) is None
    assert asyncio.run(answer(db, "我的持仓防守位破了吗", None, SimpleNamespace(id=None))) is None
    assert load_holdings(db, None)[0] == []


def test_paper_positions_are_out_of_scope(db, monkeypatch):
    """模拟盘不进实盘防守位口径（defense_price 只挂在实盘持仓上）。"""
    user = _user(db, "paper")
    db.add(
        PaperTradingPosition(
            user_id=user.id, stock_symbol="600519", stock_name="贵州茅台",
            stock_market="CN", quantity=100, entry_price=10.0, status="open",
        )
    )
    db.commit()
    _patch_quotes(monkeypatch, {"600519": _quote("600519", 1.0)})
    rows, _u, _z = load_holdings(db, user.id)
    assert rows == []
    text = asyncio.run(answer(db, "我的持仓防守位破了吗", None, user))
    assert text is not None and "模拟盘不在本口径内" in text


def test_disabled_account_and_zero_quantity_are_excluded(db):
    user = _user(db, "off")
    acc_on = _account(db, "启用", user.id)
    acc_off = _account(db, "停用", user.id, enabled=False)
    st1 = _stock(db, "600519")
    st2 = _stock(db, "000001")
    st3 = _stock(db, "600036")
    _position(db, acc_on, st1, user_id=user.id, defense_price=10.0)
    _position(db, acc_off, st2, user_id=user.id, defense_price=10.0)
    _position(db, acc_on, st3, user_id=user.id, defense_price=10.0, qty=0)

    rows, _u, zero = load_holdings(db, user.id)
    assert [r.symbol for r in rows] == ["600519"]
    assert zero == 1


def test_explicit_code_not_held_is_reported_not_judged(db, monkeypatch):
    """点名的票不在本人实盘里 → 明说不在，不拿别的仓位替它答，也不判安全。"""
    user = _user(db, "named")
    acc = _account(db, "主账户", user.id)
    st = _stock(db, "000001")
    _position(db, acc, st, user_id=user.id, defense_price=10.0)
    _patch_quotes(monkeypatch, {"600519": _quote("600519", 1.0),
                                "000001": _quote("000001", 12.0)})

    text = asyncio.run(answer(db, "我的持仓里 600519 破防守线了吗", None, user))
    assert text is not None
    assert "不在你的实盘持仓里" in text
    assert "已跌破" not in text  # 没有替它判


def test_explicit_code_is_scoped_when_held(db, monkeypatch):
    user = _user(db, "named2")
    acc = _account(db, "主账户", user.id)
    st1 = _stock(db, "600519")
    st2 = _stock(db, "000001")
    _position(db, acc, st1, user_id=user.id, defense_price=10.0)
    _position(db, acc, st2, user_id=user.id, defense_price=999.0)  # 若被纳入会误报
    _patch_quotes(monkeypatch, {"600519": _quote("600519", 12.0),
                                "000001": _quote("000001", 12.0)})

    text = asyncio.run(answer(db, "我的持仓里 600519 有没有破防守位", None, user))
    assert text is not None
    assert "本次只核验正文点名的：600519" in text
    assert "覆盖 1/1" in text and "均未跌破" in text


# ------------------------------------------------------------------ ⑥ 误路由（必须 None）


@pytest.mark.parametrize(
    "text",
    [
        "600519 现价多少",
        "600519 破位了吗",            # 单股问句, 没有持仓词
        "我的持仓怎么样",              # 诊断, 没有防守词
        "帮我设置防守位",              # 写意图
        "我的持仓防守位该设多少",       # 建议意图
        "我的持仓防守位分别在哪",       # 问价位, 不是核验
        "600519 的防守位在哪",
        "今天大盘怎么样",
        "帮我选几个防守性强的票",
        "我的持仓防守位怎么调整",
        "",
    ],
)
def test_non_defense_questions_return_none(db, text):
    user = _user(db, "route")
    acc = _account(db, "主账户", user.id)
    st = _stock(db, "600519")
    _position(db, acc, st, user_id=user.id, defense_price=10.0)
    assert asyncio.run(answer(db, text, "600519", user)) is None


@pytest.mark.parametrize(
    "text",
    [
        "我的持仓防守位破了吗？",
        "帮我检查一下持仓有没有破防守位",
        "持仓破位了吗",
        "我持仓的防守位是否触发",
        "实盘持仓的防线守住了吗",
    ],
)
def test_defense_questions_are_recognised(text):
    from src.harness.portfolio_risk import is_portfolio_defense_question

    assert is_portfolio_defense_question(text) is True


# ------------------------------------------------------------------ ⑦ 接近真实执行链


def _chat_fast_path(db, text, bound, user, runtime):
    """复刻聊天入口的旁路顺序：问价快答 → 组合防守位核验 → 否则交回主流程。

    与 `src/web/api/chat.py::_run_tool_loop_inner` 里那段同序（那边只差 3 行接线）；
    这里把它显式写出来，是为了让「谁先答、答不到会怎样」可测。
    """
    from src.harness import quick_answer

    async def _run():
        qa = await quick_answer.answer(db, text, bound, user, runtime)
        if qa:
            return qa, "quick_answer"
        risk = await answer(db, text, bound, user, runtime)
        if risk:
            return risk, "portfolio_risk"
        return None, "main_loop"

    return asyncio.run(_run())


class _Trace:
    """轨迹句柄替身（字段与 CopilotTrace.step 对齐）。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.run_id = "run-t-risk"

    def step(self, tool, args=None, *, ok=True, error=None, result=None, elapsed_ms=None):
        self.steps.append({"tool": str(tool), "args": dict(args or {}), "ok": bool(ok),
                           "error": error, "result": result})


def test_fast_path_takes_defense_question_and_leaves_quotes_to_quick_answer(db, monkeypatch):
    user = _user(db, "chain")
    acc = _account(db, "主账户", user.id)
    st1 = _stock(db, "600519")
    st2 = _stock(db, "000001")
    _position(db, acc, st1, user_id=user.id, defense_price=10.0)
    _position(db, acc, st2, user_id=user.id, defense_price=10.0)
    quotes = {"600519": _quote("600519", 9.0), "000001": _quote("000001", 12.0)}
    monkeypatch.setattr(
        "src.core.marketdata_client.md_quote_rows",
        lambda symbols, market: [quotes[s] for s in symbols if s in quotes],
    )

    trace = _Trace()
    runtime = SimpleNamespace(trace=trace)
    out, who = _chat_fast_path(db, "我的持仓防守位破了吗？", None, user, runtime)

    assert who == "portfolio_risk"
    assert out and "1 笔**已跌破**防守位，1 笔未跌破" in out
    steps = [s for s in trace.steps if s["tool"] == "portfolio_defense_check"]
    assert len(steps) == 1
    assert steps[0]["args"] == {"scope": "self_holdings", "markets": ["CN"]}
    # 全部都核验到了才算这一步成功
    assert steps[0]["ok"] is True and steps[0]["error"] is None
    # 运行记录不落完整私有数据：只有计数, 没有代码/价格/账户名
    payload = f"{steps[0]['args']}{steps[0]['result']}"
    assert "600519" not in payload and "000001" not in payload
    assert "9.0" not in payload and "主账户" not in payload


def test_trace_step_partial_when_user_has_no_defense_line(db, monkeypatch):
    """「用户没设防守位」是配置状态，不是故障：标 partial，不判失败。"""
    user = _user(db, "partial")
    acc = _account(db, "主账户", user.id)
    st1 = _stock(db, "600519")
    st2 = _stock(db, "000001")
    _position(db, acc, st1, user_id=user.id, defense_price=10.0)
    _position(db, acc, st2, user_id=user.id, defense_price=None)   # 没设防守位 → unknown
    quotes = {"600519": _quote("600519", 12.0), "000001": _quote("000001", 12.0)}
    monkeypatch.setattr(
        "src.core.marketdata_client.md_quote_rows",
        lambda symbols, market: [quotes[s] for s in symbols if s in quotes],
    )

    trace = _Trace()
    out = asyncio.run(answer(db, "我的持仓防守位破了吗？", None, user,
                             SimpleNamespace(trace=trace)))
    assert out is not None

    steps = [s for s in trace.steps if s["tool"] == "portfolio_defense_check"]
    assert len(steps) == 1
    assert steps[0]["ok"] is True and steps[0]["error"] is None
    assert "verified=1" in steps[0]["result"] and "unknown=1" in steps[0]["result"]
    assert "no_defense_line=1" in steps[0]["result"]
    assert "reasons=no_defense_line:1" in steps[0]["result"]


def test_trace_step_is_failed_when_quotes_unavailable(db, monkeypatch):
    """行情取数失败是数据不可用，必须落 failed：不能把「证不出」记成 ok。"""
    user = _user(db, "noquote")
    acc = _account(db, "主账户", user.id)
    st = _stock(db, "600519")
    _position(db, acc, st, user_id=user.id, defense_price=10.0)
    monkeypatch.setattr("src.core.marketdata_client.md_quote_rows", lambda *a, **k: [])

    trace = _Trace()
    out = asyncio.run(answer(db, "我的持仓防守位破了吗？", None, user,
                             SimpleNamespace(trace=trace)))
    assert out is not None and "无法核验" in out

    steps = [s for s in trace.steps if s["tool"] == "portfolio_defense_check"]
    assert len(steps) == 1
    assert steps[0]["ok"] is False
    assert steps[0]["error"] == "holdings_not_fully_verified"
    assert "missing_quote:1" in steps[0]["result"]


@pytest.mark.parametrize("text", [
    "汇金持仓破位了吗",
    "社保持仓跌破防线了吗",
    "国家队持仓防守位查一下",
])
def test_institution_questions_are_not_claimed_as_own_portfolio(text):
    """点名机构的问句不是「本人持仓」：否则答的是自己账户，机构直查永远轮不到。"""
    from src.harness.portfolio_risk import is_portfolio_defense_question

    assert is_portfolio_defense_question(text) is False


def test_unavailable_institution_resolver_never_reads_own_portfolio(monkeypatch, caplog):
    from src.core import national_team
    from src.harness import portfolio_risk

    def broken_scope(_):
        raise RuntimeError("private failure detail")

    def unexpected_load(*_):
        pytest.fail("scope unresolved; must not read personal holdings")

    monkeypatch.setattr(national_team, "resolve_institution_scopes", broken_scope)
    monkeypatch.setattr(portfolio_risk, "load_holdings", unexpected_load)
    assert asyncio.run(answer(object(), "汇金持仓破位了吗", None, SimpleNamespace(id="u"))) is None
    assert "机构范围" in caplog.text and "private failure detail" not in caplog.text


def test_unavailable_code_parser_never_widens_portfolio_query(monkeypatch, caplog):
    from src.harness import pipeline_router, portfolio_risk

    def broken_codes(_):
        raise RuntimeError("private holdings detail")

    def unexpected_load(*_):
        pytest.fail("code scope not established; must not read whole portfolio")

    monkeypatch.setattr(pipeline_router, "question_codes", broken_codes)
    monkeypatch.setattr(portfolio_risk, "load_holdings", unexpected_load)
    out = asyncio.run(answer(object(), "我的持仓600519跌破防守位了吗", None, SimpleNamespace(id="u")))
    assert out and "未完成核验" in out
    assert "RuntimeError" in caplog.text and "private holdings detail" not in caplog.text


def test_holdings_database_failure_does_not_log_bound_parameters(monkeypatch, caplog):
    from src.harness import portfolio_risk

    def failed_database(*_):
        raise RuntimeError("private holdings SQL parameters")

    monkeypatch.setattr(portfolio_risk, "load_holdings", failed_database)
    out = asyncio.run(answer(object(), "我的持仓跌破防守位了吗", None, SimpleNamespace(id="u")))
    assert out and "未完成核验" in out and "private holdings SQL parameters" not in out
    assert "RuntimeError" in caplog.text and "private holdings SQL parameters" not in caplog.text


def test_fast_path_falls_through_for_single_stock_quote(db, monkeypatch):
    """单股问价不能走防守位核验（否则用户永远拿不到纯读数快答）。"""
    user = _user(db, "chain2")
    acc = _account(db, "主账户", user.id)
    st = _stock(db, "600519")
    _position(db, acc, st, user_id=user.id, defense_price=10.0)
    monkeypatch.setattr("src.core.marketdata_client.md_quote_rows", lambda *a, **k: [])
    # quick_answer 需要注册表里的工具；这里只断言 risk 侧不抢
    assert asyncio.run(answer(db, "600519 现价多少", "600519", user)) is None


def test_failed_defense_check_returns_unverified_and_marks_trace(db, monkeypatch):
    """确认是本人防守位核验后，底层异常不能交给模型猜测是否安全。"""
    user = _user(db, "boom")
    monkeypatch.setattr(
        "src.harness.portfolio_risk.load_holdings",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    trace = _Trace()
    out = asyncio.run(answer(db, "我的持仓防守位破了吗", None, user, SimpleNamespace(trace=trace)))
    assert out and out.startswith("【持仓防守位核验】未完成核验")
    assert "不能判断是否触及防守位" in out
    assert len(trace.steps) == 1
    step = trace.steps[0]
    assert step["tool"] == "portfolio_defense_check" and step["ok"] is False
    assert step["error"] == "portfolio_verification_failed" and "boom" not in repr(step)


def test_trace_failure_does_not_break_answer(db, monkeypatch):
    class _BadTrace:
        run_id = "x"

        def step(self, *a, **k):
            raise RuntimeError("trace down")

    user = _user(db, "notrace")
    acc = _account(db, "主账户", user.id)
    st = _stock(db, "600519")
    _position(db, acc, st, user_id=user.id, defense_price=10.0)
    monkeypatch.setattr(
        "src.core.marketdata_client.md_quote_rows",
        lambda symbols, market: [_quote("600519", 12.0)],
    )
    out = asyncio.run(answer(db, "我的持仓防守位破了吗", None, user,
                             SimpleNamespace(trace=_BadTrace())))
    assert out is not None and "未跌破" in out


def test_answer_writes_nothing(db, monkeypatch):
    """只读：不设防线、不改数量、不新增提醒/持仓行。"""
    user = _user(db, "readonly")
    acc = _account(db, "主账户", user.id)
    st = _stock(db, "600519")
    pos = _position(db, acc, st, user_id=user.id, defense_price=10.0)
    before = (pos.defense_price, pos.quantity, db.query(Position).count())
    monkeypatch.setattr(
        "src.core.marketdata_client.md_quote_rows",
        lambda symbols, market: [_quote("600519", 9.0)],
    )
    asyncio.run(answer(db, "我的持仓防守位破了吗", None, user))
    db.expire_all()
    after = (pos.defense_price, pos.quantity, db.query(Position).count())
    assert before == after
