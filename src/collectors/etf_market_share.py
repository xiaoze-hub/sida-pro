"""市场级 ETF 日度份额替代源（沪/深交易所官方口径，**只读 dry-run**）。

## 这个模块解决什么

TQ 的 `SC8` 序列（`src/collectors/tq_etf_watch.py`）在 2026-10-16 之后停更，
需要一条不依赖 TQ、不依赖付费 key 的全市场 ETF 份额日度来源。
本模块直接读**沪深交易所官方披露**：

| 市场 | 端点 | 粒度 | 源字段 | 源单位 | 换算 |
|---|---|---|---|---|---|
| 上交所 | `query.sse.com.cn/commonQuery.do`<br>`sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L` | 按 `STAT_DATE` 查任意历史日 | `TOT_VOL` | **万份** | ×1e4 → 份 |
| 深交所 | `www.szse.cn/api/report/ShowReport`<br>`CATALOGID=scsj_fund_jjgm`（xlsx） | 区间 ≤6 个月/请求 | `基金规模(份)` | **份** | 原值 |

## 三条硬口径（写死在代码里，测试钉子钉住）

1. **单位是份额**。本模块**只产出份额（份）**，不乘净值、不产出任何"金额/市值"字段，
   也不把份额改单位叫金额。份额 × 净值是另一个口径，必须由调用方显式做并另行标注。
2. **全市场，不是宽基篮子**。模块只按"交易所返回的全部 ETF"聚合；
   不接受、也没有任何"20 只重点宽基"入口——那 20 只 2026-10-16 只占全市场 15.5%，
   拿它冒充全市场属于虚报（见 `docs`/证据报告）。
3. **缺失不补零**。某日源返回 0 行、真实日期与请求日不符、同一代码重复且份额冲突，
   一律标成 gap（`usable=False` + `gap_reason`），**不写 0、不外推、不用别的源偷偷填**。

## 上市/终止的处理（这是最容易算错的地方）

`Σ份额` 的直接差分会把**新发行上市**当成净申购、把**终止/摘牌**当成净赎回。
所以本模块把一日对差分成三段：

```
全市场份额差 (naive) = 可比口径净申购 + 新上市份额 - 终止份额
                        ↑ 只对两日都存在的代码求 Δ   ↑ 计入但单列   ↑ 计入但单列
```

`diff_shares()` 会**同时**给出 `comparable_net_sub_shares`（可比口径净申购）与
`naive_total_delta_shares`（朴素总额差），并断言
`comparable + new - gone == naive`（`identity_ok`）。调用方必须自己决定展示哪个，
不能只拿一个数当"净申购"。实测样例（2026-10-16 → 2026-11-09）：
深交所新增 11 只（合计 8.79 亿份）、上交所终止 1 只（512390，0.10 亿份）——
若直接差分会把 8.79 亿份一级发行算成二级净申购。

## 已知覆盖缺口（不外推、必须随数一起报）

- 沪市货币 ETF（511990 华宝添益、511880 银华日利、511660 建信）**不在上交所份额接口返回里**，
  实测确认（`511990`/`511880` 查 2026-10-16 结果 `in SSE = False`）。
  由东财 `f20/f2` 反推约 10.58 + 11.97 + 0.84 = 23.39 亿份，与对拍缺口 −23.16 亿份同量级
  ——**这是推断，不是证明**，`gap_estimate` 里标 `inferred=True`。
- 两个交易所都是 **T+1** 发布，没有盘中；请求当日拿不到当日份额（返回空 → 记 gap）。
- 深交所单请求 ≤6 个月，更早需要多窗口回拼（`fetch_szse_window` 由调用方按窗口切）。

## 只读

本模块**不 import 任何数据库/Web 层**，没有任何写库/写文件路径。
`dry_run()` 返回可 JSON 序列化的 dict，CLI 只打印。要落库是后续独立改动
（需要新列/新迁移，本模块故意不提供 write 入口）。

用法（真实接口，只读）：

```bash
python -m src.collectors.etf_market_share --prev 2026-11-06 --date 2026-11-09
```
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────

MARKET_SSE = "SSE"
MARKET_SZSE = "SZSE"
MARKET_LABELS = {MARKET_SSE: "上交所", MARKET_SZSE: "深交所"}

#: 唯一允许的单位。所有对外字段的份额都以它为准。
UNIT_SHARES = "份"

#: 上交所 `TOT_VOL` 是万份，需要 ×1e4 才是份。这个常量是"单位声明"，不是魔法数。
SSE_TOT_VOL_UNIT = "万份"
SSE_TOT_VOL_TO_SHARES = 1e4

SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_SQLID = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
SSE_HEADERS = {
    "Referer": "https://www.sse.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
}

SZSE_URL = "https://www.szse.cn/api/report/ShowReport"
SZSE_CATALOGID = "scsj_fund_jjgm"
SZSE_HEADERS = {
    "Referer": "https://www.szse.cn/market/fund/volume/etf/index.html",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
}
#: 深交所单请求最大跨度（源侧限制），超过会返回空表。
SZSE_MAX_WINDOW_DAYS = 183

#: 沪市货币 ETF：交易所份额接口不返回，属已确认覆盖缺口（只用于报告，不参与求和）。
SSE_MONEY_ETF_GAP_CODES = ("511990", "511880", "511660")


# ── 数据结构 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EtfShareRow:
    """一只 ETF 在某交易日的总份额（份）。"""

    code: str
    name: str
    shares: float
    market: str
    etf_type: str = ""


@dataclass
class SourceDay:
    """单个市场某交易日的份额快照 + 全部质量标记。

    `usable=False` 时 `rows` 可能仍非空（例如日期不符），**但调用方不得据此求和**。
    """

    market: str
    requested_date: str
    data_date: str | None = None
    rows: tuple[EtfShareRow, ...] = ()
    raw_row_count: int = 0
    duplicate_codes: tuple[str, ...] = ()
    conflicting_duplicates: tuple[str, ...] = ()
    dropped_no_value: int = 0
    dropped_bad_value: int = 0
    unit_source: str = ""
    unit_note: str = ""
    date_mismatch: bool = False
    usable: bool = True
    gap_reason: str | None = None
    fetch_error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def n_funds(self) -> int:
        return len(self.rows)

    @property
    def sum_shares(self) -> float:
        """Σ份额（份）。不可用时返回 0.0，但必须同时看 `usable`。"""
        return math.fsum(r.shares for r in self.rows)

    @property
    def sum_shares_yi(self) -> float:
        """Σ份额（亿份）——仅为可读，字段名带 `_yi_shares` 语义，**不是金额**。"""
        return self.sum_shares / 1e8

    def by_code(self) -> dict[str, EtfShareRow]:
        return {r.code: r for r in self.rows}


#: 单只 ETF 单日份额变动超过该比例 → 标为"跳变"，单列不并入结论。
#: 目的：份额**折算/拆分**会让份额翻倍，直接算 Δ 会被当成巨额净申购。
#: 实测样例：159335 央企科创ETF融通 2026-10-15→10-16 份额 +88.7%。
#: 阈值只是"要不要人看一眼"的筛子，**不代表**已判定为拆分。
JUMP_RATIO_THRESHOLD = 0.5


@dataclass
class ShareDiff:
    """两个交易日之间、**同一市场**的份额变动拆分（上市/终止/跳变单列）。"""

    market: str
    prev_date: str | None
    cur_date: str | None
    prev_n: int = 0
    cur_n: int = 0
    comparable_n: int = 0
    comparable_net_sub_shares: float = 0.0
    new_codes: tuple[str, ...] = ()
    new_shares: float = 0.0
    gone_codes: tuple[str, ...] = ()
    gone_shares: float = 0.0
    naive_total_delta_shares: float = 0.0
    jump_codes: tuple[str, ...] = ()
    jump_shares: float = 0.0
    jump_threshold: float = JUMP_RATIO_THRESHOLD
    identity_ok: bool = False
    usable: bool = True
    reason: str | None = None

    @property
    def comparable_ex_jump_net_sub_shares(self) -> float:
        """剔除跳变标的后的可比净申购（更保守，但仍可能含真实大额申购）。"""
        return self.comparable_net_sub_shares - self.jump_shares

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "market_label": MARKET_LABELS.get(self.market, self.market),
            "prev_date": self.prev_date,
            "cur_date": self.cur_date,
            "prev_n": self.prev_n,
            "cur_n": self.cur_n,
            "comparable_n": self.comparable_n,
            "comparable_net_sub_shares": round(self.comparable_net_sub_shares, 2),
            "comparable_net_sub_yi_shares": round(self.comparable_net_sub_shares / 1e8, 4),
            "new_listed_codes": list(self.new_codes),
            "new_listed_shares": round(self.new_shares, 2),
            "terminated_codes": list(self.gone_codes),
            "terminated_shares": round(self.gone_shares, 2),
            "naive_total_delta_shares": round(self.naive_total_delta_shares, 2),
            "jump_threshold": self.jump_threshold,
            "jump_codes": list(self.jump_codes),
            "jump_shares": round(self.jump_shares, 2),
            "jump_note": "单日份额变动超阈值的标的，可能是大额申购也可能是份额折算，未核实",
            "comparable_ex_jump_net_sub_shares": round(self.comparable_ex_jump_net_sub_shares, 2),
            "comparable_ex_jump_net_sub_yi_shares": round(
                self.comparable_ex_jump_net_sub_shares / 1e8, 4
            ),
            "identity_ok": self.identity_ok,
            "usable": self.usable,
            "reason": self.reason,
            "unit": UNIT_SHARES,
        }


def _norm_code(raw: Any) -> str:
    """统一证券代码为 6 位字符串。

    深交所 xlsx 里 `基金代码` 是**整数**（158000），证据 JSON 里也可能是 int；
    若不补齐，跨日集合比较会 100% 误判为"全部新增 + 全部终止"。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if s.isdigit():
        return s.zfill(6)
    return s


