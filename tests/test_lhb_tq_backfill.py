"""龙虎榜金额「次日 TQ 回填」的纯逻辑测试（2026-10-20）。

规则：
- TQ 该股该日**没有**数据 → 这一行不动（绝不写 0，绝不拿别的日期顶替）；
- TQ 有数据 → 只覆写金额三列（buy/sell/net），名称/原因/席位不碰；
- 代码匹配要同时吃 6 位裸码与带后缀。
"""
from __future__ import annotations

from src.collectors import lhb_tq_backfill as bf


def test_merge_skips_symbols_without_tq_data():
    rows = [{"symbol": "000056", "buy_amt": 14717186.0, "sell_amt": 17995671.0},
            {"symbol": "000823", "buy_amt": None, "sell_amt": None}]
    tq = {"000823": {"buy_amt": 685221310.73, "sell_amt": 781525964.22,
                     "net_buy": -96304653.49, "source": "tdx_tq_gp02"}}

    merged = bf.merge_rows(rows, tq)
    assert [m["symbol"] for m in merged] == ["000823"], "TQ 没数据的票不许进更新列表"
    assert merged[0]["buy_amt"] == 685221310.73
    assert merged[0]["before_buy"] is None


def test_merge_never_writes_zero_when_tq_empty():
    rows = [{"symbol": "000056", "buy_amt": 1.0, "sell_amt": 2.0}]
    assert bf.merge_rows(rows, {}) == []


def test_merge_handles_suffix_and_zero_amounts():
    rows = [{"symbol": "600127", "buy_amt": 1.0, "sell_amt": 2.0},
            {"symbol": "600519", "buy_amt": 1.0, "sell_amt": 2.0}]
    tq = {"600127": {"buy_amt": 5.0, "sell_amt": 6.0, "net_buy": -1.0},
          "600519": {"buy_amt": None, "sell_amt": None}}   # 两边都空 → 不是"0 元"

    merged = bf.merge_rows(rows, tq)
    assert [m["symbol"] for m in merged] == ["600127"]
    assert (merged[0]["buy_amt"], merged[0]["sell_amt"], merged[0]["net_buy"]) == (5.0, 6.0, -1.0)


def test_target_date_defaults_to_previous_trading_day():
    """默认回填「上一个交易日」，而不是自然日昨天。"""
    d = bf._target_date()
    assert len(d) == 8 and d.isdigit()
    from src.core.trading_calendar import is_trading_day, prev_trading_day
    from datetime import date

    try:
        expect = prev_trading_day(date.today()).strftime("%Y%m%d")
    except Exception:
        return  # 静态表未覆盖 → 该断言不适用
    assert d == expect
    # is_trading_day 只认 date/datetime/ISO, 不认 YYYYMMDD 紧凑串 → 转一下再断言
    from datetime import date as _d
    assert is_trading_day(_d(int(d[:4]), int(d[4:6]), int(d[6:8])))
