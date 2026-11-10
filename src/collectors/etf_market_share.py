"""市场级 ETF 日度份额替代源（沪/深交易所官方口径，**只读 dry-run**）。

## 这个模块解决什么

TQ 的 `SC8` 序列（`src/collectors/tq_etf_watch.py`）在 2026-10-16 之后停更，
需要一条不依赖 TQ、不依赖付费 key 的全市场 ETF 份额日度来源。
本模块直接读**沪深交易所官方披露**：

| 市场 | 端点 | 粒度 | 源字段 | 源单位 | 换算 |
|---|---|---|---|---|---|
| 上交所 | `query.sse.com.cn/commonQuery.do`<br>`sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L` | 按 `STAT_DATE` 查任意历史日 | `TOT_VOL` | **万份** | ×1e4 → 份 |
| 深交所 | `www.szse.cn/api/report/ShowReport`<br>`CATALOGID=scsj_fund_jjgm`（xlsx） | 区间 ≤6 个月/请求 | `基金规模(份)` | **份** | 原值 |

## 硬口径（写死在代码里，测试钉子钉住）

1. **单位是份额**。本模块**只产出份额（份）**，不乘净值、不产出任何"金额/市值"字段，
   也不把份额改单位叫金额。份额 × 净值是另一个口径，必须由调用方显式做并另行标注。
2. **全市场，不是宽基篮子**。模块只按"交易所返回的全部 ETF"聚合；
   不接受、也没有任何"20 只重点宽基"入口——那 20 只 2026-10-16 只占全市场 15.5%，
   拿它冒充全市场属于虚报（见 `docs`/证据报告）。
3. **沪深两个市场强制在场**。缺一个市场就是 `market_missing` gap，合计不得标 `complete`。
   只拿到一个交易所时，任何"全市场合计"都是低报，必须显式失败。
4. **日期严格解析并过滤**。只接受 `YYYY-MM-DD` / `YYYYMMDD` 且是真实日历日；
   `2026-11-9`、`2026/11/09`、`2026-02-30`、带时间戳的字符串一律拒绝。
   源里别的日期的行必须丢弃并计数，**不能混进请求日求和**。
5. **缺失不补零，脏行 fail-closed**。某日源返回 0 行、真实日期与请求日不符、同一代码重复且份额冲突，
   一律标成 gap（`usable=False` + `gap_reason`），**不写 0、不外推、不用别的源偷偷填**。
   任何一行份额/代码解析失败而被丢弃（`dropped_no_value` / `dropped_bad_value`）同样 fail-closed：
   该日 `usable=False`，报告**不输出部分和**（`sum_shares=None` + `sum_withheld=True`），
   因为丢一行就等于合计低报一行。日期脏的行（`dropped_bad_date`）单独计数并告警，
   同样不参与求和。

## 上市/终止的处理（这是最容易算错的地方）

`Σ份额` 的直接差分会把**新发行上市**当成净申购、把**终止/摘牌**当成净赎回。
所以本模块把一日对差分成三段：

```
全市场份额差 (naive) = 可比口径份额差 + 新上市份额 - 终止份额
                        ↑ 只对两日都存在的代码求 Δ   ↑ 计入但单列   ↑ 计入但单列
```

`diff_shares()` 会**同时**给出 `comparable_change_shares`（可比口径份额差）与
`naive_total_delta_shares`（朴素总额差），并断言
`comparable + new - gone == naive`（`identity_ok`）。实测样例
（2026-10-16 → 2026-11-09）：深交所新增 11 只（合计 8.79 亿份）、
上交所终止 1 只（512390，0.10 亿份）——若直接差分会把 8.79 亿份一级发行
算成二级净申购。

## 份额差永远不叫"净申购"

审计口径：**份额差绝不称净申购**。原因不是措辞洁癖——份额变化 = 申赎 + 折算/拆分 +
一级发行/摘牌 + 数据口径调整，单看两天份额根本推不出资金净流入。
即使两个日期在日历上相邻（工作日→次日 / 周五→下周一），也只说明"可能是一步"，
不代表这两天都是交易日，更不代表中间的份额变化全是二级申赎。

所以本模块**没有任何 `net_subscription` 数值字段**，对外统一叫
`comparable_change_shares`（可比口径份额差）。`adjacent_session` 只作为
"日历上相邻"的提示，不构成称净申购的许可。跨日/跨节假日/同日/倒序时
`span_calendar_days` 会给出实际跨度并标注 `not_net_subscription=True`。

## 已知覆盖缺口（不外推、必须随数一起报）

- **货币 ETF 的缺口是「未知集合」**。上交所份额接口不返回货币 ETF（实测 2026-11-09：
  511990 / 511880 / 511660 都不在返回里，而 511010 等债券 ETF 都在），但**没有任何免费接口
  能枚举「沪市货币 ETF 完整名单」**，所以本模块只说「以下候选代码当日未出现在源里」，
  绝不说「只缺这三只」。报告字段：`known_gaps.sse_money_etf.codes`（当日未出现的候选）、
  `checked_codes`（核对过的候选）、`verified_at`（核对日）、`coverage_unknown=True`、
  `candidate_list_complete=False`。候选一旦在源里出现（例如 511990 恢复披露），
  它会自动从 `codes` 移到 `present_codes`，不允许继续宣称缺失。
- **深市不同**：深交所份额接口实测**返回**货币 ETF（159001 / 159003 / 159005 在源里），
  所以深市合计含货币 ETF、沪市合计不含。`known_gaps.szse_money_etf` 按当日实际返回核对，
  `market_total.money_etf_coverage` 分市场标注，避免一句 `covers_money_etf=False`
  把「深市有、沪市没有」抹平成「全都没有」。
- 因此 `market_total.sum_shares` **不是完整全市场份额**：`full_market_total=False`，
  `coverage_note` 说明缺的是沪市货币 ETF（且缺口集合未知）。
- 两个交易所都是 **T+1** 发布，没有盘中；请求当日拿不到当日份额（返回空 → 记 gap）。
- **上交所分页必须证明取全**：`pageHelp.pageSize` 实测被服务端截到 2000
  （`pageSizeWithOutLimit=2000`，请求 10000 也只给 2000），单页装满就会静默截断。
  `fetch_sse_day` 按 `pageHelp.pageCount` 逐页取全（有上限，超限 fail-closed）；
  `parse_sse_payload` 只认 `int` 型 `total`：缺 `total`、`total` 是字符串、或
  `total != 实收行数` 一律 `usable=False` —— 无法证明取全就不许当完整。
- **深交所单请求 ≤6 个日历月**：`fetch_szse_window` 按**自然月**切窗（`iter_windows`），
  不是 183 天。实测 `2026-01-05~2026-07-05`（正好 6 个月）可取 103 天，
  多 1 天（`~2026-07-06`）整窗回「没有找到符合条件的数据！」占位行。
- **历史不止近 6 个月**：6 个月是**单次请求窗口**上限，不是源侧保留期。实测
  `2021-11-01~2022-04-29` 仍可取到（更早未验证，不断言）。窗口取不到时记
  `empty_result` / `fetch_error`，不推断、不外推、不拿别的源偷偷补。
- **取数失败要传播到目标日**：`fetch_szse_window` 会记录失败窗口区间；
  目标日没取到且同期有失败窗口时，`dry_run` 报 `gap_reason=fetch_error`
  并把原始错误放进 `fetch_error`，绝不把网络失败写成「源无该日数据」。

## 只读

本模块**不 import 任何数据库/Web 层**，没有任何写库/写文件路径（连 `--json-out` 都没有）。
`dry_run()` 返回可 JSON 序列化的 dict，CLI 只打印 stdout。
要落库是后续独立改动（需要新列/新迁移，本模块故意不提供 write 入口）。

## CLI

```bash
python -m src.collectors.etf_market_share --prev 2026-11-06 --date 2026-11-09
```

退出码：

- `0`：沪深两市都取到且可用，请求的变动也可用（`complete=True`）；
- `1`：数据/口径不完整（缺市场、日期不符、丢行 fail-closed、缺前一交易日、跨日变动等），
  报告 `complete=False`，`verdict.blockers` 说明原因，**且不输出部分和**
  （`market_total.sum_shares=None` / `share_change.*=None` + `*_withheld=True`）；
- `2`：参数非法或完全无数据（报告 `verdict.status=unusable`）。

`0` **只代表交易所份额接口口径的两市数据完整**，不代表完整全市场——沪市货币 ETF
缺口见 `known_gaps.sse_money_etf`（缺口是未知集合，深市则该接口含货币 ETF）。
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────

MARKET_SSE = "SSE"
MARKET_SZSE = "SZSE"
REQUIRED_MARKETS = (MARKET_SSE, MARKET_SZSE)
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
#: 深交所区间接口在无数据时返回的占位行（整行只有这一句话），不是 schema 错误。
SZSE_NO_DATA_MARKERS = ("没有找到符合条件的数据", "no data found")
#: 深交所单请求最大跨度：**6 个日历月**（不是 183 天）。
#: 实测 2026-01-05~2026-07-05（正好 6 个月）可取 103 天；~2026-07-06（+1 天）整窗回占位行。
SZSE_MAX_WINDOW_MONTHS = 6

#: 上交所分页：请求 10000 服务端也只给 2000（`pageSizeWithOutLimit=2000`），
#: 所以必须按 `pageHelp.pageCount` 翻页，否则单页装满就静默截断。
SSE_PAGE_SIZE = 2000
#: 翻页上限（防御：pageCount 异常/服务端重复回同一页时不死循环，超限 fail-closed）。
SSE_MAX_PAGES = 50

#: 沪市货币 ETF 的**候选**代码：只用于逐日核对源里是否已出现，**不是完整缺口清单**。
#: 没有任何免费接口能枚举「沪市货币 ETF 完整名单」→ 缺口本身是未知集合，
#: 不允许把这三只当成「只缺这三只」。
SSE_MONEY_ETF_CANDIDATES = ("511990", "511880", "511660")
#: 深市货币 ETF 候选：实测这些代码**在**深交所份额接口返回里（深市合计含货币 ETF）。
SZSE_MONEY_ETF_CANDIDATES = ("159001", "159003", "159005")

#: 货币 ETF 缺口说明。缺的是**免费公开源**的覆盖，不是我们不取。
MONEY_ETF_GAP_NOTE = (
    "免费公开源（沪深交易所份额接口）不返回沪市货币 ETF，因此 Σ份额 是"
    "交易所份额接口口径的合计，**不含**沪市货币 ETF，不是完整全市场份额；"
    "沪市货币 ETF 的完整名单无法从免费接口枚举，缺口是**未知集合**，"
    "候选清单只用于逐日核对、不等于缺口全集；需另接货币基金份额源后重算，"
    "不得用固定偏移补齐。深市份额接口则返回货币 ETF，两市口径不一致，故分市场标注。"
)

#: CLI 退出码
EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 1
EXIT_UNUSABLE = 2

#: 相邻交易日的最大日历跨度：工作日→次日(1)；周五→下周一(3)。
#: 没有交易日历，节假日后的首日无法证明"相邻"，一律按跨日处理。
_ADJACENT_FRIDAY = 4


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

    `usable=False` 时 `rows` 可能仍非空（例如重复冲突），**但调用方不得据此求和**。
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
    dropped_bad_date: int = 0
    out_of_date_rows: int = 0
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
    def dropped_share_rows(self) -> int:
        """份额/代码解析失败而被丢弃的行数。

        `>0` 表示这一天的合计**必然低报**，因此该日不得 `usable`，
        报告也不得把 `sum_shares` 当完整合计输出。
        """
        return self.dropped_no_value + self.dropped_bad_value

    @property
    def sum_shares(self) -> float:
        """已解析行的 Σ份额（份）。

        `usable=False` 时这只是**部分和**，仅作诊断；报告必须同时看 `sum_withheld`。
        """
        return math.fsum(r.shares for r in self.rows)

    @property
    def sum_shares_yi(self) -> float:
        """Σ份额（亿份）——仅为可读，字段名带 `_yi_shares` 语义，**不是金额**。"""
        return self.sum_shares / 1e8

    def by_code(self) -> dict[str, EtfShareRow]:
        return {r.code: r for r in self.rows}


@dataclass
class ShareDiff:
    """两个交易日之间、**同一市场**的份额变动拆分（上市/终止单列）。"""

    market: str
    prev_date: str | None
    cur_date: str | None
    prev_n: int = 0
    cur_n: int = 0
    comparable_n: int = 0
    #: 可比口径份额差（只对两日都存在的代码求 Δ）。**永远不是净申购**：
    #: 份额变化含申赎/折算/拆分/一级发行/摘牌，本模块不给净申购数值。
    comparable_change_shares: float = 0.0
    new_codes: tuple[str, ...] = ()
    new_shares: float = 0.0
    gone_codes: tuple[str, ...] = ()
    gone_shares: float = 0.0
    naive_total_delta_shares: float = 0.0
    span_calendar_days: int | None = None
    #: 仅表示"日历上相邻"（工作日→次日 / 周五→下周一），**不构成称净申购的许可**。
    adjacent_session: bool = False
    identity_ok: bool = False
    usable: bool = True
    reason: str | None = None

    @property
    def change_label(self) -> str:
        return "share_change"

    def to_dict(self) -> dict[str, Any]:
        adjacent = self.adjacent_session
        note = (
            "份额差 ≠ 资金净流入：含申赎、折算/拆分、一级发行/摘牌，"
            "本模块不提供净申购数值"
        )
        # 不可用时**绝不输出部分差**：数字一律 0.0 + numbers_withheld=True，
        # 免得调用方把「只算了一个边」的差值当成变动。
        withheld = not self.usable

        def _num(value: float) -> float:
            return 0.0 if withheld else round(value, 2)

        def _codes(codes: tuple[str, ...]) -> list[str]:
            return [] if withheld else list(codes)

        return {
            "market": self.market,
            "market_label": MARKET_LABELS.get(self.market, self.market),
            "prev_date": self.prev_date,
            "cur_date": self.cur_date,
            "prev_n": self.prev_n,
            "cur_n": self.cur_n,
            "comparable_n": self.comparable_n,
            "span_calendar_days": self.span_calendar_days,
            "adjacent_session": adjacent,
            "change_label": self.change_label,
            "comparable_change_shares": _num(self.comparable_change_shares),
            "comparable_change_yi_shares": _num(self.comparable_change_shares / 1e8),
            # 审计口径：份额差绝不称净申购 —— 不输出任何 net_subscription 数值
            "not_net_subscription": True,
            "not_net_subscription_reason": note,
            "new_listed_codes": _codes(self.new_codes),
            "new_listed_shares": _num(self.new_shares),
            "terminated_codes": _codes(self.gone_codes),
            "terminated_shares": _num(self.gone_shares),
            "naive_total_delta_shares": _num(self.naive_total_delta_shares),
            "naive_note": "总额直差，含一级发行与摘牌，不等于净申购",
            "interpretation_warning": (
                "份额变动只说明份额差；本模块不判定资金流，也未核实份额折算/拆分。"
                "大额变动可能是申购、赎回、折算或拆分，需另行核实。"
            ),
            "identity_ok": bool(self.identity_ok and self.usable),
            "usable": self.usable,
            "numbers_withheld": withheld,
            "withheld_reason": (
                f"变动不可用({self.reason})：不输出部分差，数字全部置 0 并标记 withheld"
                if withheld else None
            ),
            "reason": self.reason,
            "unit": UNIT_SHARES,
        }


def _norm_code(raw: Any) -> str:
    """统一证券代码为 6 位字符串。

    深交所 xlsx 里 `基金代码` 是**整数**（158000），证据 JSON 里也可能是 int；
    若不补齐，跨日集合比较会 100% 误判为"全部新增 + 全部终止"。
    """
    if raw is None or isinstance(raw, bool):
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


_DATE_DASH_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_COMPACT_RE = re.compile(r"^(\d{8})$")


def parse_date_strict(raw: Any) -> _date | None:
    """严格解析日期：只认 `YYYY-MM-DD` / `YYYYMMDD`（trim 后整串匹配），且必须是真实日历日。

    拒绝 `2026-11-9`、`2026/11/09`、`2026-13-01`、`2026-02-30`、带时间戳、
    float（`20261109.0`）、bool 等一切含糊形式——含糊日期不能用来判"哪天"。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, _date):
        return raw
    if isinstance(raw, float):
        return None
    if isinstance(raw, int):
        s = str(raw)
    else:
        s = str(raw).strip()
    m = _DATE_DASH_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = _DATE_COMPACT_RE.match(s)
        if not m:
            return None
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
    try:
        return _date(y, mo, d)
    except ValueError:
        return None