def _parse_float(raw: Any) -> float | None:
    """严格解析份额数值；空串/None/非数字/负数/NaN 一律返回 None（不补零）。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
    else:
        s = str(raw).strip().replace(",", "")
        if not s or s in {"-", "--", "—"}:
            return None
        try:
            v = float(s)
        except (TypeError, ValueError):
            return None
    if math.isnan(v) or math.isinf(v):
        return None
    if v < 0:
        return None
    return v


def _norm_date(raw: Any) -> str | None:
    """把 `2026-11-09` / `20261109` / date / datetime 归一成 `YYYY-MM-DD`。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, _date):
        return raw.isoformat()
    s = str(raw).strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return None


# ── 解析（纯函数，无 IO，测试直接打这里）────────────────────────────────


def parse_sse_payload(payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
                      requested_date: str) -> SourceDay:
    """解析上交所 `commonQuery.do` 响应为 `SourceDay`。

    - `TOT_VOL` 单位**万份** → ×1e4 转**份**；
    - 空/非法 `TOT_VOL` 丢弃并计数（不补零）；
    - 同代码重复：份额相同 → 去重并记录；份额不同 → `usable=False`（无法判断哪个对）。
    """
    rows_raw: Sequence[Mapping[str, Any]]
    if isinstance(payload, Mapping):
        rows_raw = payload.get("result") or []
        page = payload.get("pageHelp") or {}
        total = page.get("total") if isinstance(page, Mapping) else None
    else:
        rows_raw = payload or []
        total = None

    day = SourceDay(
        market=MARKET_SSE,
        requested_date=requested_date,
        unit_source=f"TOT_VOL（{SSE_TOT_VOL_UNIT}）",
        unit_note=f"TOT_VOL × {SSE_TOT_VOL_TO_SHARES:.0f} → {UNIT_SHARES}",
        raw_row_count=len(rows_raw),
    )

    if not rows_raw:
        day.usable = False
        day.gap_reason = "empty_result"  # 非交易日 / 未发布 / 停更，一律不当 0
        day.warnings.append("源对当日返回 0 行：可能是非交易日，也可能是尚未发布，无法区分")
        return day

    by_code: dict[str, EtfShareRow] = {}
    dups: list[str] = []
    conflicts: list[str] = []
    seen_values: dict[str, float] = {}
    seen_dates: set[str] = set()

    for item in rows_raw:
        if not isinstance(item, Mapping):
            day.dropped_bad_value += 1
            continue
        code = _norm_code(item.get("SEC_CODE"))
        if not code:
            day.dropped_bad_value += 1
            continue
        d = _norm_date(item.get("STAT_DATE"))
        if d:
            seen_dates.add(d)
        vol = _parse_float(item.get("TOT_VOL"))
        if vol is None:
            day.dropped_no_value += 1
            continue
        shares = vol * SSE_TOT_VOL_TO_SHARES
        if code in seen_values:
            dups.append(code)
            if not math.isclose(seen_values[code], shares, rel_tol=1e-9, abs_tol=1e-6):
                conflicts.append(code)
                continue
            continue  # 完全重复，去重
        seen_values[code] = shares
        by_code[code] = EtfShareRow(
            code=code,
            name=str(item.get("SEC_NAME") or "").strip(),
            shares=shares,
            market=MARKET_SSE,
            etf_type=str(item.get("ETF_TYPE") or "").strip(),
        )

    day.rows = tuple(by_code[k] for k in sorted(by_code))
    day.duplicate_codes = tuple(sorted(set(dups)))
    day.conflicting_duplicates = tuple(sorted(set(conflicts)))

    if len(seen_dates) == 1:
        day.data_date = next(iter(seen_dates))
    elif len(seen_dates) > 1:
        day.data_date = sorted(seen_dates)[-1]
        day.warnings.append(f"源返回多个 STAT_DATE: {sorted(seen_dates)}")
    else:
        day.data_date = None

    if day.data_date and day.data_date != requested_date:
        # 源把别的日期当请求日返回 → 不能当成请求日使用
        day.date_mismatch = True
        day.usable = False
        day.gap_reason = f"date_mismatch(请求 {requested_date}, 源内 {day.data_date})"

    if conflicts:
        day.usable = False
        day.gap_reason = f"duplicate_conflict({len(conflicts)} 只代码份额不一致)"

    if not by_code:
        day.usable = False
        day.gap_reason = day.gap_reason or "no_valid_rows"

    if total is not None and isinstance(total, (int, float)) and total > 0:
        if int(total) != day.raw_row_count:
            # 分页没取全：Σ 会低报，必须暴露而不是静默
            day.warnings.append(f"分页未取全: total={total} 实收={day.raw_row_count}")
            day.usable = False
            day.gap_reason = day.gap_reason or f"pagination_incomplete(total={total})"

    return day


