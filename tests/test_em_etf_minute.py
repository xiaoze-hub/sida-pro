"""东财 trends2 → ETF 分钟成交额兜底源：口径、完整性、来源隔离（2026-11-10）。

为什么要有这个兜底：TQ 的 1m 全部来自客户端 `minline/*.lc1`，2026-11-05 晚间后
未再下载 ⇒ 11/06、11/09 的 1m 恒 0 根（`ErrorId:"0"` + `Value:[]`，不报错）。
东财 `trends2` 仍在服务这些日期（实测滚动 5 个交易日、每天 241 根）。

本文件钉死的四件事（任一写错都会静默出错、不抛异常）：

1. **单位**：`f56` 是「手」→ ×100 才是股；`f57` 是「元」→ ÷1e4 才是库内 `amount_wan`
   （万元）。乘除反了图上仍是「几亿」，只是量级差 100 倍。
2. **竞价归属**：东财把 09:30 集合竞价单独成一根，TQ 把它并进 09:31。不合并就
   241 根，且基线按分钟对齐时多出一个 TQ 永远没有的 09:30 —— 必须并入 09:31。
3. **不许覆盖、不许混源**：同一交易日要么全是 TQ（`tdx_tq`），要么全是东财
   （`em_trends2`）；已有任何行（任何来源）的日期一律跳过，写入只用
   INSERT-IGNORE，永不 UPDATE 已有分钟。
4. **不许拿残缺冒充完整**：根数不足、末根不是 15:00、单位自洽性不过、标的数
   不过 80% 闸门 —— 整天不写（报告写原因），绝不写一部分当完成。

数据库全部用 tmp_path 下的独立 SQLite（`m243` + `m245` 建表），不碰 `src.web.database`。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, text

from src.collectors import em_etf_minute as em


# ── 夹具：独立库 + 真实报文片段 ──────────────────────────────────────────

#: 2026-11-05 510300.SH 真实 trends2 行（09:30 / 09:31 / 15:00，实测抓取）。
#: 09:30 那根 163,884 手 = 16,388,400 份，与腾讯 mkline 完全一致。
_REAL_1105 = {
    "0930": ("0.000", "4.149", "4.149", "4.149", "163884", "67995472.000", "4.1490"),
    "0931": ("0.000", "4.155", "4.159", "4.145", "927369", "385137072.000", "4.1524"),
    "1500": ("0.000", "4.195", "4.195", "4.188", "154264", "64713748.000", "4.1837"),
}

_AUCTION_MINUTE = "09:30"


def _session_minutes() -> list[str]:
    """09:30 竞价 + 09:31–11:30 + 13:01–15:00 = 241 根（与东财实测一致，无 13:00）。"""
    out = [_AUCTION_MINUTE]
    for h, m0, m1 in ((9, 31, 60), (10, 0, 60), (11, 0, 31)):
        out += [f"{h:02d}:{m:02d}" for m in range(m0, m1)]
    for h, m0, m1 in ((13, 1, 60), (14, 0, 60), (15, 0, 1)):
        out += [f"{h:02d}:{m:02d}" for m in range(m0, m1)]
    return out


def _trends_line(day: str, hhmm: str, close: str, lots: str, yuan: str, avg: str) -> str:
    return f"{day} {hhmm},0.000,{close},{close},{close},{lots},{yuan},{avg}"


def _full_day_lines(day: str, *, price: float = 4.17, lots: int = 1000) -> list[str]:
    """构造一天的 241 行；三根真实锚点用实测值，其余用自洽的价/量/额。"""
    lines: list[str] = []
    minutes = _session_minutes()
    for hhmm in minutes:
        key = hhmm.replace(":", "")
        if day == "2026-11-05" and key in _REAL_1105:
            o, c, h, low, v, a, avg = _REAL_1105[key]
            lines.append(f"{day} {hhmm},{o},{c},{h},{low},{v},{a},{avg}")
            continue
        yuan = f"{lots * 100 * price:.3f}"
        lines.append(_trends_line(day, hhmm, f"{price:.3f}", str(lots), yuan, f"{price:.4f}"))
    assert len(lines) == 241
    return lines


def _payload(code: str, days: list[str], **kw) -> dict:
    trends: list[str] = []
    for d in days:
        trends += _full_day_lines(d, **kw)
    return {
        "rc": 0,
        "data": {
            "code": code,
            "market": 1 if code.startswith(("5", "6", "9")) else 0,
            "name": "沪深300ETF华泰柏瑞",
            "decimal": 3,
            "preClose": 4.13,
            "trends": trends,
        },
    }


@pytest.fixture()
def iso_engine(tmp_path):
    """独立 SQLite：只建本模块需要的两张表（走真实迁移函数，不碰生产库）。"""
    from src.web.migrations import _m243_etf_minute_flow, _m245_etf_minute_source

    eng = create_engine(f"sqlite:///{tmp_path / 'etf_minute.db'}")
    with eng.begin() as conn:
        _m243_etf_minute_flow(conn)
        _m245_etf_minute_source(conn)
    yield eng
    eng.dispose()


def _rows(eng, day: str | None = None):
    sql = "SELECT trade_date, minute, etf_code, amount_wan, volume, source FROM etf_minute_flow"
    params = {}
    if day:
        sql += " WHERE trade_date = :d"
        params["d"] = day
    with eng.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _fake_fetch(payload_by_code):
    """把 HTTP 缝换成按代码返回固定报文（记录调用次数）。"""
    calls: list[str] = []

    def _inner(code: str, *, ndays: int = 5, timeout: float = 8.0):
        calls.append(code)
        p = payload_by_code(code)
        return p

    _inner.calls = calls  # type: ignore[attr-defined]
    return _inner


_CODES = [c for c, _n, _cat in __import__("src.collectors.tq_etf_watch", fromlist=["x"]).ETF_BASKET]


# ── 1. 代码 → secid（错映射会静默返回 null） ─────────────────────────────

def test_secid_uses_market_prefix_not_guesswork():
    assert em.secid("510300.SH") == "1.510300"
    assert em.secid("588000.SH") == "1.588000"
    assert em.secid("159919.SZ") == "0.159919"
    assert em.secid("000001.SZ") == "0.000001"


def test_secid_refuses_unknown_format():
    """猜不出市场就别猜 —— 传错的 secid 只会静默 0 数据。"""
    for bad in ("", "510300", "ABC.SH", "999999.HK"):
        with pytest.raises(ValueError):
            em.secid(bad)


# ── 2. 解析与单位 ───────────────────────────────────────────────────────

def test_parse_converts_lots_to_shares_and_yuan_to_wan():
    p = {"data": {"code": "510300", "trends": _full_day_lines("2026-11-05")}}
    by_day = em.parse_trends(p, "510300.SH")
    bars = by_day["20261105"]
    assert len(bars) == 241
    first = bars[0]
    assert first.minute == "0930"
    # 163,884 手 → 16,388,400 股；67,995,472 元 → 6,799.5472 万元
    assert first.volume == 16_388_400
    assert first.amount_wan == pytest.approx(6_799.5472, abs=1e-6)
    assert bars[-1].minute == "1500"


def test_parse_preserves_real_trade_dates_only():
    """日期来自报文本身（真实交易日），周末/缺日不会凭空造行。"""
    p = _payload("510300", ["2026-11-05", "2026-11-06"])
    by_day = em.parse_trends(p, "510300.SH")
    assert sorted(by_day) == ["20261105", "20261106"]


def test_parse_rejects_payload_for_other_instrument():
    """secid 传错（如指数 999999 与 1.000001 混用）时返回 null 或异码，必须显式失败。"""
    with pytest.raises(em.SourceMismatch):
        em.parse_trends(_payload("000001", ["2026-11-05"]), "510300.SH")


def test_parse_returns_empty_for_null_data():
    assert em.parse_trends({"rc": 0, "data": None}, "510300.SH") == {}


# ── 3. 竞价归一（09:30 → 09:31） ────────────────────────────────────────

def test_canonicalize_merges_auction_into_0931():
    p = _payload("510300", ["2026-11-05"])
    raw = em.parse_trends(p, "510300.SH")["20261105"]
    merged = em.canonicalize_auction(raw)
    assert len(merged) == 240, "241 根含 09:30 竞价 → 并入 09:31 后应是 240 根"
    assert merged[0].minute == "0931"
    assert "0930" not in [b.minute for b in merged]
    # 额、量相加（真实锚点：67,995,472 + 385,137,072 元）
    expect_wan = (67_995_472.0 + 385_137_072.0) / 1e4
    assert merged[0].amount_wan == pytest.approx(expect_wan, abs=1e-4)
    assert merged[0].volume == 16_388_400 + 92_736_900
    # 收盘价取 09:31 那根（并进来的竞价不改变分钟收盘）
    assert merged[0].close == pytest.approx(4.155)
    assert merged[-1].minute == "1500"
    assert all(b.minute != "0930" for b in merged)


def test_canonicalized_0931_matches_real_tq_bar():
    """跨源锚点：并入竞价后的 09:31 根必须等于 TQ 实测 09:31 根。

    TQ 2026-11-05 510300.SH 09:31 实测（经网关抓取落盘）：Amount 45,313.26 万元、
    Volume 109,125,296 股、Close 4.155。东财同日 09:30(67,995,472 元/163,884 手)
    + 09:31(385,137,072 元/927,369 手) 相加 = 45,313.2544 万元 / 109,125,300 股。
    差 56 元 / 4 股（手取整），价格完全一致 —— 这就是「把竞价并进 09:31」的依据。
    """
    p = _payload("510300", ["2026-11-05"])
    merged = em.canonicalize_auction(em.parse_trends(p, "510300.SH")["20261105"])
    bar = merged[0]
    assert bar.minute == "0931"
    assert bar.close == pytest.approx(4.155, abs=1e-9)
    assert abs(bar.amount_wan - 45_313.26) <= 0.01      # ≤100 元
    assert abs(bar.volume - 109_125_296) <= 100         # ≤100 股（手取整）


def test_canonicalize_is_idempotent_when_0930_absent():
    p = _payload("510300", ["2026-11-05"])
    merged = em.canonicalize_auction(em.parse_trends(p, "510300.SH")["20261105"])
    again = em.canonicalize_auction(merged)
    assert [b.minute for b in again] == [b.minute for b in merged]
    assert again[0].amount_wan == pytest.approx(merged[0].amount_wan)


def test_canonicalize_keeps_auction_when_0931_missing():
    """极端情形：只有 09:30、没有 09:31 → 竞价本身成为 09:31，不丢金额。"""
    raw = [em.MinuteBar("0930", 100.0, 10_000, 4.1)]
    merged = em.canonicalize_auction(raw)
    assert [(b.minute, b.amount_wan, b.volume) for b in merged] == [("0931", 100.0, 10_000)]


# ── 4. 完整性 / 单位自洽 校验 ───────────────────────────────────────────

def _clean_bars(day: str = "2026-11-05"):
    """一天 240 根（已把 09:30 竞价并入 09:31）。"""
    p = _payload("510300", [day])
    return em.canonicalize_auction(em.parse_trends(p, "510300.SH")[day.replace("-", "")])


def test_validate_accepts_complete_day():
    ok, problems = em.validate_day("20261105", _clean_bars())
    assert ok, problems
    assert problems == []


def test_validate_rejects_partial_day_last_minute_before_1500():
    bars = [b for b in _clean_bars() if b.minute <= "1459"]
    ok, problems = em.validate_day("20261105", bars)
    assert not ok
    assert any("1500" in p or "partial" in p for p in problems)


def test_validate_rejects_short_day():
    bars = _clean_bars()[:200]
    ok, problems = em.validate_day("20261105", bars)
    assert not ok
    assert any("根数" in p for p in problems)


def test_validate_rejects_wrong_volume_unit():
    """手没 ×100（或少乘 100 倍）→ 单位自洽性崩，必须拒绝而不是入库。"""
    bars = [em.MinuteBar(b.minute, b.amount_wan, b.volume // 100, b.close) for b in _clean_bars()]
    ok, problems = em.validate_day("20261105", bars)
    assert not ok
    assert any("单位" in p for p in problems)


def test_validate_tolerates_zero_volume_placeholder_minutes():
    bars = list(_clean_bars())
    idx = [i for i, b in enumerate(bars) if b.minute in ("1458", "1459")]
    for i in idx:
        b = bars[i]
        bars[i] = em.MinuteBar(b.minute, 0.0, 0, b.close)
    ok, problems = em.validate_day("20261105", bars)
    assert ok, problems


def test_validate_rejects_out_of_session_minutes():
    bars = _clean_bars() + [em.MinuteBar("1300", 1.0, 1000, 4.1), em.MinuteBar("1200", 1.0, 1000, 4.1)]
    ok, problems = em.validate_day("20261105", bars)
    assert not ok
    assert any("时段" in p for p in problems)


# ── 5. 写入：只 INSERT，不覆盖，不混源 ──────────────────────────────────

def test_write_day_never_overwrites_existing_rows(iso_engine):
    with iso_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO etf_minute_flow "
                "(trade_date, minute, etf_code, amount_wan, volume, source) "
                "VALUES ('20261105','0931','510300.SH', 1.0, 100, 'tdx_tq')"
            )
        )
    n = em.write_day("20261105", {"510300.SH": _clean_bars()}, engine=iso_engine)
    assert n == 0
    rows = _rows(iso_engine, "20261105")
    assert len(rows) == 1
    assert rows[0]["amount_wan"] == 1.0
    assert rows[0]["source"] == "tdx_tq"


def test_write_day_sets_source_on_new_rows(iso_engine):
    n = em.write_day("20261105", {"510300.SH": _clean_bars()}, engine=iso_engine)
    assert n == 240
    rows = _rows(iso_engine, "20261105")
    assert {r["source"] for r in rows} == {"em_trends2"}
    assert len({r["minute"] for r in rows}) == 240


def test_write_day_is_all_or_nothing_on_db_error(iso_engine):
    """写库失败返回 -1（不抛进调度层），且整日回滚：不许留半天的行。"""
    class Boom(Exception):
        pass

    bars = {"510300.SH": _clean_bars(), "510050.SH": _clean_bars()}
    real_begin = iso_engine.begin

    def _boom():
        raise Boom("disk full")

    iso_engine.begin = _boom  # type: ignore[assignment]
    try:
        assert em.write_day("20261105", bars, engine=iso_engine) == -1
    finally:
        iso_engine.begin = real_begin  # type: ignore[assignment]
    assert _rows(iso_engine) == []


# ── 6. 编排：日期纪律 + 覆盖率闸门 + 对账 ───────────────────────────────

def _all_codes_payload(code: str):
    return _payload(code.split(".")[0], ["2026-11-06"])


def test_dry_run_plans_but_writes_nothing(iso_engine, monkeypatch):
    monkeypatch.setattr(em, "_get_json", _fake_fetch(lambda c: _payload(c.split(".")[0], ["2026-11-06"])))
    rep = em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=True)
    assert _rows(iso_engine) == []
    day = rep["days"][0]
    assert day["action"] == "planned"
    assert day["etfs"] == len(_CODES)


def test_backfill_writes_complete_date_with_provenance(iso_engine, monkeypatch):
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_codes_payload))
    rep = em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    rows = _rows(iso_engine, "20261106")
    assert len(rows) == len(_CODES) * 240
    assert {r["source"] for r in rows} == {"em_trends2"}
    assert {r["minute"] for r in rows} >= {"0931", "1130", "1500"}
    assert "0930" not in {r["minute"] for r in rows}
    assert rep["days"][0]["action"] == "written"
    assert rep["days"][0]["rows"] == len(_CODES) * 240
    assert rep["source"] == "em_trends2"
    assert rep["caliber"]["amount_wan"] == "万元"
    assert rep["caliber"]["volume"] == "股"
    assert "09:31" in rep["caliber"]["auction"]


def test_backfill_skips_date_that_already_has_any_rows(iso_engine, monkeypatch):
    """已有行（哪怕只有 1 行）就不再取数/写入；但只有 1 行属于残缺日 → 必须报 partial。

    旧行为把「有行」当「已采完」→ CLI 退出码 0，把只有零星几只的日期掩盖成成功。
    现在 `date_partially_ingested` 不在 benign 集合里，exit_code 返回 1。
    """
    with iso_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO etf_minute_flow "
                "(trade_date, minute, etf_code, amount_wan, volume, source) "
                "VALUES ('20261106','0931','510300.SH', 5.0, 100, 'tdx_tq')"
            )
        )
    fetch = _fake_fetch(_all_codes_payload)
    monkeypatch.setattr(em, "_get_json", fetch)
    rep = em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "date_partially_ingested"
    assert day["existing_sources"] == ["tdx_tq"]
    assert day["existing_codes"] == 1
    assert fetch.calls == [], "已有数据的日期连取数都不该做"
    assert len(_rows(iso_engine)) == 1
    assert em.exit_code(rep) == 1


def test_backfill_refuses_today_and_future(iso_engine, monkeypatch):
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_codes_payload))
    rep = em.backfill(
        ["20261110", "20261111"], engine=iso_engine, now=datetime(2026, 11, 10, 20, 0), dry_run=False
    )
    assert [d["action"] for d in rep["days"]] == ["skipped", "skipped"]
    assert {d["reason"] for d in rep["days"]} == {"not_closed_day"}
    assert _rows(iso_engine) == []


def test_backfill_skips_non_trading_day(iso_engine, monkeypatch):
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_codes_payload))
    rep = em.backfill(["20261107"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    assert rep["days"][0]["reason"] == "non_trading_day"
    assert _rows(iso_engine) == []


def test_backfill_reports_out_of_window_date(iso_engine, monkeypatch):
    """滚动 5 日窗口外的日期：明确报 out_of_window，不写任何东西。"""
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_codes_payload))
    rep = em.backfill(["20261103"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "out_of_window"
    assert _rows(iso_engine) == []


def test_backfill_aborts_whole_date_when_too_few_etfs(iso_engine, monkeypatch):
    """20 只里只回 10 只 → 整天不写（门槛 16/20），绝不写半边当完成。"""
    good = set(_CODES[:10])

    def _partial(code: str):
        if code not in good:
            return {"rc": 0, "data": None}
        return _payload(code.split(".")[0], ["2026-11-06"])

    monkeypatch.setattr(em, "_get_json", _fake_fetch(_partial))
    rep = em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "insufficient_etfs"
    assert day["etfs"] == 10
    assert _rows(iso_engine) == []


def test_backfill_refuses_write_when_one_basket_code_incomplete(iso_engine, monkeypatch):
    """19 只完整 + 1 只残缺 → **整天不写**（20/20 全篮子闸门），残只进 dropped。

    旧行为：丢弃残只后按 80% 闸门写 19 只，返库后还可能在别处被当成"已覆盖"。
    """
    bad = _CODES[3]

    def _fetch(code: str):
        if code == bad:
            p = _payload(code.split(".")[0], ["2026-11-06"])
            p["data"]["trends"] = p["data"]["trends"][:150]
            return p
        return _payload(code.split(".")[0], ["2026-11-06"])

    monkeypatch.setattr(em, "_get_json", _fake_fetch(_fetch))
    rep = em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "insufficient_etfs"
    assert day["expected_etfs"] == len(_CODES)
    assert day["etfs"] == len(_CODES) - 1
    assert bad in day["dropped"]
    assert bad in day["missing_codes"]
    assert day["verified"] is False
    assert _rows(iso_engine) == []
    assert em.exit_code(rep) == 1


def test_backfill_reconciles_against_daily_reference(iso_engine, monkeypatch):
    """给到日线参照（TQ 1d）时逐只对账：超差的标的整只丢弃，且 19/20 → 整天不写。"""
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_codes_payload))
    # 日线成交额含集合竞价：东财 241 根（09:30 独立一根 + 240 根）归一后金额一分不丢，
    # 所以参照必须是 241 根的合计，而不是 240 根 —— 写少了会误伤所有标的。
    totals = {c: 241 * 1000 * 100 * 4.17 / 1e4 for c in _CODES}
    skew = _CODES[0]
    totals[skew] *= 1.5  # 差 50% → 单位/标的错配

    rep = em.backfill(
        ["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0),
        dry_run=False, daily_ref=lambda d: totals, require_verified=True,
    )
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "unverified", "结构 20 只齐全但有一只对账超差 → 不可证实，不写"
    assert day["reconciled"] is False, "有一只超差被丢掉 → 写入集不完整，不能称已对账"
    assert day["verified"] is False, "有一只被对账丢掉 → 整体不可称 verified"
    assert skew in day["dropped"] and skew in day["missing_codes"]
    assert _rows(iso_engine) == []


def test_backfill_marks_unreconciled_when_reference_missing(iso_engine, monkeypatch):
    """取不到 TQ 日线 → 不假装对过账：reconciled=False 且写明原因。"""
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_codes_payload))

    def _no_ref(_d):
        raise RuntimeError("TQ 网关不可用")

    rep = em.backfill(
        ["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0),
        dry_run=False, daily_ref=_no_ref,
    )
    day = rep["days"][0]
    assert day["action"] == "written"
    assert day["reconciled"] is False
    assert "TQ" in day["reconcile_error"]


def test_backfill_is_idempotent_second_run_skips(iso_engine, monkeypatch):
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_codes_payload))
    first = em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    assert first["days"][0]["action"] == "written"
    n_after_first = len(_rows(iso_engine, "20261106"))
    second = em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    assert second["days"][0]["reason"] == "date_already_ingested"
    assert len(_rows(iso_engine, "20261106")) == n_after_first


def test_backfill_reports_fetch_failure_without_writing(iso_engine, monkeypatch):
    def _boom(_code, **_kw):
        raise RuntimeError("HTTP 502")

    monkeypatch.setattr(em, "_get_json", _boom)
    rep = em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "fetch_failed"
    assert "502" in day["error"]
    assert _rows(iso_engine) == []


# ── 6.5 迁移 245：存量行回填 tdx_tq ─────────────────────────────────────

def test_migration_245_backfills_existing_rows_as_tdx_tq(tmp_path):
    """老库（无 source 列）跑 245 后：存量行全部标 tdx_tq，且不新增/不改动其它列。

    迁移前该表的唯一写入方是 TQ 采集器（全仓 grep 结论），所以回填有据可依；
    NULL 只可能来自手工构造的数据，不能被当成"新来源"混进口径。
    """
    from src.web.migrations import _m243_etf_minute_flow, _m245_etf_minute_source

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        _m243_etf_minute_flow(conn)
        conn.execute(text(
            "INSERT INTO etf_minute_flow (trade_date, minute, etf_code, amount_wan, volume) "
            "VALUES ('20261105','0931','510300.SH', 45313.26, 109125296)"
        ))
    with eng.begin() as conn:
        _m245_etf_minute_source(conn)
    rows = _rows(eng)
    assert len(rows) == 1
    assert rows[0]["source"] == "tdx_tq"
    assert rows[0]["amount_wan"] == 45313.26
    assert rows[0]["volume"] == 109125296
    # 幂等：再跑一次不重复加列、不改值
    with eng.begin() as conn:
        _m245_etf_minute_source(conn)
    assert _rows(eng) == rows


# ── 7. 读侧契约：兜底数据能被现有只读面原样消费 ──────────────────────

def test_reader_consumes_fallback_day_without_0930(iso_engine, monkeypatch):
    """`src/core/etf_watch_feed` 不改一行就能吃这批数据：240 根、无 09:30 空洞。

    竞价不归一的话这里会多出一根 09:30，且 TQ 日没有对应分钟 —— 基线按分钟对齐时
    那个点永远是 1 个样本，图上就是一个假尖峰。
    """
    from src.core import etf_watch_feed as feed

    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_codes_payload))
    em.backfill(["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0), dry_run=False)

    def _run(key, params=None, fmt=None):
        sql = feed.SQL[key].format(**fmt) if fmt else feed.SQL[key]
        with iso_engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params or {}).mappings()]

    payload = feed.intraday_payload(_run, "20261106", now=datetime(2026, 11, 11, 9, 0))
    minutes = [s["minute"] for s in payload["series"]]
    assert minutes[0] == "0931" and minutes[-1] == "1500"
    assert "0930" not in minutes
    assert len(minutes) == 240
    assert payload["totals"]["coverage"] == 1.0
    assert payload["totals"]["strict_minutes"] == 240


# ── 8. 缺口盘查 / 退出码（运维入口用） ─────────────────────────────────

def test_exit_code_treats_benign_skips_as_success():
    """「本来就补不了/已经补过」不该让运维脚本报红，否则天天误报。"""
    ok = {"days": [
        {"date": "20261106", "action": "written"},
        {"date": "20261107", "action": "skipped", "reason": "non_trading_day"},
        {"date": "20261110", "action": "skipped", "reason": "not_closed_day"},
        {"date": "20261109", "action": "skipped", "reason": "date_already_ingested"},
    ]}
    assert em.exit_code(ok) == 0
    for reason in ("fetch_failed", "write_failed", "insufficient_etfs", "out_of_window"):
        bad = {"days": [{"date": "20261106", "action": "skipped", "reason": reason}]}
        assert em.exit_code(bad) == 1, reason


def test_gap_dates_lists_trading_days_without_rows(iso_engine):
    from src.collectors import tq_etf_watch as ew

    dates = ew.recent_trade_dates(5, end=date(2026, 11, 10))
    assert dates[-1] == "20261110"
    with iso_engine.begin() as conn:
        for code in _CODES:
            for hm in em.SESSION_MINUTES:
                conn.execute(
                    text(
                        "INSERT INTO etf_minute_flow "
                        "(trade_date, minute, etf_code, amount_wan, volume, source) "
                        "VALUES ('20261105',:m,:c, 1.0, 100, 'tdx_tq')"
                    ),
                    {"m": hm, "c": code},
                )
    gaps = em.gap_dates(days=5, end=date(2026, 11, 10), engine=iso_engine)
    assert "20261105" not in gaps
    assert "20261106" in gaps and "20261109" in gaps
    assert "20261110" in gaps, "今天（未收盘）也算缺口，由编排层 not_closed_day 拒绝写"


def test_gap_dates_counts_only_complete_baskets(iso_engine):
    """只写了一两只标的的日期不算「已覆盖」——那正是当前 11/06 的形态。"""
    with iso_engine.begin() as conn:
        for code in _CODES[:2]:
            conn.execute(
                text(
                    "INSERT INTO etf_minute_flow "
                    "(trade_date, minute, etf_code, amount_wan, volume, source) "
                    "VALUES ('20261106','0931',:c, 1.0, 100, 'tdx_tq')"
                ),
                {"c": code},
            )
    gaps = em.gap_dates(days=5, end=date(2026, 11, 10), engine=iso_engine)
    assert "20261106" in gaps


def test_gap_dates_flags_16_of_20_even_though_it_is_80_percent(iso_engine):
    """16/20（旧 80% 闸门会判"达标"）按全篮子口径必须是缺口。"""
    with iso_engine.begin() as conn:
        for code in _CODES[:16]:
            for hm in em.SESSION_MINUTES:
                conn.execute(
                    text(
                        "INSERT INTO etf_minute_flow "
                        "(trade_date, minute, etf_code, amount_wan, volume, source) "
                        "VALUES ('20261106',:m,:c, 1.0, 100, 'tdx_tq')"
                    ),
                    {"m": hm, "c": code},
                )
    assert "20261106" in em.gap_dates(days=5, end=date(2026, 11, 10), engine=iso_engine)


# ── 9. 审计阻断：全篮子 / NULL 来源归因 / 取数预算 ──────────────────────

def test_gate_is_full_basket_not_80_percent():
    assert em._gate(20) == 20, "20 只篮子必须 20 只齐全，80% 闸门(16)已删除"
    assert em._gate(3) == 3
    assert em.REQUIRED_COVERAGE == 1.0


def test_existing_overview_16_of_20_is_not_complete(iso_engine):
    """旧库里的 16/20 天必须判残缺（不可称 complete、不可被自动补缺跳过）。"""
    with iso_engine.begin() as conn:
        for code in _CODES[:16]:
            for hm in em.SESSION_MINUTES:
                conn.execute(
                    text(
                        "INSERT INTO etf_minute_flow "
                        "(trade_date, minute, etf_code, amount_wan, volume, source) "
                        "VALUES ('20261106',:m,:c, 1.0, 100, 'tdx_tq')"
                    ),
                    {"m": hm, "c": code},
                )
    ov = em.existing_overview(iso_engine, "20261106", min_codes=20, codes=_CODES)
    assert ov["complete"] is False
    assert ov["codes"] == 16
    assert ov["missing_codes"] == sorted(_CODES[16:])
    assert ov["short_codes"] == []


def test_existing_overview_requires_basket_membership_not_just_count(iso_engine):
    """20 个代码但其中一个不是篮子标的：仍算缺一只，不能按总数判 complete。"""
    stray = "510999.SH"
    codes = list(_CODES[:-1]) + [stray]
    with iso_engine.begin() as conn:
        for code in codes:
            for hm in em.SESSION_MINUTES:
                conn.execute(
                    text(
                        "INSERT INTO etf_minute_flow "
                        "(trade_date, minute, etf_code, amount_wan, volume, source) "
                        "VALUES ('20261106',:m,:c, 1.0, 100, 'tdx_tq')"
                    ),
                    {"m": hm, "c": code},
                )
    ov = em.existing_overview(iso_engine, "20261106", min_codes=20, codes=_CODES)
    assert ov["complete"] is False
    assert ov["missing_codes"] == [_CODES[-1]]
    assert ov["unexpected_codes"] == [stray]


def test_existing_overview_null_source_is_attributed_unknown(iso_engine):
    """NULL 来源不能被静默并进唯一来源：必须显式归为 unknown 且阻断 complete。"""
    with iso_engine.begin() as conn:
        for code in _CODES[:-1]:
            for hm in em.SESSION_MINUTES:
                conn.execute(
                    text(
                        "INSERT INTO etf_minute_flow "
                        "(trade_date, minute, etf_code, amount_wan, volume, source) "
                        "VALUES ('20261106',:m,:c, 1.0, 100, 'tdx_tq')"
                    ),
                    {"m": hm, "c": code},
                )
        for hm in em.SESSION_MINUTES:
            conn.execute(
                text(
                    "INSERT INTO etf_minute_flow "
                    "(trade_date, minute, etf_code, amount_wan, volume, source) "
                    "VALUES ('20261106',:m,:c, 1.0, 100, NULL)"
                ),
                {"m": hm, "c": _CODES[-1]},
            )
    ov = em.existing_overview(iso_engine, "20261106", min_codes=20, codes=_CODES)
    assert ov["sources"] == ["tdx_tq", "unknown"]
    assert ov["null_source_rows"] == len(em.SESSION_MINUTES)
    assert ov["complete"] is False


def test_tq_daily_ref_total_budget_bounds_paging(iso_engine, monkeypatch):
    """40 页 × 15s 的旧实现会拖 10 分钟；现在整次取数有 wall-clock 预算。"""
    from src.collectors import tq_etf_watch as ew

    calls: list[float] = []
    clock = {"t": 0.0}

    def _tick() -> float:
        clock["t"] += 2.0
        return clock["t"]

    codes = ["510300.SH", "159919.SZ", "510310.SH"]

    def _fake_rpc(method, params, timeout=20.0):
        calls.append(timeout)
        idx = int(params["stock_page_index"])
        code = codes[idx] if idx < len(codes) else codes[-1]
        return {
            "Value": {
                code: {"Date": ["20261106"], "Amount": [10008.0]},
            },
            "has_more": True,
        }

    monkeypatch.setattr(ew, "_rpc_full", _fake_rpc)
    ref = em.tq_daily_ref(codes, timeout=15.0, budget_s=5.0, monotonic=_tick)
    out = ref("20261106")
    assert calls, "至少应发一页"
    assert len(calls) < len(codes) + 1, f"预算耗尽后不应继续翻页: {len(calls)}"
    assert all(t <= 15.0 for t in calls)
    assert set(out) <= set(codes)

    # 一页都没拿到时必须显式报「预算」，不能静默返回空参照
    monkeypatch.setattr(ew, "_rpc_full", lambda *a, **kw: {"Value": {}, "has_more": True})
    empty_ref = em.tq_daily_ref(codes, budget_s=1.0, monotonic=_tick)
    with pytest.raises(RuntimeError, match="预算"):
        empty_ref("20261106")


def test_backfill_fetch_budget_stops_issuing_requests(iso_engine, monkeypatch):
    """取数预算用尽后不再发 HTTP，剩余标的显式记 budget exhausted。"""
    fetch = _fake_fetch(_all_codes_payload)
    monkeypatch.setattr(em, "_get_json", fetch)
    ticks = iter(range(0, 10_000, 100))
    rep = em.backfill(
        ["20261106"], engine=iso_engine, now=datetime(2026, 11, 10, 9, 0),
        dry_run=False, fetch_budget_s=150.0, monotonic=lambda: next(ticks),
    )
    day = rep["days"][0]
    assert len(fetch.calls) == 1
    assert day["action"] == "skipped"
    assert day["reason"] in ("insufficient_etfs", "fetch_failed")
    assert any("fetch_budget_exhausted" in v for v in day["dropped"].values())
    assert _rows(iso_engine) == []