def _norm_date(raw: Any) -> str | None:
    d = parse_date_strict(raw)
    return d.isoformat() if d else None


def _find_market(days: Iterable[SourceDay], market: str) -> SourceDay | None:
    return next((d for d in days if d.market == market), None)


# ── 解析（纯函数，无 IO，测试直接打这里）────────────────────────────────


def parse_sse_payload(payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
                      requested_date: str) -> SourceDay:
    """解析上交所 `commonQuery.do` 响应为 `SourceDay`。

    - `TOT_VOL` 单位**万份** → ×1e4 转**份**；
    - 日期严格解析；非请求日的行丢弃并计数（`out_of_date_rows`）；
    - 空/非法 `TOT_VOL` 丢弃并计数（不补零）；
    - 同代码重复：份额相同 → 去重并记录；份额不同 → `usable=False`（无法判断哪个对）。
    """
    rd = parse_date_strict(requested_date)
    if rd is None:
        return SourceDay(
            market=MARKET_SSE, requested_date=str(requested_date),
            usable=False, gap_reason="bad_date",
        )
    requested_date = rd.isoformat()
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
        d = parse_date_strict(item.get("STAT_DATE"))
        if d is None:
            day.dropped_bad_date += 1
            continue
        seen_dates.add(d.isoformat())
        if d.isoformat() != requested_date:
            day.out_of_date_rows += 1
            continue
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

    if requested_date in seen_dates:
        day.data_date = requested_date
    elif seen_dates:
        day.data_date = max(seen_dates)
    else:
        day.data_date = None

    if day.data_date and day.data_date != requested_date:
        # 源把别的日期当请求日返回 → 不能当成请求日使用
        day.date_mismatch = True
        day.usable = False
        day.gap_reason = f"date_mismatch(请求 {requested_date}, 源内 {day.data_date})"

    if day.out_of_date_rows:
        day.warnings.append(
            f"源内另有 {day.out_of_date_rows} 行非请求日数据，已丢弃（未参与求和）"
        )

    if conflicts:
        day.usable = False
        day.gap_reason = f"duplicate_conflict({len(conflicts)} 只代码份额不一致)"

    if day.dropped_share_rows:
        # 丢一行就是合计低报一行：fail-closed，报告不输出部分和
        day.usable = False
        day.gap_reason = day.gap_reason or (
            f"dropped_rows(no_value={day.dropped_no_value},"
            f"bad_value={day.dropped_bad_value})"
        )
        day.warnings.append(
            f"{day.dropped_share_rows} 行份额/代码无法解析被丢弃"
            f"（no_value={day.dropped_no_value}, bad_value={day.dropped_bad_value}）"
            "→ 该日合计必然低报，已 fail-closed（不出部分和）"
        )
    if day.dropped_bad_date:
        day.warnings.append(
            f"{day.dropped_bad_date} 行日期无法严格解析被丢弃，未参与求和"
            "（无法证明它不属于请求日，未据此宣称完整）"
        )

    if not by_code:
        day.usable = False
        if day.gap_reason is None:
            day.gap_reason = (
                "no_dated_rows" if (day.dropped_bad_date and not seen_dates) else "no_valid_rows"
            )

    # 分页必须**证明**取全：total 缺失 / 非 int / 与实收不符，都不能当完整
    if total is None:
        day.usable = False
        day.gap_reason = day.gap_reason or "pagination_unverified(total_missing)"
        day.warnings.append("源未返回 pageHelp.total：无法证明分页取全，fail-closed")
    elif isinstance(total, bool) or not isinstance(total, int):
        day.usable = False
        day.gap_reason = day.gap_reason or (
            f"pagination_unverified(total_type={type(total).__name__})"
        )
        day.warnings.append(
            f"pageHelp.total 类型异常（{type(total).__name__}={total!r}）：不能用来校验分页完整性"
        )
    elif total != day.raw_row_count:
        # 分页没取全：Σ 会低报，必须暴露而不是静默
        day.warnings.append(f"分页未取全: total={total} 实收={day.raw_row_count}")
        day.usable = False
        day.gap_reason = day.gap_reason or f"pagination_incomplete(total={total})"

    return day