def parse_szse_records(records: Iterable[Mapping[str, Any]],
                       requested_date: str) -> SourceDay:
    """解析深交所 `scsj_fund_jjgm` xlsx 行（`基金规模(份)`，单位**份**，无需换算）。"""
    day = SourceDay(
        market=MARKET_SZSE,
        requested_date=requested_date,
        unit_source="基金规模(份)",
        unit_note=f"原值即 {UNIT_SHARES}（无换算）",
    )

    by_code: dict[str, EtfShareRow] = {}
    dups: list[str] = []
    conflicts: list[str] = []
    seen_values: dict[str, float] = {}
    seen_dates: set[str] = set()
    n = 0

    for rec in records or ():
        if not isinstance(rec, Mapping):
            day.dropped_bad_value += 1
            continue
        n += 1
        code = _norm_code(rec.get("基金代码") if "基金代码" in rec else rec.get("code"))
        if not code:
            day.dropped_bad_value += 1
            continue
        d = _norm_date(rec.get("日期") if "日期" in rec else rec.get("date"))
        if d:
            seen_dates.add(d)
            if d != requested_date:
                continue  # 区间接口会带多日，只取目标日
        raw = rec.get("基金规模(份)")
        if raw is None:
            raw = rec.get("基金份额", rec.get("shares"))
        shares = _parse_float(raw)
        if shares is None:
            day.dropped_no_value += 1
            continue
        if code in seen_values:
            dups.append(code)
            if not math.isclose(seen_values[code], shares, rel_tol=1e-9, abs_tol=1e-6):
                conflicts.append(code)
                continue
            continue
        seen_values[code] = shares
        by_code[code] = EtfShareRow(
            code=code,
            name=str(rec.get("基金简称") or rec.get("name") or "").strip(),
            shares=shares,
            market=MARKET_SZSE,
        )

    day.raw_row_count = n
    day.rows = tuple(by_code[k] for k in sorted(by_code))
    day.duplicate_codes = tuple(sorted(set(dups)))
    day.conflicting_duplicates = tuple(sorted(set(conflicts)))
    day.data_date = sorted(seen_dates)[-1] if seen_dates else None

    if not by_code:
        day.usable = False
        day.gap_reason = day.gap_reason or "empty_result"
        day.warnings.append("源对该日无有效行：可能是非交易日，也可能是尚未发布，无法区分")
    if seen_dates and requested_date not in seen_dates:
        day.date_mismatch = True
        day.usable = False
        day.gap_reason = f"date_mismatch(请求 {requested_date}, 源内 {sorted(seen_dates)[-1]})"
    if conflicts:
        day.usable = False
        day.gap_reason = f"duplicate_conflict({len(conflicts)} 只代码份额不一致)"

    return day


