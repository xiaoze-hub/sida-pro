"""S6 (2026-08-23): A 股交易日历工具单元测试

覆盖:
- is_trading_day: 周末 / 法定节假日 / 调休补班 / 普通工作日
- add_trading_days: 跨春节 / 跨国庆 / 跨周末
- next/prev_trading_day: 边界 + 跳过节假日
- trading_days_between: 区间计数(含端点 / 不含端点)
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.trading_calendar import (
    add_trading_days,
    is_trading_day,
    next_trading_day,
    prev_trading_day,
    trading_days_between,
)


class TestIsTradingDay:
    """is_trading_day 判定规则"""

    def test_normal_weekday_is_trading_day(self):
        """普通工作日(无节假日) = 交易日"""
        assert is_trading_day("2025-03-12") is True  # 周三

    def test_weekend_not_trading(self):
        """周六周日 = 休市"""
        assert is_trading_day("2025-03-08") is False  # 周六
        assert is_trading_day("2025-03-09") is False  # 周日

    def test_spring_festival_holiday(self):
        """春节 2025: 1/28 除夕 ~ 2/4 初七 全部休市"""
        for d in ("2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
                  "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04"):
            assert is_trading_day(d) is False, f"{d} 应该休市"

    def test_spring_festival_makeup_workday(self):
        """春节调休补班: 2025-01-26 周日 / 2025-02-08 周六 变为交易日"""
        assert is_trading_day("2025-01-26") is True
        assert is_trading_day("2025-02-08") is True

    def test_national_day_holiday(self):
        """国庆 2025: 10/1 ~ 10/8 全部休市"""
        for d in ("2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04",
                  "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08"):
            assert is_trading_day(d) is False, f"{d} 应该休市"

    def test_national_day_makeup_workday(self):
        """国庆调休补班: 2025-09-28 周日 / 2025-10-11 周六 变为交易日"""
        assert is_trading_day("2025-09-28") is True
        assert is_trading_day("2025-10-11") is True

    def test_labor_day_2026(self):
        """劳动节 2026: 5/1~5/5"""
        for d in ("2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05"):
            assert is_trading_day(d) is False

    def test_holiday_weekday_override(self):
        """法定节假日覆盖 weekday 判定: 周一如果是节假日, 也判定为非交易日"""
        # 2025-04-07 是周一(清明假最后一天已结束, 实际是工作日; 选 2026-04-06 周一)
        assert is_trading_day("2026-04-06") is False  # 周一, 清明假期内

    def test_makeup_weekend_override(self):
        """调休补班覆盖周末判定: 周六周日如果是补班日, 判定为交易日"""
        # 2025-10-11 周六, 国庆调休补班
        assert is_trading_day("2025-10-11") is True

    def test_date_object_input(self):
        """支持 date 对象输入"""
        d = date(2025, 10, 1)
        assert is_trading_day(d) is False

    def test_datetime_object_input(self):
        """支持 datetime 对象输入"""
        from datetime import datetime
        d = datetime(2025, 3, 12, 10, 30)
        assert is_trading_day(d) is True

    def test_invalid_string(self):
        """非法字符串 → False(不抛异常)"""
        assert is_trading_day("not-a-date") is False
        assert is_trading_day("") is False


class TestAddTradingDays:
    """add_trading_days 跨节假日计数"""

    def test_zero_or_negative_returns_start(self):
        """n<=0 → 返回 start 本身"""
        assert add_trading_days("2025-03-12", 0) == date(2025, 3, 12)
        assert add_trading_days("2025-03-12", -3) == date(2025, 3, 12)

    def test_one_trading_day_skip_weekend(self):
        """加 1 个交易日: 跳过周末"""
        # 2025-03-13(周四) + 1 = 2025-03-14(周五)
        assert add_trading_days("2025-03-13", 1) == date(2025, 3, 14)
        # 2025-03-14(周五) + 1 = 2025-03-17(周一, 跳过周末)
        assert add_trading_days("2025-03-14", 1) == date(2025, 3, 17)

    def test_cross_spring_festival(self):
        """跨春节: 加 5 个交易日不会落到春节假内"""
        # 2025-01-23 周四(春节前最后一个工作日)+ 5 个交易日
        # 1/24(周五) 1/27(周一) → 春节假从 1/28 开始 → 应该跳过
        result = add_trading_days("2025-01-23", 5)
        # 预期: 1/24, 1/27 然后跳到 2/5(补班后首个) 1/26(补班) → 1/26 + 3 个 → 2/10
        # 验证: result 一定是 date 且不为春节假内
        assert isinstance(result, date)
        assert is_trading_day(result) is True
        assert result > date(2025, 2, 4)  # 春节假已经结束

    def test_cross_national_day(self):
        """跨国庆: 加 10 个交易日不会落到国庆假内"""
        # 2025-09-26 周五(国庆前最后一个工作日)+ 10 个交易日
        result = add_trading_days("2025-09-26", 10)
        assert isinstance(result, date)
        assert is_trading_day(result) is True
        assert result > date(2025, 10, 8)  # 国庆假已结束

    def test_count_matches_is_trading_day(self):
        """add_trading_days 的结果 = start + N 个 is_trading_day=True 的日期"""
        start = date(2025, 9, 15)  # 周一
        n = 10
        end = add_trading_days(start, n)
        cnt = 0
        cur = start
        while cur < end:
            cur = cur.replace(day=cur.day)  # 桩
            from datetime import timedelta
            cur = cur + timedelta(days=1)
            if is_trading_day(cur):
                cnt += 1
        assert cnt == n

    def test_accepts_datetime_input(self):
        """支持 datetime 输入"""
        from datetime import datetime
        start = datetime(2025, 3, 12, 10, 0)
        result = add_trading_days(start, 1)
        assert result == date(2025, 3, 13)


class TestNextPrevTradingDay:
    """next_trading_day / prev_trading_day"""

    def test_next_from_weekday(self):
        """工作日的 next → 下一个工作日"""
        # 2025-03-12(周三) → 2025-03-13(周四)
        assert next_trading_day("2025-03-12") == date(2025, 3, 13)

    def test_next_from_friday(self):
        """周五的 next → 跳过周末"""
        # 2025-03-14(周五) → 2025-03-17(周一)
        assert next_trading_day("2025-03-14") == date(2025, 3, 17)

    def test_next_from_holiday(self):
        """节假日 next → 跳过节假日的下一个工作日"""
        # 2025-10-01(国庆) → 2025-10-09(周四)
        assert next_trading_day("2025-10-01") == date(2025, 10, 9)

    def test_prev_from_weekday(self):
        """工作日的 prev → 前一个工作日"""
        # 2025-03-12(周三) → 2025-03-11(周二)
        assert prev_trading_day("2025-03-12") == date(2025, 3, 11)

    def test_prev_from_monday(self):
        """周一的 prev → 跳过周末"""
        # 2025-03-17(周一) → 2025-03-14(周五)
        assert prev_trading_day("2025-03-17") == date(2025, 3, 14)

    def test_prev_from_holiday(self):
        """节假日 prev → 跳过节假日前一个工作日"""
        # 2025-10-08(国庆最后一天) → 2025-09-30(周二)
        assert prev_trading_day("2025-10-08") == date(2025, 9, 30)


class TestTradingDaysBetween:
    """trading_days_between 区间计数"""

    def test_same_day_zero(self):
        """start == end → 0"""
        assert trading_days_between("2025-03-12", "2025-03-12") == 0

    def test_one_week_full(self):
        """整周(无节假日): 不含 end 端点语义 → 4 个交易日"""
        # 2025-03-10 周一(不含) ~ 2025-03-15 周六(不含) = 3/11~3/14 = 4 天
        assert trading_days_between("2025-03-10", "2025-03-15") == 4
        assert trading_days_between("2025-03-10", "2025-03-15", inclusive=True) == 4  # 周六不算交易日
        # 含 end 端点: (3/10, 3/14] = 3/11~3/14, 周五 3/14 是交易日 → 4 个
        assert trading_days_between("2025-03-10", "2025-03-14", inclusive=True) == 4

    def test_cross_weekend(self):
        """跨周末 = 4 个交易日(不含 end 周一)"""
        # (3/10, 3/17) 开区间 = 3/11~3/14, 跳过 3/15~3/16 周末
        assert trading_days_between("2025-03-10", "2025-03-17") == 4

    def test_cross_spring_festival(self):
        """跨春节(2025-01-28~02-04)"""
        # 2025-01-23 周四 ~ 2025-02-10 周一
        # 1/24(五) 1/27(一) → 2 个交易日 + 春节假 + 2/5(二) 2/6(三) 2/7(四) 2/8(六补) → 7 个交易日
        # 总共 9 个, 但 2/10 是周一, 不含端点 → 7 个
        cnt = trading_days_between("2025-01-23", "2025-02-10")
        assert cnt == 7

    def test_inclusive_end(self):
        """inclusive=True 含 end 当天"""
        # (3/10, 3/14] = 3/11~3/14, 周五 3/14 是交易日 → 4 个
        assert trading_days_between("2025-03-10", "2025-03-14", inclusive=True) == 4
        # 2025-03-15 周六 inclusive: end 是周六非交易日, 计数不变 = 4
        assert trading_days_between("2025-03-10", "2025-03-15", inclusive=True) == 4

    def test_reverse_range_zero(self):
        """end <= start → 0"""
        assert trading_days_between("2025-03-14", "2025-03-10") == 0


class TestPredictionOutcomeAddTradingDays:
    """S6 集成: prediction_outcome._add_trading_days 应使用交易日历工具"""

    def test_uses_calendar_not_weekday(self):
        """prediction_outcome 内的 _add_trading_days 与 trading_calendar.add_trading_days 一致。

        S6 关键修复: 旧实现走 weekday()<5 会把 9/27(六) 9/28(日) 9/29(一) 全算交易日,
        但 9/29 已是国庆假; 新实现走交易日历, 跳过假日后第一个交易日应是 9/30。
        """
        from src.core.prediction_outcome import _add_trading_days

        start = date(2025, 9, 26)  # 国庆前最后工作日(周五)
        result = _add_trading_days(start, 3)
        # 必须不在国庆假内
        assert is_trading_day(result) is True
        # 2025-09-26 起算: 9/28(日补班) 9/29(一) 9/30(二) → 第 3 个交易日 = 9/30
        assert result == date(2025, 9, 30)