def parse_szse_records(records: Iterable[Mapping[str, Any]],
                       requested_date: str) -> SourceDay:
    """解析深交所 `scsj_fund_jjgm` xlsx 行（`基金规模(份)`，单位**份**，无需换算）。

    区间接口会一次返回多日：日期严格解析后**只保留请求日**的行，
    `data_date` 恒等于请求日（在源里出现时），不能取区间最大日。
    """
    rd = parse_date_strict(requested_date)
    if rd is None:
        return SourceDay(
            market=MARKET_SZSE, requested_date=str(requested_date),
            usable=False, gap_reason="bad_date",
        )
    requested_date = rd.isoformat()
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
        d = parse_date_strict(rec.get("日期") if "日期" in rec else rec.get("date"))
        if d is None:
            day.dropped_bad_date += 1
            continue
        seen_dates.add(d.isoformat())
        if d.isoformat() != requested_date:
            day.out_of_date_rows += 1
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

    if requested_date in seen_dates:
        day.data_date = requested_date
    elif seen_dates:
        day.data_date = max(seen_dates)
    else:
        day.data_date = None

    if day.data_date and day.data_date != requested_date:
        day.date_mismatch = True
        day.usable = False
        day.gap_reason = f"date_mismatch(请求 {requested_date}, 源内 {day.data_date})"

    if day.out_of_date_rows:
        day.warnings.append(
            f"区间源另有 {day.out_of_date_rows} 行非请求日数据，已丢弃（未参与求和）"
        )

    if conflicts:
        day.usable = False
        day.gap_reason = f"duplicate_conflict({len(conflicts)} 只代码份额不一致)"

    if day.dropped_share_rows:
        # 丢一行就是合计低报一行：fail-closed，报告不输出部分和
        day.usable = False
        day.gap_reason = day.gap_reason or (
            f"dropped_rows(no_value={day.dropped_no_value},"
            f"bad_value={day.dropped_bad_value})"
        )
        day.warnings.append(
            f"{day.dropped_share_rows} 行份额/代码无法解析被丢弃"
            f"（no_value={day.dropped_no_value}, bad_value={day.dropped_bad_value}）"
            "→ 该日合计必然低报，已 fail-closed（不出部分和）"
        )
    if day.dropped_bad_date:
        day.warnings.append(
            f"{day.dropped_bad_date} 行日期无法严格解析被丢弃，未参与求和"
            "（无法证明它不属于请求日，未据此宣称完整）"
        )

    if not by_code:
        day.usable = False
        if day.gap_reason is None:
            day.gap_reason = (
                "no_dated_rows" if (day.dropped_bad_date and not seen_dates) else "empty_result"
            )
        day.warnings.append("源对该日无有效行：可能是非交易日，也可能是尚未发布，无法区分")

    return day