# ── 对差（上市/终止单列）────────────────────────────────────────────────


def diff_shares(prev: SourceDay | None, cur: SourceDay | None) -> ShareDiff:
    """两个交易日同市场份额变动，**可比口径与总额差分开**。

    恒等式：`comparable_net_sub + new_shares - gone_shares == naive_total_delta`
    """
    if prev is None or cur is None:
        return ShareDiff(
            market=(cur or prev).market if (cur or prev) else "?",
            prev_date=prev.data_date if prev else None,
            cur_date=cur.data_date if cur else None,
            usable=False,
            reason="missing_side",
        )

    d = ShareDiff(
        market=cur.market,
        prev_date=prev.data_date,
        cur_date=cur.data_date,
        prev_n=prev.n_funds,
        cur_n=cur.n_funds,
    )

    if not prev.usable or not cur.usable:
        d.usable = False
        d.reason = f"source_unusable(prev={prev.gap_reason}, cur={cur.gap_reason})"

    p = prev.by_code()
    c = cur.by_code()
    common = sorted(set(p) & set(c))
    new_codes = sorted(set(c) - set(p))
    gone_codes = sorted(set(p) - set(c))

    d.comparable_n = len(common)
    d.comparable_net_sub_shares = math.fsum(c[k].shares - p[k].shares for k in common)
    d.new_codes = tuple(new_codes)
    d.new_shares = math.fsum(c[k].shares for k in new_codes)
    d.gone_codes = tuple(gone_codes)
    d.gone_shares = math.fsum(p[k].shares for k in gone_codes)
    d.naive_total_delta_shares = cur.sum_shares - prev.sum_shares

    # 跳变（疑似份额折算/拆分，或是超大额申购）：单列，不进结论，也不擅自归类
    jump = []
    for k in common:
        pv = p[k].shares
        if pv <= 0:
            continue
        ratio = (c[k].shares - pv) / pv
        if abs(ratio) > JUMP_RATIO_THRESHOLD:
            jump.append(k)
    d.jump_codes = tuple(sorted(jump))
    d.jump_shares = math.fsum(c[k].shares - p[k].shares for k in jump)

    d.identity_ok = math.isclose(
        d.comparable_net_sub_shares + d.new_shares - d.gone_shares,
        d.naive_total_delta_shares,
        rel_tol=1e-9,
        abs_tol=1e-3,
    )
    if not d.identity_ok:
        d.usable = False
        d.reason = (d.reason + "; " if d.reason else "") + "identity_broken"
    return d


