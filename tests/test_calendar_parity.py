"""交易日历「双表一致性」门禁(2026-09-18, D5 依赖方向整改配套)。

背景: 8010(forecast)是纯计算服务, 依赖方向只允许 8000→8010 —— `forecast_lib` 里
禁止出现 `import src.*`, 且 `Dockerfile.forecast` 只 COPY `forecast_server.py` +
`forecast_lib/`(镜像内没有 src/)。所以交易日历在 8010 侧**只能自带一份静态表**。

自带一份表就有漂移风险(改了主表忘了附表 → 预测目标日悄悄偏一天)。本文件用逐日比对
把风险钉死: 2024-01-01 ~ 2028-12-31 每一天, 两侧 `is_trading_day` 结果必须完全一致;
任一侧改了表而另一侧没跟, CI 必红(红线: 不允许"两边都回落 weekday"式糊弄)。

不改判定口径: 这里的期望值就是 `src/core/trading_calendar` 的现有行为。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

FROM = date(2024, 1, 1)
TO = date(2028, 12, 31)


def _dates():
    d = FROM
    while d <= TO:
        yield d
        d += timedelta(days=1)


def test_forecast_calendar_matches_main_calendar_day_by_day():
    from forecast_lib import trading_days as ftd
    from src.core import trading_calendar as tc

    mismatches: list[tuple[str, object, object]] = []
    for d in _dates():
        try:
            a: object = ftd.is_trading_day(d)
        except Exception as e:  # noqa: BLE001
            a = f"ERR:{type(e).__name__}"
        try:
            b: object = tc.is_trading_day(d)
        except Exception as e:  # noqa: BLE001
            b = f"ERR:{type(e).__name__}"
        if a != b:
            mismatches.append((d.isoformat(), a, b))
        if len(mismatches) > 20:  # 只报前 20 条, 避免刷屏
            break

    assert mismatches == [], (
        "forecast_lib 与 src.core 的交易日历不一致(改表必须两边同步): "
        f"{mismatches[:20]}"
    )


def test_forecast_next_trading_days_matches_main_equivalent():
    """`next_trading_days` 的第一个日期必须与主应用口径一致(预测目标日就取这个)。"""
    from forecast_lib import trading_days as ftd
    from src.core import trading_calendar as tc

    for start in (date(2026, 9, 30), date(2026, 10, 1), date(2027, 2, 5), date(2025, 5, 1)):
        got = ftd.next_trading_days(start, 3)
        # 主应用口径: 从 start 次日起逐日取交易日
        exp: list[date] = []
        cur = start + timedelta(days=1)
        while len(exp) < 3:
            if tc.is_trading_day(cur):
                exp.append(cur)
            cur += timedelta(days=1)
        assert got == exp, f"{start}: forecast={got} main={exp}"


def test_forecast_calendar_raises_outside_covered_years():
    """表外年份必须显式抛错(与主应用同口径), 不允许静默当成"非交易日"。"""
    from forecast_lib import trading_days as ftd

    with pytest.raises(ftd.TradingCalendarError):
        ftd.is_trading_day(date(2031, 1, 6))