# ── 对差（上市/终止单列；份额差永不称净申购）──────────────────────────


def _calendar_span(prev_date: str | None, cur_date: str | None) -> int | None:
    p = parse_date_strict(prev_date)
    c = parse_date_strict(cur_date)
    if p is None or c is None:
        return None
    return (c - p).days


def is_adjacent_session(prev_date: str | None, cur_date: str | None) -> bool:
    """两个日期是否可证明为**相邻交易日**（本模块没有交易日历）。

    只认：工作日 → 次日；周五 → 下周一。其余一律 False——宁可保守，
    也不把跨节假日/跨多日的区间份额差冒充成"单日净申购"。
    """
    p = parse_date_strict(prev_date)
    span = _calendar_span(prev_date, cur_date)
    if p is None or span is None:
        return False
    if span == 1 and p.weekday() <= 3:
        return True
    if span == 3 and p.weekday() == _ADJACENT_FRIDAY:
        return True
    return False


def diff_shares(prev: SourceDay | None, cur: SourceDay | None) -> ShareDiff:
    """两个交易日同市场份额变动，**可比口径与总额差分开**。

    恒等式：`comparable_change + new_shares - gone_shares == naive_total_delta`
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

    d.span_calendar_days = _calendar_span(prev.data_date, cur.data_date)
    d.adjacent_session = is_adjacent_session(prev.data_date, cur.data_date)

    if not prev.usable or not cur.usable:
        d.usable = False
        d.reason = f"source_unusable(prev={prev.gap_reason}, cur={cur.gap_reason})"

    if d.span_calendar_days is None:
        d.usable = False
        d.reason = d.reason or "date_unknown"
    elif d.span_calendar_days <= 0:
        d.usable = False
        d.reason = d.reason or "prev_not_before_cur"

    p = prev.by_code()
    c = cur.by_code()
    common = sorted(set(p) & set(c))
    new_codes = sorted(set(c) - set(p))
    gone_codes = sorted(set(p) - set(c))

    d.comparable_n = len(common)
    d.comparable_change_shares = math.fsum(c[k].shares - p[k].shares for k in common)
    d.new_codes = tuple(new_codes)
    d.new_shares = math.fsum(c[k].shares for k in new_codes)
    d.gone_codes = tuple(gone_codes)
    d.gone_shares = math.fsum(p[k].shares for k in gone_codes)
    d.naive_total_delta_shares = cur.sum_shares - prev.sum_shares

    d.identity_ok = math.isclose(
        d.comparable_change_shares + d.new_shares - d.gone_shares,
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
    usable = day.usable
    return {
        "market": day.market,
        "market_label": MARKET_LABELS.get(day.market, day.market),
        "requested_date": day.requested_date,
        "data_date": day.data_date,
        # 不可用时不输出部分和：份额合计是「完整才给」的字段
        "sum_shares": round(day.sum_shares, 2) if usable else None,
        "sum_yi_shares": round(day.sum_shares_yi, 4) if usable else None,
        "sum_withheld": not usable,
        "sum_withheld_reason": (
            None if usable
            else f"usable=False({day.gap_reason})：不出部分和（丢行/缺行即低报）"
        ),
        "n_funds": day.n_funds,
        "unit": UNIT_SHARES,
        "unit_source": day.unit_source,
        "unit_note": day.unit_note,
        "usable": usable,
        "gap_reason": day.gap_reason,
        "date_mismatch": day.date_mismatch,
        "raw_row_count": day.raw_row_count,
        "duplicate_codes": list(day.duplicate_codes),
        "conflicting_duplicates": list(day.conflicting_duplicates),
        "dropped_no_value": day.dropped_no_value,
        "dropped_bad_value": day.dropped_bad_value,
        "dropped_bad_date": day.dropped_bad_date,
        "dropped_share_rows": day.dropped_share_rows,
        "out_of_date_rows": day.out_of_date_rows,
        "warnings": list(day.warnings),
        "fetch_error": day.fetch_error,
    }


def _sse_money_etf_gap(day: SourceDay | None, as_of: str | None) -> dict[str, Any]:
    """按**当日实际返回**核对沪市货币 ETF 候选，绝不写死「只缺三只」。

    货币 ETF 的完整名单无法从免费接口枚举 → 缺口是**未知集合**，
    `codes` 只表示「候选里当日没出现的」，`checked_codes` 记录核对过哪些。
    """
    checked = list(SSE_MONEY_ETF_CANDIDATES)
    present = {r.code for r in (day.rows if day is not None else ())}
    absent = [c for c in checked if c not in present]
    seen = [c for c in checked if c in present]
    return {
        "codes": absent,
        "checked_codes": checked,
        "present_codes": seen,
        "verified_at": as_of,
        "verified_against": f"{MARKET_SSE} 当日返回的代码集合",
        "source_day_usable": bool(day is not None and day.usable),
        "coverage_unknown": True,
        "candidate_list_complete": False,
        "reason": "免费公开源（上交所 ETF 份额接口）不返回货币 ETF",
        "affects_total": True,
        "inferred": True,
        "quantified": False,
        "note": MONEY_ETF_GAP_NOTE,
        "verified_note": (
            "缺口是**未知集合**：codes 只是候选里当日未出现的代码，"
            "不是沪市货币 ETF 的完整缺口清单；候选一旦在源里出现会自动移出。"
        ),
    }


def _szse_money_etf_coverage(day: SourceDay | None, as_of: str | None) -> dict[str, Any]:
    """深交所份额接口实测**返回**货币 ETF：按当日实际返回标注覆盖。"""
    checked = list(SZSE_MONEY_ETF_CANDIDATES)
    present = {r.code for r in (day.rows if day is not None else ())}
    observed = [c for c in checked if c in present]
    return {
        "covered": bool(observed),
        "codes": observed,
        "checked_codes": checked,
        "verified_at": as_of,
        "verified_against": f"{MARKET_SZSE} 当日返回的代码集合",
        "affects_total": False,
        "note": (
            "深交所份额接口实测返回货币 ETF（159001/159003/159005），"
            "所以深市合计**含**货币 ETF，沪市合计不含 → 两市口径不一致，已分市场标注。"
        ),
        "verified_note": (
            None if observed
            else "当日未观测到深市货币 ETF 候选，不能据此断言覆盖或缺失"
        ),
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
    - 沪深两个市场**强制在场**：缺哪个就补 `market_missing` gap，绝不静默少算；
    - 任一市场当日不可用 → 全市场合计 `complete=False`，且**不出部分和**
      （`sum_shares=None` + `sum_withheld=True`）；
    - 给了多日映射 `{date: [...]}` 时，**更早那天自动当 prev**，不许静默丢掉；
    - 变动只要有一个市场不可用就是部分和：`complete=False`、
      `covered_markets` 只列可用市场、`missing_prev_markets` 列出全部不可用市场，
      汇总数值置 `None`（`numbers_withheld=True`），`adjacent_session` 也不许为 True；
    - 货币 ETF 缺口按**当日实际返回**核对：沪市候选缺谁就报谁、并标
      `coverage_unknown=True`；深市单独标覆盖，不一句 `covers_money_etf=False` 抹平；
    - `tq_sc8_reference` / `money_etf_gap` 只作为**外部证据**透传，不参与求和。
    """
    date_keys = sorted(days)
    current_date = date_keys[-1] if date_keys else None

    prev_from_days = False
    unused_dates: list[str] = []
    if prev_days is None and len(date_keys) >= 2:
        # 调用方直接给了多日快照：更早那天就是 prev。静默丢掉它等于隐藏变动。
        prev_days = {date_keys[-2]: days[date_keys[-2]]}
        prev_from_days = True
        unused_dates = date_keys[:-2]
    prev_requested = prev_days is not None
    prev_map: Mapping[str, Sequence[SourceDay]] = prev_days or {}
    prev_date = max(prev_map) if prev_map else None

    markets: dict[str, dict[str, Any]] = {}
    diffs: dict[str, dict[str, Any]] = {}

    for market in REQUIRED_MARKETS:
        if current_date is None:
            continue
        cur = _find_market(days.get(current_date, ()), market)
        if cur is None:
            cur = SourceDay(
                market=market, requested_date=current_date,
                usable=False, gap_reason="market_missing",
            )
        markets[market] = _market_day(cur)

        if prev_requested:
            prev = _find_market(prev_map.get(prev_date, ()), market) if prev_date else None
            if prev is None:
                prev = SourceDay(
                    market=market, requested_date=prev_date or "",
                    usable=False, gap_reason="market_missing",
                )
            diffs[market] = diff_shares(prev, cur).to_dict()

    usable_markets = [m for m, v in markets.items() if v.get("usable")]
    gap_markets = {m: v.get("gap_reason") for m, v in markets.items() if not v.get("usable")}
    markets_complete = (
        current_date is not None
        and set(markets) == set(REQUIRED_MARKETS)
        and all(v.get("usable") for v in markets.values())
    )

    usable_diff_markets = [m for m, d in diffs.items() if d.get("usable")]
    diff_complete = bool(
        prev_requested
        and set(diffs) == set(REQUIRED_MARKETS)
        and all(d.get("usable") for d in diffs.values())
    )
    complete = bool(markets_complete and (diff_complete if prev_requested else True))

    # 合计只在「两市都完整」时给数值；否则不出部分和
    total_shares = (
        math.fsum(markets[m]["sum_shares"] for m in usable_markets)
        if markets_complete else None
    )

    usable_diffs = [d for d in diffs.values() if d.get("usable")]
    comparable_sum = (
        math.fsum(d["comparable_change_shares"] for d in usable_diffs) if diff_complete else None
    )
    naive_sum = (
        math.fsum(d["naive_total_delta_shares"] for d in usable_diffs) if diff_complete else None
    )
    # 只要有一个市场不可用，就不许标「相邻交易日」——那会让部分和看起来像单日变动
    adjacent_out: bool | None = None
    if prev_requested:
        adjacent_out = bool(
            diff_complete and usable_diffs and all(d["adjacent_session"] for d in usable_diffs)
        )

    change_label = "not_available" if (not prev_requested or not diffs) else "share_change"

    cur_days_today = days.get(current_date, ()) if current_date else ()
    sse_day = _find_market(cur_days_today, MARKET_SSE)
    szse_day = _find_market(cur_days_today, MARKET_SZSE)
    money_gap = (
        dict(money_etf_gap) if money_etf_gap
        else _sse_money_etf_gap(sse_day, current_date)
    )
    szse_money = _szse_money_etf_coverage(szse_day, current_date)

    return {
        "kind": "etf_market_share_dry_run",
        "read_only": True,
        "wrote_database": False,
        "as_of_requested": current_date,
        "prev_requested": prev_date,
        "unit": UNIT_SHARES,
        "amount_fields_present": False,   # 显式声明：本报告没有金额/市值
        "basket_proxy_used": False,       # 显式声明：不是 20 只宽基
        "complete": complete,
        "markets": markets,
        "market_total": {
            "n_funds": sum(markets[m]["n_funds"] for m in usable_markets),
            "n_funds_partial": not markets_complete,
            "sum_shares": round(total_shares, 2) if total_shares is not None else None,
            "sum_yi_shares": round(total_shares / 1e8, 4) if total_shares is not None else None,
            "sum_withheld": total_shares is None,
            "sum_withheld_reason": (
                None if total_shares is not None
                else "沪深未同时可用：不出部分和（缺市场即低报），"
                     f"missing={gap_markets}"
            ),
            "unit": UNIT_SHARES,
            "complete": bool(markets_complete),
            "usable_markets": usable_markets,
            "missing_markets": gap_markets,
            "excludes_money_etf": True,
            # 口径声明：沪市不含货币 ETF；深市实测含 → 合计不构成完整货币 ETF 覆盖
            "covers_money_etf": False,
            "money_etf_coverage": {
                "sse": "excluded",
                "szse": "included" if szse_money["covered"] else "unverified",
            },
            "full_market_total": False,
            "coverage_note": MONEY_ETF_GAP_NOTE,
            "note": (
                "仅含可用市场合计；不可用市场未计入且未补零（不出部分和）"
                if not markets_complete
                else "沪深两市已取到，但合计仍不含沪市货币 ETF，不是完整全市场"
            ),
        },
        "share_change": {
            "available": bool(prev_requested and diffs),
            "prev_requested": prev_requested,
            "prev_source": (
                "inferred_from_days" if prev_from_days
                else ("explicit" if prev_requested else None)
            ),
            "unused_dates": unused_dates,
            "label": change_label,
            "complete": (diff_complete if prev_requested else None),
            "covered_markets": sorted(usable_diff_markets),
            "missing_prev_markets": [
                mk for mk, d in diffs.items() if not d.get("usable")
            ],
            "adjacent_session": adjacent_out,
            "not_net_subscription": True if (prev_requested and diffs) else None,
            "not_net_subscription_reason": (
                "份额差 ≠ 资金净流入：含申赎、折算/拆分、一级发行/摘牌，"
                "本模块不提供净申购数值"
                if (prev_requested and diffs) else None
            ),
            "comparable_change_shares": (
                round(comparable_sum, 2) if comparable_sum is not None else None
            ),
            "comparable_change_yi_shares": (
                round(comparable_sum / 1e8, 4) if comparable_sum is not None else None
            ),
            "naive_total_delta_shares": (
                round(naive_sum, 2) if naive_sum is not None else None
            ),
            "naive_total_delta_yi_shares": (
                round(naive_sum / 1e8, 4) if naive_sum is not None else None
            ),
            "numbers_withheld": bool(prev_requested and not diff_complete),
            "withheld_reason": (
                "变动不完整：有市场不可用，汇总差值只是部分和，故不出数值"
                if (prev_requested and not diff_complete) else None
            ),
            "naive_note": "总额直差，含一级发行与摘牌，不等于净申购",
            "by_market": diffs,
            "unit": UNIT_SHARES,
            "note": (
                "comparable_change 只对两日都存在的代码求 Δ，已剔除新上市/终止；"
                "份额差绝不称净申购（无任何 net_subscription 数值），"
                "无论两天在日历上是否相邻；"
                "naive_total_delta 是总额直差，含一级发行与摘牌，也不等于净申购。"
            ),
        },
        "known_gaps": {
            "sse_money_etf": money_gap,
            "szse_money_etf": szse_money,
        },
        "tq_sc8_reference": tq_sc8_reference or {"available": False},
        "verdict": _verdict(markets, diffs, markets_complete, prev_requested),
    }