# ── 市场聚合 ──────────────────────────────────────────────────────────────


def _market_day(day: SourceDay) -> dict[str, Any]:
    return {
        "market": day.market,
        "market_label": MARKET_LABELS.get(day.market, day.market),
        "requested_date": day.requested_date,
        "data_date": day.data_date,
        "n_funds": day.n_funds,
        "sum_shares": round(day.sum_shares, 2),
        "sum_yi_shares": round(day.sum_shares_yi, 4),
        "unit": UNIT_SHARES,
        "unit_source": day.unit_source,
        "unit_note": day.unit_note,
        "usable": day.usable,
        "gap_reason": day.gap_reason,
        "date_mismatch": day.date_mismatch,
        "raw_row_count": day.raw_row_count,
        "duplicate_codes": list(day.duplicate_codes),
        "conflicting_duplicates": list(day.conflicting_duplicates),
        "dropped_no_value": day.dropped_no_value,
        "dropped_bad_value": day.dropped_bad_value,
        "warnings": list(day.warnings),
        "fetch_error": day.fetch_error,
    }


def build_dry_run(
    days: Mapping[str, Sequence[SourceDay]],
    prev_days: Mapping[str, Sequence[SourceDay]] | None = None,
    *,
    tq_sc8_reference: Mapping[str, Any] | None = None,
    money_etf_gap: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把 `{date: [SourceDay...]}` 汇总成一份可直接 JSON 的只读报告。

    - 只按"交易所返回的全部 ETF"聚合，**没有**宽基篮子入口；
    - 任一市场当日不可用 → 全市场合计标记 `partial=True`，不假装完整；
    - `tq_sc8_reference` / `money_etf_gap` 只作为**外部证据**透传，不参与求和。
    """
    date_keys = sorted(days)
    current_date = date_keys[-1] if date_keys else None
    prev_date = sorted(prev_days)[-1] if prev_days else None

    markets: dict[str, dict[str, Any]] = {}
    diffs: dict[str, dict[str, Any]] = {}

    for market in (MARKET_SSE, MARKET_SZSE):
        cur = next((d for d in (days.get(current_date, ()) if current_date else ())
                    if d.market == market), None)
        prev = None
        if prev_days and prev_date:
            prev = next((d for d in prev_days.get(prev_date, ()) if d.market == market), None)
        if cur is not None:
            markets[market] = _market_day(cur)
        if cur is not None and prev is not None:
            diffs[market] = diff_shares(prev, cur).to_dict()

    usable_markets = [m for m, v in markets.items() if v.get("usable")]
    gap_markets = {m: v.get("gap_reason") for m, v in markets.items() if not v.get("usable")}
    total_usable = bool(markets) and len(usable_markets) == len(markets) and not gap_markets
    total_shares = math.fsum(markets[k]["sum_shares"] for k in usable_markets)

    comparable_sum = math.fsum(
        d["comparable_net_sub_shares"] for d in diffs.values() if d.get("usable")
    )
    naive_sum = math.fsum(
        d["naive_total_delta_shares"] for d in diffs.values() if d.get("usable")
    )
    ex_jump_sum = math.fsum(
        d["comparable_ex_jump_net_sub_shares"] for d in diffs.values() if d.get("usable")
    )
    jump_all = sorted({c for d in diffs.values() for c in d.get("jump_codes", [])})

    return {
        "kind": "etf_market_share_dry_run",
        "read_only": True,
        "wrote_database": False,
        "as_of_requested": current_date,
        "prev_requested": prev_date,
        "unit": UNIT_SHARES,
        "amount_fields_present": False,   # 显式声明：本报告没有金额/市值
        "basket_proxy_used": False,       # 显式声明：不是 20 只宽基
        "markets": markets,
        "market_total": {
            "n_funds": sum(markets[k]["n_funds"] for k in usable_markets),
            "sum_shares": round(total_shares, 2),
            "sum_yi_shares": round(total_shares / 1e8, 4),
            "unit": UNIT_SHARES,
            "complete": bool(total_usable),
            "missing_markets": gap_markets,
            "note": "仅含可用市场合计；不可用市场未计入且未补零" if not total_usable else "",
        },
        "share_change": {
            "comparable_net_sub_shares": round(comparable_sum, 2),
            "comparable_net_sub_yi_shares": round(comparable_sum / 1e8, 4),
            "comparable_ex_jump_net_sub_shares": round(ex_jump_sum, 2),
            "comparable_ex_jump_net_sub_yi_shares": round(ex_jump_sum / 1e8, 4),
            "jump_threshold": JUMP_RATIO_THRESHOLD,
            "jump_codes": jump_all,
            "naive_total_delta_shares": round(naive_sum, 2),
            "naive_total_delta_yi_shares": round(naive_sum / 1e8, 4),
            "by_market": diffs,
            "unit": UNIT_SHARES,
            "note": (
                "comparable_net_sub 只对两日都存在的代码求 Δ，已剔除新上市/终止；"
                "comparable_ex_jump 再剔除单日变动超阈值的标的（疑似份额折算）；"
                "naive_total_delta 是总额直差，含一级发行与摘牌，不等于净申购。"
            ),
        },
        "known_gaps": {
            "sse_money_etf": money_etf_gap
            or {
                "codes": list(SSE_MONEY_ETF_GAP_CODES),
                "reason": "上交所 ETF 份额接口不返回货币 ETF",
                "inferred": True,
                "note": "未量化，需单独补源",
            }
        },
        "tq_sc8_reference": tq_sc8_reference or {"available": False},
        "verdict": _verdict(markets, diffs, total_usable),
    }


def _verdict(markets: Mapping[str, Mapping[str, Any]],
             diffs: Mapping[str, Mapping[str, Any]],
             total_usable: bool) -> dict[str, Any]:
    """给出「可否用于产品」的机器可读判断——只描述取数本身，不外推。"""
    blockers: list[str] = []
    if not markets:
        blockers.append("no_market_data")
    for m, v in markets.items():
        if not v.get("usable"):
            blockers.append(f"{m}:{v.get('gap_reason')}")
    for m, v in diffs.items():
        if not v.get("usable"):
            blockers.append(f"{m}_diff:{v.get('reason')}")

    if not markets:
        status = "unusable"
    elif blockers:
        status = "degraded"
    else:
        status = "usable_for_daily_shares"
    return {
        "status": status,
        "blockers": blockers,
        "full_market_share_level": total_usable,
        "supports_intraday": False,
        "supports_amount": False,
        "note": (
            "份额口径可 T+1 用；金额/市值需外部另算净值，本模块不提供；"
            "沪市货币 ETF 缺口需另补，不得用固定偏移补齐。"
        ),
    }


# ── 真实取数（IO 集中在这里，可注入 http 便于测试）──────────────────────


def _default_get(url: str, *, params: Mapping[str, Any], headers: Mapping[str, str],
                 timeout: float) -> Any:
    import requests  # 延迟 import：解析纯函数无需网络依赖

    # 用 Session 才能关掉 trust_env（requests.get 不接受该参数）；
    # 交易所域名直连，避免进程里的 HTTP(S)_PROXY 把请求带偏。
    with requests.Session() as sess:
        sess.trust_env = False
        return sess.get(url, params=dict(params), headers=dict(headers), timeout=timeout)


def fetch_sse_day(trade_date: str, *, http: Callable[..., Any] | None = None,
                  retries: int = 3, timeout: float = 25.0) -> SourceDay:
    """真实拉取上交所某日 ETF 份额（T+1）。失败返回 `usable=False` 的 SourceDay，不抛。"""
    get = http or _default_get
    params = {
        "isPagination": "true",
        "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        "sqlId": SSE_SQLID,
        "STAT_DATE": trade_date,
    }
    last_err: str | None = None
    for attempt in range(max(1, retries)):
        try:
            resp = get(SSE_URL, params=params, headers=SSE_HEADERS, timeout=timeout)
            payload = resp.json()
            day = parse_sse_payload(payload, trade_date)
            return day
        except Exception as exc:  # noqa: BLE001 - 网络异常必须变成 gap，不能中断调度
            last_err = f"{type(exc).__name__}: {exc}"
            logger.warning("SSE %s 取数失败(%d/%d): %s", trade_date, attempt + 1, retries, last_err)
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    return SourceDay(
        market=MARKET_SSE,
        requested_date=trade_date,
        usable=False,
        gap_reason="fetch_error",
        fetch_error=last_err,
    )


def fetch_szse_window(start: str, end: str, *, http: Callable[..., Any] | None = None,
                      timeout: float = 40.0) -> dict[str, SourceDay]:
    """真实拉取深交所 `[start, end]` 区间（≤6 个月）的 ETF 份额。

    返回 `{YYYY-MM-DD: SourceDay}`，包含区间内**源里实际出现的每一天**。
    缺失的日子不会出现在返回值里（调用方据此判 gap，而不是补零）。
    """
    get = http or _default_get
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": SZSE_CATALOGID,
        "TABKEY": "tab1",
        "txtStart": start,
        "txtEnd": end,
        "jjlb": "ETF",
        "random": "0.123456",
    }
    try:
        resp = get(SZSE_URL, params=params, headers=SZSE_HEADERS, timeout=timeout)
        content = getattr(resp, "content", None)
        if content is None:
            content = resp.content
        records = _read_szse_xlsx(content)
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        logger.warning("SZSE %s~%s 取数失败: %s", start, end, msg)
        return {
            start: SourceDay(market=MARKET_SZSE, requested_date=start,
                             usable=False, gap_reason="fetch_error", fetch_error=msg)
        }

    by_date: dict[str, list[Mapping[str, Any]]] = {}
    for rec in records:
        d = _norm_date(rec.get("日期") if isinstance(rec, Mapping) else None)
        if d:
            by_date.setdefault(d, []).append(rec)
    return {d: parse_szse_records(recs, d) for d, recs in sorted(by_date.items())}


def _read_szse_xlsx(content: bytes) -> list[dict[str, Any]]:
    import pandas as pd  # 延迟 import

    df = pd.read_excel(io.BytesIO(content))
    df.columns = [str(c).strip() for c in df.columns]
    return df.to_dict("records")


def iter_windows(start: str, end: str, max_days: int = SZSE_MAX_WINDOW_DAYS) -> list[tuple[str, str]]:
    """把长区间切成深交所允许的 ≤6 个月窗口。"""
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    out: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        stop = min(cur + timedelta(days=max_days - 1), e)
        out.append((cur.isoformat(), stop.isoformat()))
        cur = stop + timedelta(days=1)
    return out


def dry_run(start: str, end: str, *, prev: str | None = None,
            http: Callable[..., Any] | None = None) -> dict[str, Any]:
    """端到端只读 dry-run：拉两日、聚合、给判断。**不写库、不写文件。**"""
    cur_sse = fetch_sse_day(end, http=http)
    prev_sse = fetch_sse_day(prev, http=http) if prev else None

    sz_window = fetch_szse_window(prev or start, end, http=http)
    cur_szse = sz_window.get(end) or SourceDay(
        market=MARKET_SZSE, requested_date=end,
        usable=False, gap_reason="not_in_window_result",
    )
    prev_szse = (sz_window.get(prev) if prev else None) or (
        SourceDay(market=MARKET_SZSE, requested_date=prev,
                  usable=False, gap_reason="not_in_window_result")
        if prev else None
    )

    days = {end: [cur_sse, cur_szse]}
    prev_days = {prev: [prev_sse, prev_szse]} if prev and prev_sse and prev_szse else None

    return build_dry_run(days, prev_days)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="市场级 ETF 日度份额替代源（沪深交易所，只读 dry-run，不写库）"
    )
    ap.add_argument("--date", required=True, help="目标交易日 YYYY-MM-DD")
    ap.add_argument("--prev", help="前一交易日 YYYY-MM-DD（给则算份额变动）")
    ap.add_argument("--start", help="深交所窗口起点，默认=--prev 或 --date")
    ap.add_argument("--json-out", help="可选：把报告写到该文件（默认仅 stdout）")
    args = ap.parse_args(argv)

    report = dry_run(args.start or args.prev or args.date, args.date, prev=args.prev)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
