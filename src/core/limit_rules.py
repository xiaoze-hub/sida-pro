"""A 股涨跌停价规则(批次A/批次B共用, 2026-09-06 28号)。

交易所口径(四舍五入到分):
- 主板(60/00): ±10%
- 创业板(30)/科创板(688/689): ±20%
- ST/*ST: ±5%(需名称判断, 代码无法判 → is_st 由调用方传)
- 北交所(43/83/87/92): ±30%
- 新股上市首日/除权日按交易所规则特殊处理(此处 prev_close=None → 返回 None,
  首日无涨跌幅限制的情形由调用方标注)。

诚实标注: ST 判断依赖股票名称, 采样任务拿不到名称时按非 ST 处理,
误判只影响 is_sealed 标记(封板成功率), 不影响撤单率主线指标; 周一盘中实测校准。
"""
from __future__ import annotations


def _round_cent(x: float) -> float:
    """四舍五入到分(不用 round(): 银行家舍入+浮点误差, 如 round(2.675,2)=2.67;
    交易所口径是普通四舍五入 → 2.68)。"""
    return int(x * 100 + 0.5) / 100


def limit_ratio(symbol6: str, is_st: bool | None = False) -> float | None:
    """按代码段返回涨跌停幅度(0.10/0.20/0.30/0.05)。无法识别 → None。"""
    code = (symbol6 or "").strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(("43", "83", "87", "92")):
        return 0.30
    if code.startswith(("30", "688", "689")):
        return 0.20
    if code.startswith(("60", "00")):
        return 0.05 if is_st else 0.10
    return None


def limit_up_price(symbol6: str, prev_close: float | None, is_st: bool | None = False) -> float | None:
    """昨收 → 涨停价。prev_close 缺失/非正(新股首日/数据缺失) → None(显式无数据)。"""
    ratio = limit_ratio(symbol6, is_st)
    if ratio is None or not prev_close or prev_close <= 0:
        return None
    return _round_cent(prev_close * (1 + ratio))


def limit_down_price(symbol6: str, prev_close: float | None, is_st: bool | None = False) -> float | None:
    ratio = limit_ratio(symbol6, is_st)
    if ratio is None or not prev_close or prev_close <= 0:
        return None
    return _round_cent(prev_close * (1 - ratio))