def _verdict(markets: Mapping[str, Mapping[str, Any]],
             diffs: Mapping[str, Mapping[str, Any]],
             markets_complete: bool,
             prev_requested: bool) -> dict[str, Any]:
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
    elif blockers or not markets_complete or (prev_requested and not diffs):
        status = "degraded"
    else:
        status = "usable_for_daily_shares"

    return {
        "status": status,
        "blockers": blockers,
        "full_market_share_level": bool(markets_complete),
        "covers_money_etf": False,
        "supports_intraday": False,
        "supports_amount": False,
        "supports_full_market_total": False,
        "note": (
            "份额口径可 T+1 用；金额/市值需外部另算净值，本模块不提供；"
            "免费源不覆盖沪市货币 ETF（深市接口则返回货币 ETF，两市口径不一致），"
            "合计不是完整全市场，不得用固定偏移补齐。"
        ),
    }


# ── 真实取数（统一 market_http，可注入便于测试）──────────────────────────


class MarketHttpError(RuntimeError):
    """重试耗尽后的取数失败。"""


def market_http(url: str, *, params: Mapping[str, Any], headers: Mapping[str, str],
                timeout: float) -> Any:
    """沪深两条取数路径**唯一**的默认 HTTP 入口。

    用 Session 才能关掉 `trust_env`（`requests.get` 不接受该参数）；
    交易所域名直连，避免进程里的 HTTP(S)_PROXY 把请求带偏。
    """
    import requests  # 延迟 import：解析纯函数无需网络依赖

    with requests.Session() as sess:
        sess.trust_env = False
        return sess.get(url, params=dict(params), headers=dict(headers), timeout=timeout)


def _request(url: str, *, params: Mapping[str, Any], headers: Mapping[str, str],
             timeout: float, retries: int,
             http: Callable[..., Any] | None = None) -> Any:
    get = http or market_http
    attempts = max(1, int(retries))
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return get(url, params=params, headers=headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 网络异常统一转成 gap
            last = exc
            logger.warning(
                "GET %s 失败(%d/%d): %s: %s",
                url, attempt + 1, attempts, type(exc).__name__, exc,
            )
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise MarketHttpError(f"{type(last).__name__}: {last}") from last


def _resp_json(resp: Any) -> Any:
    method = getattr(resp, "json", None)
    if not callable(method):
        raise ValueError("响应不支持 json()")
    return method()


def _resp_bytes(resp: Any) -> bytes:
    content = getattr(resp, "content", None)
    if content is None:
        raise ValueError("响应没有 content")
    return content


def fetch_sse_day(trade_date: str, *, http: Callable[..., Any] | None = None,
                  retries: int = 3, timeout: float = 25.0) -> SourceDay:
    """真实拉取上交所某日 ETF 份额（T+1），**按 pageCount 翻页取全**。

    实测服务端把 `pageHelp.pageSize` 截到 2000（`pageSizeWithOutLimit=2000`）：
    只请求第 1 页在行数 >2000 时必然静默截断。这里逐页取到 `pageHelp.pageCount`，
    并用 `total` 做最终校验（`parse_sse_payload`）。任一页失败 / 无 pageCount 也无 total
    → `usable=False`，**绝不返回半份数据**。
    """
    td = parse_date_strict(trade_date)
    if td is None:
        return SourceDay(
            market=MARKET_SSE, requested_date=str(trade_date),
            usable=False, gap_reason="bad_date", fetch_error="日期格式非法",
        )
    trade_date = td.isoformat()

    collected: list[Any] = []
    total: int | None = None
    page_count: int | None = None
    page_no = 1

    while True:
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": str(SSE_PAGE_SIZE),
            "pageHelp.pageNo": str(page_no),
            "pageHelp.beginPage": str(page_no),
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": str(page_no),
            "sqlId": SSE_SQLID,
            "STAT_DATE": trade_date,
        }
        try:
            resp = _request(
                SSE_URL, params=params, headers=SSE_HEADERS,
                timeout=timeout, retries=retries, http=http,
            )
            payload = _resp_json(resp)
        except Exception as exc:  # noqa: BLE001 - 网络异常必须变成 gap，不能中断调度
            last_err = f"{type(exc).__name__}: {exc}"
            logger.warning("SSE %s 第 %d 页取数失败: %s", trade_date, page_no, last_err)
            return SourceDay(
                market=MARKET_SSE, requested_date=trade_date,
                usable=False, gap_reason="fetch_error", fetch_error=last_err,
            )

        page = payload.get("pageHelp") if isinstance(payload, Mapping) else None
        rows = payload.get("result") if isinstance(payload, Mapping) else None
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            msg = f"result 不是列表({type(rows).__name__})"
            logger.warning("SSE %s 第 %d 页 %s", trade_date, page_no, msg)
            return SourceDay(
                market=MARKET_SSE, requested_date=trade_date,
                usable=False, gap_reason="bad_payload", fetch_error=msg,
            )
        collected.extend(rows)

        if total is None and isinstance(page, Mapping):
            raw_total = page.get("total")
            if isinstance(raw_total, int) and not isinstance(raw_total, bool):
                total = raw_total
        if page_count is None and isinstance(page, Mapping):
            raw_pc = page.get("pageCount")
            if isinstance(raw_pc, int) and not isinstance(raw_pc, bool):
                page_count = raw_pc

        if page_count is not None:
            if page_no >= page_count:
                break
        elif total is not None:
            if len(collected) >= total:
                break
            if not rows:
                # 没给 pageCount、还没取够 total 且已经拿到空页：无法继续，不能当完整
                msg = f"分页中断: pageNo={page_no} 返回空页, 已收 {len(collected)}/{total}"
                logger.warning("SSE %s %s", trade_date, msg)
                return SourceDay(
                    market=MARKET_SSE, requested_date=trade_date,
                    usable=False, gap_reason="pagination_incomplete",
                    fetch_error=msg,
                )
        else:
            # 既无 pageCount 也无 total：交给 parse 判 pagination_unverified（fail-closed）
            break

        page_no += 1
        if page_no > SSE_MAX_PAGES:
            msg = f"翻页超过上限 {SSE_MAX_PAGES} 页，疑似服务端重复回同一页"
            logger.warning("SSE %s %s", trade_date, msg)
            return SourceDay(
                market=MARKET_SSE, requested_date=trade_date,
                usable=False, gap_reason="pagination_overflow", fetch_error=msg,
            )

    merged_page: dict[str, Any] = {
        "pageNo": 1, "pageSize": SSE_PAGE_SIZE, "pageCount": page_count,
    }
    if total is not None:
        merged_page["total"] = total
    day = parse_sse_payload({"result": collected, "pageHelp": merged_page}, trade_date)
    if page_no > 1:
        day.warnings.append(f"分页取全: {page_no} 页共 {len(collected)} 行（pageCount={page_count}）")
    return day


def _add_months(d: _date, months: int) -> _date:
    """按**自然月**推进；目标月天数不足时收到月末（宁可窗口短一点）。

    不能按 183 天推：实测深交所上限是 6 个**日历月**，
    `2026-01-05~2026-07-05` 可用，`~2026-07-06`（+1 天）整窗回占位行。
    """
    y = d.year + (d.month - 1 + months) // 12
    m0 = (d.month - 1 + months) % 12 + 1
    first_next = _date(y, m0, 1) + timedelta(days=32)
    last_day = (first_next.replace(day=1) - timedelta(days=1)).day
    return _date(y, m0, min(d.day, last_day))


def iter_windows(start: str, end: str,
                 max_months: int = SZSE_MAX_WINDOW_MONTHS) -> list[tuple[str, str]]:
    """把长区间切成深交所允许的 **≤6 个日历月**窗口（含首尾）。

    日期必须严格合法；`start > end` 直接报错，不返回空列表让你以为"取到了"。
    """
    s = parse_date_strict(start)
    e = parse_date_strict(end)
    if s is None or e is None:
        raise ValueError("iter_windows 需要合法的 YYYY-MM-DD 日期")
    if s > e:
        raise ValueError(f"start({start}) 晚于 end({end})")
    if int(max_months) < 1:
        raise ValueError(f"max_months 必须 ≥1，收到 {max_months!r}")
    out: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        stop = min(_add_months(cur, int(max_months)), e)
        out.append((cur.isoformat(), stop.isoformat()))
        cur = stop + timedelta(days=1)
    return out


def _is_szse_no_data_row(rec: Any) -> bool:
    if not isinstance(rec, Mapping):
        return False
    raw = rec.get("日期")
    text = "" if raw is None else str(raw)
    return any(marker in text for marker in SZSE_NO_DATA_MARKERS)


class SzseWindowDays(dict):
    """`{YYYY-MM-DD: SourceDay}` + 取数失败的窗口区间。

    失败必须能被调用方看到：目标日没取到、而同期有窗口取数失败时，
    绝不能把「我们没取到」写成「源没有该日数据」。
    """

    def __init__(self) -> None:
        super().__init__()
        self.failed_windows: list[tuple[str, str, str]] = []

    def failure_for(self, day: str) -> str | None:
        """目标日落在某个失败窗口内 → 返回该窗口的原始错误。"""
        for win_start, win_end, msg in self.failed_windows:
            if win_start <= day <= win_end:
                return msg
        return None

    def failure_summary(self) -> str | None:
        if not self.failed_windows:
            return None
        return "; ".join(f"{s}~{e}: {msg}" for s, e, msg in self.failed_windows)


def fetch_szse_window(start: str, end: str, *, http: Callable[..., Any] | None = None,
                      retries: int = 3, timeout: float = 40.0) -> SzseWindowDays:
    """真实拉取深交所 `[start, end]` 的 ETF 份额，**自动按 ≤6 个日历月分窗回拼**。

    返回 `{YYYY-MM-DD: SourceDay}`（`SzseWindowDays`），包含各窗口内**源里实际出现的每一天**。
    缺失的日子不会出现在返回值里（调用方据此判 gap，而不是补零）；
    某个窗口取数失败时：该窗口起始日放一个 `fetch_error` 的 gap，
    同时把窗口区间记进 `failed_windows`，供调用方把错误传播到目标日。
    """
    out = SzseWindowDays()
    for win_start, win_end in iter_windows(start, end):
        params = {
            "SHOWTYPE": "xlsx",
            "CATALOGID": SZSE_CATALOGID,
            "TABKEY": "tab1",
            "txtStart": win_start,
            "txtEnd": win_end,
            "jjlb": "ETF",
            "random": "0.123456",
        }
        try:
            resp = _request(
                SZSE_URL, params=params, headers=SZSE_HEADERS,
                timeout=timeout, retries=retries, http=http,
            )
            records = _read_szse_xlsx(_resp_bytes(resp))
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            logger.warning("SZSE %s~%s 取数失败: %s", win_start, win_end, msg)
            out.failed_windows.append((win_start, win_end, msg))
            out[win_start] = SourceDay(
                market=MARKET_SZSE, requested_date=win_start,
                usable=False, gap_reason="fetch_error", fetch_error=msg,
            )
            continue

        sentinel_found = any(_is_szse_no_data_row(rec) for rec in records)
        if sentinel_found:
            # "没有找到符合条件的数据！" 是源在无数据时的占位行，不是基金行
            records = [rec for rec in records if not _is_szse_no_data_row(rec)]

        by_date: dict[str, list[Mapping[str, Any]]] = {}
        for rec in records:
            d = _norm_date(rec.get("日期") if isinstance(rec, Mapping) else None)
            if d:
                by_date.setdefault(d, []).append(rec)

        if not by_date:
            if sentinel_found or not records:
                reason = "empty_result"
                warn = (
                    f"窗口 {win_start}~{win_end} 源返回无数据占位行"
                    "（超出单次请求上限或该区间无数据）"
                )
            else:
                reason = "no_dated_rows"
                warn = f"窗口 {win_start}~{win_end} 有行但无任何可解析日期"
            out[win_start] = SourceDay(
                market=MARKET_SZSE, requested_date=win_start,
                usable=False, gap_reason=reason, warnings=[warn],
            )
            continue
        for d, recs in by_date.items():
            out[d] = parse_szse_records(recs, d)

    ordered = SzseWindowDays()
    ordered.failed_windows = out.failed_windows
    ordered.update(sorted(out.items()))
    return ordered


def _read_szse_xlsx(content: bytes) -> list[dict[str, Any]]:
    import pandas as pd  # 延迟 import

    df = pd.read_excel(io.BytesIO(content))
    df.columns = [str(c).strip() for c in df.columns]
    return df.to_dict("records")


def _szse_day_or_gap(sz_window: Mapping[str, SourceDay], target: str,
                     *, default_reason: str) -> SourceDay:
    """取目标日的深交所快照；没取到就把**取数失败**传播到目标日。

    只有「窗口都成功、源里确实没有这一天」才允许报 `no_rows_for_date`；
    只要有窗口取数失败，就不能把「我们没取到」写成「源无数据」。
    """
    day = sz_window.get(target)
    if day is not None:
        return day
    failure = failed_any = None
    if isinstance(sz_window, SzseWindowDays):
        failure = sz_window.failure_for(target)
        failed_any = sz_window.failure_summary()
    if failure or failed_any:
        detail = failure or failed_any
        return SourceDay(
            market=MARKET_SZSE, requested_date=target, usable=False,
            gap_reason="fetch_error", fetch_error=detail,
            warnings=[
                f"目标日 {target} 未取到，且同期有窗口取数失败（{detail}）："
                "无法区分『源无该日数据』与『我们取数失败』，按取数失败处理",
            ],
        )
    return SourceDay(
        market=MARKET_SZSE, requested_date=target,
        usable=False, gap_reason=default_reason,
    )


def dry_run(start: str, end: str, *, prev: str | None = None,
            http: Callable[..., Any] | None = None) -> dict[str, Any]:
    """端到端只读 dry-run：拉两日、聚合、给判断。**不写库、不写文件。**"""
    s = parse_date_strict(start)
    e = parse_date_strict(end)
    if s is None or e is None:
        raise ValueError("start/end 必须是合法的 YYYY-MM-DD 日期")
    if s > e:
        raise ValueError(f"start({start}) 晚于 end({end})")
    p = parse_date_strict(prev) if prev else None
    if prev and p is None:
        raise ValueError("--prev 必须是合法的 YYYY-MM-DD 日期")
    if p is not None and p >= e:
        raise ValueError(f"--prev({prev}) 必须早于 --date({end})")

    window_start = min(s, p) if p is not None else s
    end_str = e.isoformat()
    prev_str = p.isoformat() if p is not None else None

    cur_sse = fetch_sse_day(end_str, http=http)
    prev_sse = fetch_sse_day(prev_str, http=http) if prev_str else None

    sz_window = fetch_szse_window(window_start.isoformat(), end_str, http=http)
    cur_szse = _szse_day_or_gap(sz_window, end_str, default_reason="no_rows_for_date")
    prev_szse = (
        _szse_day_or_gap(sz_window, prev_str, default_reason="no_rows_for_date")
        if prev_str else None
    )

    days = {end_str: [cur_sse, cur_szse]}
    prev_days = {prev_str: [prev_sse, prev_szse]} if (prev_str and prev_sse and prev_szse) else None
    return build_dry_run(days, prev_days)


def cli_exit_code(report: Mapping[str, Any]) -> int:
    """0=完整且可用；1=不完整（含缺市场/缺前置/跨日）；2=参数/数据不可用。"""
    verdict = report.get("verdict") or {}
    if verdict.get("status") == "unusable" or not report.get("markets"):
        return EXIT_UNUSABLE
    if report.get("complete") is True and verdict.get("status") == "usable_for_daily_shares":
        return EXIT_COMPLETE
    return EXIT_INCOMPLETE


def _fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return EXIT_UNUSABLE


def main(argv: Sequence[str] | None = None, *,
         http: Callable[..., Any] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="市场级 ETF 日度份额替代源（沪深交易所，只读 dry-run，不写库）"
    )
    ap.add_argument("--date", required=True, help="目标交易日 YYYY-MM-DD（严格）")
    ap.add_argument("--prev", help="前一交易日 YYYY-MM-DD（给则算份额变动，必须早于 --date）")
    ap.add_argument("--start", help="深交所窗口起点，默认=--prev 或 --date")
    args = ap.parse_args(argv)

    d = parse_date_strict(args.date)
    if d is None:
        return _fail(f"--date 必须是严格的 YYYY-MM-DD 且为真实日历日，收到: {args.date!r}")

    p = None
    if args.prev:
        p = parse_date_strict(args.prev)
        if p is None:
            return _fail(f"--prev 必须是严格的 YYYY-MM-DD 且为真实日历日，收到: {args.prev!r}")
        if p >= d:
            return _fail(f"--prev({args.prev}) 必须早于 --date({args.date})")

    s = None
    if args.start:
        s = parse_date_strict(args.start)
        if s is None:
            return _fail(f"--start 必须是严格的 YYYY-MM-DD 且为真实日历日，收到: {args.start!r}")
        if s > d:
            return _fail(f"--start({args.start}) 不能晚于 --date({args.date})")

    try:
        report = dry_run(
            (s or p or d).isoformat(),
            d.isoformat(),
            prev=p.isoformat() if p else None,
            http=http,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转成非 0
        return _fail(f"dry-run 失败: {type(exc).__name__}: {exc}")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return cli_exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
