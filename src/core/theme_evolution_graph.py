"""题材—标的—事件可追溯证据图(纯函数层)。

## 这一层是什么

把调用者**已经核验过**的显式事件整理成结构化证据图, 供后续 API / 前端消费:

* 事件: 唯一 `event_id`、`source`、发生日 `occurred_on`、主题原文 `theme_text`、
  标的身分声明 `instrument{id, identity_status, identity_basis}`、可未知的 `published_at`;
* 可选日 K: 只用来回答"该标的在该日有没有一根 K 线", **价格一律不进入输出**;
* 可选独立交易日历: 显式传入的交易日序列, 是唯一允许用来判断"跨日是否连续"的依据。

节点四类: `cycle`(观测窗口) / `theme`(主题原文) / `instrument`(已核实标的) / `event`(事件)。
边只允许七类观察性关系(见 `EDGE_SEMANTICS`), 每条边自带 `not_claimed`,
写明它**不**主张因果、资金、主线、持续、买卖或价位。

## 硬边界(违反即 bug)

1. **纯函数**: 无 IO、无网络、无数据库、无时钟、不改写入参。`as_of` 必须由调用者
   显式给出; `generated_at` 只做透传, 不读系统时间。
2. **拒绝未来**: 事件日、K 线日、`published_at` 晚于 `as_of` 一律抛错, 不"截断"也不"跳过"。
3. **身份不猜**: 只有 `identity_status="verified"` 且带 `identity_basis` 的事件才连到
   `instrument` 节点。同名、同日、字符串相似、同一原始代码但状态未核实 —— 都不拼证券,
   只进 `unresolved` 与 `diagnostics`。主题只按**原文完全相等**合并, 不做相似度匹配。
4. **缺日历不许断言连续**: 没有交易日历时 `coverage.continuity="unknown"`,
   不生成周期窗口, 不生成窗口成员边, 不出现 `trading_days_between`;
   被抑制的关系数量在 `diagnostics.suppressed_relations` 里明写。
5. **断档靠日历**: 有日历时, 观测日之间缺的交易日逐日列出; 两个观测窗口之间必有
   `trading_days_between` 与 `unobserved_trading_days`。断档的成因是"这些交易日没有观测到事件"
   (可能是未采集), 不是"市场当时没有题材"。
6. **共现只在证据支持时成立**: 主题共现要求**同一已核实标的、同一日期**;
   不跨日、不跨标的、不跨窗口聚合。
7. **无交易语义**: 输出不含方向/收益/目标/仓位/资金/因果字段(`FORBIDDEN_OUTPUT_KEYS`),
   本层自带叙述过 `assert_no_trading_wording`。
8. **fail closed**: 日期格式、周末、重复 id、未切分标签(`+`)、缺失来源、非法日历
   (重复/乱序/含周末)、非法 K 线(非有限数/OHLC 自相矛盾/重复) —— 全部抛
   `GraphInputError`, 不"尽量算"。
9. **不硬编码历史案例**: 周期窗口完全由"观测日 + 交易日历"推导, 不带任何具体案例名,
   也不做主线/接力判定。

## 输入契约

`events`: `list[dict]`, 逐条:

    {"event_id": "R001-L0",                        # 必填, 全图唯一
     "occurred_on": "2026-01-05" | "20260105",     # 必填; 周末/未来/不在日历内即拒
     "theme_text": "钠电材料",                      # 必填; 原始标签, 含 '+' 视为未切分
     "source": "vendor-snapshot",                  # 必填
     "branch": "电池",                              # 可选; 调用者声明的题材分支, 不做推断
     "instrument": {"id": "002591",                 # 必填; 原样保留, 不补后缀/不改大小写
                    "name": "示例",                 # 可选, 仅供阅读, 绝不用于身份
                    "identity_status": "verified",  # verified / unverified / unknown
                    "identity_basis": "…"},         # verified 必填: 凭据说明
     "published_at": "unknown" | "2026-01-05T09:00:00+08:00",   # 可选, 可未知
     "reference": {"file": "…json", "row": 3, "sha256": "<64 hex>"}}  # 可选, 原样回显

`klines`: 可选 `list[dict]` —— `{"id"|"code", "date", "open", "high", "low", "close", "volume"}`,
数值必须有限、价格为正、成交量非负且 OHLC 自洽, 同一 (标的, 日期) 只允许一行。

`calendar`: 可选 `list[str]`, 独立交易日历; 必须严格升序、无重复、不含周末。
晚于 `as_of` 的日历项会被排除并计数(不参与连续性判断)。

`branch`: 只做调用者声明的分组汇总(`branches.declared`); 同一主题原文被声明到多个分支时
记入 `branches.conflicting`, 不挑一个; 未声明进 `branches.unknown`。

标识里含 `|`(节点/边 id 的分隔符)一律拒绝, 否则 `edge_id` 不可解析。
同一 (日期, 标的, 主题, 来源, 引用行) 出现多条时, 只在
`diagnostics.duplicate_observation_candidates` 里报数, 不静默合并、也不丢弃。

## 已知局限

* 未核验身份/未给日历/未给 K 线时, 对应维度一律 `unknown`, 不会用别的字段补。
* 主题合并按原文完全相等; 不同供应商对同一概念的措辞差异不会被识别(也不应在本层识别)。
* 交易日历为调用方输入，本层无法独立校验其来源或完整性；按该列表分段仅是相对结果。
* K 线身份关联只有在调用方明确提供 cross_source_identity_basis 时才尝试，依据真实性未核验；缺证据为 unknown，而非无K线。
* K 线只回答"有没有", 不提供复权、停牌、涨跌停可成交性信息。
* 事件来源的归因口径、修订历史、时点可见性均由调用者负责; 本层只区分
  `published_at` 已知/未知两种状态。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

SCHEMA_VERSION = "theme_evo_graph_v1"
RULE_VERSION = "theme_evo_graph_v1"

NODE_KINDS = ("cycle", "theme", "instrument", "event")

IDENTITY_VERIFIED = "verified"
IDENTITY_UNVERIFIED = "unverified"
IDENTITY_UNKNOWN = "unknown"
IDENTITY_STATUSES = (IDENTITY_VERIFIED, IDENTITY_UNVERIFIED, IDENTITY_UNKNOWN)

EDGE_TYPES = (
    "event_in_theme",
    "event_on_instrument",
    "theme_observed_on_instrument",
    "theme_copresent_same_instrument_same_day",
    "cycle_contains_theme",
    "cycle_contains_instrument",
    "cycle_precedes_cycle",
)

EDGE_SEMANTICS = {
    "event_in_theme": "EXACT_RAW_TEXT_MEMBERSHIP",
    "event_on_instrument": "EXPLICIT_VERIFIED_IDENTITY",
    "theme_observed_on_instrument": "OBSERVED_SAME_DAY_LABEL_ON_VERIFIED_INSTRUMENT",
    "theme_copresent_same_instrument_same_day": "SAME_DAY_CO_PRESENCE_ON_VERIFIED_INSTRUMENT",
    "cycle_contains_theme": "OBSERVATION_WINDOW_MEMBERSHIP",
    "cycle_contains_instrument": "OBSERVATION_WINDOW_MEMBERSHIP",
    "cycle_precedes_cycle": "TEMPORAL_ORDER_ONLY",
}

#: 每条边都明写"不主张什么", 也用于断言输出里没有这些概念。
NOT_CLAIMED = (
    "causality",
    "capital_flow",
    "price_direction",
    "return_forecast",
    "buy_sell",
    "price_target",
    "mainline_status",
    "theme_persistence",
    "theme_handoff",
    "market_cycle",
    "identity_inference",
)

CLAIMS = {
    "causality": "not_inferred",
    "capital_flow": "not_used",
    "price_direction": "not_provided",
    "return_forecast": "not_provided",
    "buy_sell": "not_provided",
    "price_target": "not_provided",
    "mainline_status": "not_asserted",
    "theme_persistence": "not_asserted",
    "theme_handoff": "not_asserted",
    "market_cycle": "not_asserted",
    "identity_inference": "not_used",
    "hardcoded_case": "not_used",
}

#: 输出里绝不允许出现的字段名(方向/收益/目标/仓位/资金/因果)。
FORBIDDEN_OUTPUT_KEYS = frozenset({
    "signal", "signal_score", "direction", "buy", "sell", "recommend",
    "recommendation", "target_price", "expected_return", "forward_return",
    "future_return", "return_fwd", "alpha", "position", "position_size",
    "weight", "score", "confidence", "caused_by", "causal", "flow",
    "capital_flow", "inflow", "outflow", "net_buy", "qfq", "hfq",
    "pct_chg", "change_pct", "win_rate", "limit_up_prob",
})

#: 本层自己写的叙述里禁止出现的交易措辞(来源标签原文不受此限)。
FORBIDDEN_WORDS = (
    "建议买入", "可买入", "建议卖出", "建议加仓", "建议减仓", "必买",
    "推荐买入", "看多", "看空", "抄底", "追高", "止盈",
)

#: 供应商常把多个题材用 `+` 串在一个字段里; 未切分的原始串不许当单个主题存。
THEME_SEPARATORS = ("+", "\uff0b")

LIMITATIONS = (
    "身份只在调用者声明 identity_status=verified 且有 identity_basis 时成立; 本层不核验任何证券身份, "
    "也不按名称、日期或相似字符串连接标的。",
    "未提供交易日历时无法判断跨日是否连续: 输出 continuity=unknown, 不生成观测窗口与窗口成员边, "
    "并显式列出被抑制的关系数量。",
    "断档的成因只能是'这些交易日没有观测到事件'; 采集是否完整、市场当时是否安静都无法从本层推出。",
    "主题只按原文完全相等合并; 不同来源的措辞差异不会被识别, 同一原文跨来源合并会记录每个来源的条数。",
    "K线只回答该标在该日是否存在一根K线, 价格在通过一致性校验后被丢弃, 不产生收益、复权或可成交性判断。",
    "published_at 未知时一律 unknown, 不接受自报的盘中可见性; 时点可回放性未证实。",
    "周期窗口是'连续观测到的交易日窗口'(OBSERVATION_WINDOW_ONLY), 不是对市场大周期的判定。",
    "所有关系都是观察性的: 不含因果、资金流、主线、接力或任何买卖、价位含义。",
    "同日同标的的主题共现边数随该标的当日标签数呈组合增长(C(n,2)); 数量本身不是强度。",
)

_DATE_RE = re.compile(r"^(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_UNKNOWN_TOKENS = frozenset({"", "unknown", "未知", "none", "null"})

_UNOBSERVED_CAP = 90
_EVIDENCE_CAP_DEFAULT = 5

#: 节点/边 id 用 `|` 和 `:` 做分隔; 原始标识里带 `|` 会让 id 不可解析, 直接拒绝。
RESERVED_ID_CHARS = ("|",)


_KIND_ORDER = {kind: idx for idx, kind in enumerate(NODE_KINDS)}
_EDGE_ORDER = {etype: idx for idx, etype in enumerate(EDGE_TYPES)}


class GraphInputError(ValueError):
    """输入不可用时抛这个(带稳定 code, 便于调用方/测试断言)。"""

    def __init__(self, code: str, message: str, *, detail: Mapping[str, Any] | None = None):
        self.code = code
        self.detail = dict(detail or {})
        super().__init__(f"[{code}] {message}")


def _fail(code: str, message: str, **detail: Any) -> GraphInputError:
    return GraphInputError(code, message, detail=detail)


def _reject_reserved(value: str, code: str, where: str, **detail: Any) -> str:
    """节点/边 id 的分隔符不允许出现在原始标识里, 否则 id 不可解析。"""
    hits = [ch for ch in RESERVED_ID_CHARS if ch in value]
    if hits:
        raise _fail(code, f"{where} 含保留分隔符 {hits}: {value!r}", **detail)
    return value


# ---------------------------------------------------------------- 日期
def parse_calendar_date(value: Any, *, field: str = "date") -> date:
    """严格解析自然日: 只接受 `YYYYMMDD` / `YYYY-MM-DD`, 不做任何"智能"修补。"""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise _fail("invalid_date_format",
                    f"{field} 必须是 YYYYMMDD 或 YYYY-MM-DD, 实得 {value!r}",
                    field=field, value=repr(value))
    compact = value.replace("-", "")
    try:
        parsed = datetime.strptime(compact, "%Y%m%d").date()
    except ValueError as exc:
        raise _fail("invalid_date_format", f"{field} 不是真实日历日: {value!r} ({exc})",
                    field=field, value=repr(value)) from exc
    if parsed.strftime("%Y%m%d") != compact:
        raise _fail("invalid_date_format", f"{field} 不是真实日历日: {value!r}",
                    field=field, value=repr(value))
    return parsed


def parse_trade_date(value: Any, *, field: str = "date") -> date:
    """严格解析交易日: 在自然日基础上再拒绝周末(A 股无周末交易)。"""
    parsed = parse_calendar_date(value, field=field)
    if parsed.weekday() >= 5:
        raise _fail("weekend_date", f"{field} 落在周末({parsed.isoformat()}), A 股无交易",
                    field=field, value=repr(value), weekday=parsed.weekday())
    return parsed


# ---------------------------------------------------------------- 校验小工具
def _require_clean_str(value: Any, code: str, message: str, **detail: Any) -> str:
    if not isinstance(value, str):
        raise _fail(code, message, **detail)
    if not value or value != value.strip():
        raise _fail(code, message, **detail)
    return value


def _require_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail("invalid_kline_value", f"{field} 必须是数值, 实得 {value!r}",
                    field=field, value=repr(value))
    num = float(value)
    if not math.isfinite(num):
        raise _fail("invalid_kline_value", f"{field} 必须是有限数, 实得 {value!r}",
                    field=field, value=repr(value))
    return num


def _iso(day: date) -> str:
    return day.isoformat()


def _capped(items: list[Any], cap: int) -> tuple[list[Any], int, bool]:
    total = len(items)
    return items[:cap], total, total > cap


# ---------------------------------------------------------------- 事件
def _normalize_instrument(raw: Mapping[str, Any], where: str, event_id: str) -> dict:
    inst = raw.get("instrument")
    if not isinstance(inst, Mapping):
        raise _fail("missing_instrument",
                    f"{where}.instrument 缺失: 事件必须携带标的身份声明",
                    field=f"{where}.instrument", event_id=event_id)

    ident = inst.get("id")
    alias = inst.get("code")
    if ident is not None and alias is not None and ident != alias:
        raise _fail("instrument_id_conflict",
                    f"{where}.instrument.id 与 .code 不一致: {ident!r} != {alias!r}",
                    field=f"{where}.instrument", event_id=event_id)
    instrument_id = _require_clean_str(
        ident if ident is not None else alias,
        "invalid_instrument_id",
        f"{where}.instrument.id 必须是非空且无空白的字符串(不做后缀推断/大小写归一)",
        field=f"{where}.instrument.id", event_id=event_id)
    _reject_reserved(instrument_id, "reserved_separator_in_instrument_id",
                     f"{where}.instrument.id", event_id=event_id)

    status = inst.get("identity_status")
    if status is None:
        raise _fail("missing_identity_status",
                    f"{where}.instrument.identity_status 缺失: 身份状态不能默认",
                    field=f"{where}.instrument.identity_status", event_id=event_id)
    if status not in IDENTITY_STATUSES:
        raise _fail("invalid_identity_status",
                    f"{where}.instrument.identity_status={status!r} 不在 {list(IDENTITY_STATUSES)}",
                    field=f"{where}.instrument.identity_status", event_id=event_id)

    name = inst.get("name")
    if name is not None and not isinstance(name, str):
        raise _fail("invalid_instrument_name", f"{where}.instrument.name 必须是字符串或 null",
                    field=f"{where}.instrument.name", event_id=event_id)

    basis = inst.get("identity_basis")
    if status == IDENTITY_VERIFIED:
        if not isinstance(basis, str) or not basis.strip():
            raise _fail("missing_identity_basis",
                        f"{where}.instrument.identity_basis 缺失: 声明 verified 必须给出核验依据",
                        field=f"{where}.instrument.identity_basis", event_id=event_id)
    elif basis is not None and not isinstance(basis, str):
        raise _fail("invalid_identity_basis", f"{where}.instrument.identity_basis 必须是字符串或 null",
                    field=f"{where}.instrument.identity_basis", event_id=event_id)

    return {"id": instrument_id, "name": name, "identity_status": status,
            "identity_basis": basis}


def _normalize_published(value: Any, where: str, as_of: date,
                         event_id: str) -> tuple[str | None, str, date | None]:
    if value is None:
        return None, "unknown", None
    if isinstance(value, str) and value.strip().lower() in _UNKNOWN_TOKENS:
        return None, "unknown", None
    if not isinstance(value, str):
        raise _fail("invalid_published_at",
                    f"{where}.published_at 必须是 ISO 时间串或 unknown, 实得 {value!r}",
                    field=f"{where}.published_at", event_id=event_id)
    text = value.strip()
    try:
        if "T" in text or " " in text:
            parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            day = parsed.date()
        else:
            day = parse_calendar_date(text, field=f"{where}.published_at")
    except ValueError as exc:
        raise _fail("invalid_published_at",
                    f"{where}.published_at 无法解析: {value!r} ({exc})",
                    field=f"{where}.published_at", event_id=event_id) from exc
    if day > as_of:
        raise _fail("future_published_at",
                    f"{where}.published_at={text!r} 晚于 as_of={_iso(as_of)}",
                    field=f"{where}.published_at", event_id=event_id, date=_iso(day))
    return text, "known", day


def _normalize_reference(raw: Any, where: str, event_id: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _fail("invalid_reference", f"{where}.reference 必须是 mapping 或 null",
                    field=f"{where}.reference", event_id=event_id)
    file = _require_clean_str(raw.get("file"), "invalid_reference_file",
                              f"{where}.reference.file 必须是非空字符串",
                              field=f"{where}.reference.file", event_id=event_id)
    row = raw.get("row")
    if row is not None and (isinstance(row, bool) or not isinstance(row, int) or row < 0):
        raise _fail("invalid_reference_row",
                    f"{where}.reference.row 必须是 >=0 的整数, 实得 {row!r}",
                    field=f"{where}.reference.row", event_id=event_id)
    sha = raw.get("sha256")
    if sha is not None and (not isinstance(sha, str) or not _SHA_RE.match(sha)):
        raise _fail("invalid_reference_sha256",
                    f"{where}.reference.sha256 必须是 64 位小写十六进制, 实得 {sha!r}",
                    field=f"{where}.reference.sha256", event_id=event_id)
    return {"file": file, "row": row, "sha256": sha}


def _normalize_events(events: Any, as_of: date) -> list[dict]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise _fail("invalid_events_container", "events 必须是 list/tuple")
    if not events:
        raise _fail("events_empty", "events 为空: 无证据可建图(不伪造节点)")

    out: list[dict] = []
    seen: set[str] = set()
    for idx, raw in enumerate(events):
        where = f"events[{idx}]"
        if not isinstance(raw, Mapping):
            raise _fail("invalid_event", f"{where} 必须是 mapping", field=where)

        event_id = _require_clean_str(raw.get("event_id"), "invalid_event_id",
                                      f"{where}.event_id 必须是非空且无空白的字符串",
                                      field=f"{where}.event_id")
        _reject_reserved(event_id, "reserved_separator_in_event_id",
                         f"{where}.event_id", event_id=event_id)
        if event_id in seen:
            raise _fail("duplicate_event_id",
                        f"重复事件 id: {event_id!r} —— 事件 id 必须全图唯一",
                        event_id=event_id)
        seen.add(event_id)

        day = parse_trade_date(raw.get("occurred_on"), field=f"{where}.occurred_on")
        if day > as_of:
            raise _fail("future_event_date",
                        f"{where}.occurred_on={_iso(day)} 晚于 as_of={_iso(as_of)}",
                        field=f"{where}.occurred_on", event_id=event_id, date=_iso(day))

        theme = _require_clean_str(raw.get("theme_text"), "invalid_theme_text",
                                   f"{where}.theme_text 必须是非空且无空白的原文标签",
                                   field=f"{where}.theme_text", event_id=event_id)
        if any(sep in theme for sep in THEME_SEPARATORS):
            raise _fail("theme_text_not_split",
                        f"{where}.theme_text 未切分(含 '+'): {theme!r}",
                        field=f"{where}.theme_text", event_id=event_id)
        _reject_reserved(theme, "reserved_separator_in_theme_text",
                         f"{where}.theme_text", event_id=event_id)

        source = _require_clean_str(raw.get("source"), "invalid_source",
                                    f"{where}.source 必须是非空来源标识",
                                    field=f"{where}.source", event_id=event_id)

        branch = raw.get("branch")
        if branch is not None:
            branch = _require_clean_str(branch, "invalid_branch",
                                        f"{where}.branch 必须是字符串或 null",
                                        field=f"{where}.branch", event_id=event_id)

        instrument = _normalize_instrument(raw, where, event_id)
        published_at, visibility, published_date = _normalize_published(
            raw.get("published_at"), where, as_of, event_id)
        reference = _normalize_reference(raw.get("reference"), where, event_id)

        out.append({
            "event_id": event_id, "date": day, "date_iso": _iso(day),
            "date_compact": day.strftime("%Y%m%d"), "theme_text": theme,
            "source": source, "branch": branch, "instrument": instrument,
            "published_at": published_at, "visibility": visibility,
            "published_date": published_date, "reference": reference,
        })
    return out


# ---------------------------------------------------------------- 交易日历
def _normalize_calendar(calendar: Any, as_of: date) -> dict | None:
    if calendar is None:
        return None
    if not isinstance(calendar, Sequence) or isinstance(calendar, (str, bytes)):
        raise _fail("invalid_calendar", "calendar 必须是 list/tuple 或 None")
    if not calendar:
        raise _fail("calendar_empty", "calendar 为空: 空日历不能当作'没有交易日'")

    days: list[date] = []
    for idx, raw in enumerate(calendar):
        try:
            day = parse_trade_date(raw, field=f"calendar[{idx}]")
        except GraphInputError as exc:
            code = ("calendar_weekend_entry" if exc.code == "weekend_date"
                    else "invalid_calendar_date")
            raise _fail(code, str(exc), field=f"calendar[{idx}]") from exc
        days.append(day)
    if len(set(days)) != len(days):
        dup = sorted({_iso(d) for d in days if days.count(d) > 1})
        raise _fail("duplicate_calendar_date", f"calendar 有重复日期: {dup}", dates=dup)
    for prev, nxt in zip(days, days[1:]):
        if nxt <= prev:
            raise _fail("calendar_not_ascending",
                        f"calendar 必须严格升序: {_iso(prev)} -> {_iso(nxt)}",
                        prev=_iso(prev), next=_iso(nxt))

    effective = [d for d in days if d <= as_of]
    ignored = [d for d in days if d > as_of]
    if not effective:
        raise _fail("calendar_empty_until_as_of",
                    f"calendar 在 as_of={_iso(as_of)} 之前没有任何交易日",
                    as_of=_iso(as_of))
    return {
        "dates": effective,
        "index": {d: i for i, d in enumerate(effective)},
        "date_set": set(effective),
        "entries": len(days),
        "excluded_after_as_of": len(ignored),
        "excluded_dates": [_iso(d) for d in ignored],
        "first": _iso(effective[0]),
        "last": _iso(effective[-1]),
    }


# ---------------------------------------------------------------- K线(只保留"有没有")
def _normalize_klines(klines: Any, cal: dict | None, as_of: date) -> list[dict] | None:
    if klines is None:
        return None
    if not isinstance(klines, Sequence) or isinstance(klines, (str, bytes)):
        raise _fail("invalid_klines_container", "klines 必须是 list/tuple 或 None")
    if not klines:
        return None  # 空列表等同未提供, 并会在 coverage 里体现

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for idx, raw in enumerate(klines):
        where = f"klines[{idx}]"
        if not isinstance(raw, Mapping):
            raise _fail("invalid_kline", f"{where} 必须是 mapping", field=where)

        ident = raw.get("id")
        alias = raw.get("code")
        if ident is not None and alias is not None and ident != alias:
            raise _fail("instrument_id_conflict",
                        f"{where}.id 与 .code 不一致: {ident!r} != {alias!r}",
                        field=where)
        instrument_id = _require_clean_str(
            ident if ident is not None else alias, "invalid_kline_instrument_id",
            f"{where}.id 必须是非空且无空白的字符串", field=f"{where}.id")

        day = parse_trade_date(raw.get("date"), field=f"{where}.date")
        if day > as_of:
            raise _fail("future_kline_date",
                        f"{where}.date={_iso(day)} 晚于 as_of={_iso(as_of)}",
                        field=f"{where}.date", date=_iso(day))
        if cal is not None and day not in cal["date_set"]:
            raise _fail("bar_date_not_in_calendar",
                        f"{where}.date={_iso(day)} 不在已提供的交易日历里",
                        field=f"{where}.date", date=_iso(day))

        open_ = _require_number(raw.get("open"), f"{where}.open")
        high = _require_number(raw.get("high"), f"{where}.high")
        low = _require_number(raw.get("low"), f"{where}.low")
        close = _require_number(raw.get("close"), f"{where}.close")
        volume = _require_number(raw.get("volume"), f"{where}.volume")
        if min(open_, high, low, close) <= 0:
            raise _fail("invalid_kline_value", f"{where} 价格必须为正", field=where)
        if volume < 0:
            raise _fail("invalid_kline_value", f"{where}.volume 不能为负: {volume}", field=where)
        if high < max(open_, close, low) or low > min(open_, close):
            raise _fail("invalid_kline_value",
                        f"{where} OHLC 自相矛盾: o={open_} h={high} l={low} c={close}",
                        field=where)

        key = (instrument_id, _iso(day))
        if key in seen:
            raise _fail("duplicate_kline",
                        f"重复K线(instrument={instrument_id}, date={key[1]})",
                        instrument_id=instrument_id, date=key[1])
        seen.add(key)
        basis = raw.get("cross_source_identity_basis")
        # 非空依据仅为调用者声明，图层不会独立核实其真实性。
        identity_declared = isinstance(basis, str) and bool(basis.strip())
        out.append({"instrument_id": instrument_id, "date": day, "date_iso": key[1],
                    "cross_source_identity_declared": identity_declared,
                    "cross_source_identity_basis": basis if identity_declared else None})
    return out


# ---------------------------------------------------------------- 分段(只靠日历)
def _build_segments(observed: list[date], cal: dict | None) -> list[dict]:
    def make(segment_id: str, days: list[date]) -> dict:
        return {
            "segment_id": segment_id,
            "start": _iso(days[0]), "end": _iso(days[-1]),
            "observed_dates": [_iso(d) for d in days],
            "observed_day_count": len(days),
            "calendar_verified": False,
            "continuity": "unknown",
            "window_trading_days": None,
            "unobserved_trading_days": None,
            "unobserved_trading_day_count": None,
            "break_after_previous": None,
        }

    if cal is None:
        return [make("seg-0001", observed)]

    groups: list[list[date]] = [[observed[0]]]
    idx = cal["index"]
    for day in observed[1:]:
        if idx[day] == idx[groups[-1][-1]] + 1:
            groups[-1].append(day)
        else:
            groups.append([day])

    segments: list[dict] = []
    for i, days in enumerate(groups):
        seg = make(f"seg-{i + 1:04d}", days)
        start_idx, end_idx = idx[days[0]], idx[days[-1]]
        observed_set = set(days)
        inside = [cal["dates"][k] for k in range(start_idx, end_idx + 1)
                  if cal["dates"][k] not in observed_set]
        dates_capped, total, truncated = _capped([_iso(d) for d in inside], _UNOBSERVED_CAP)
        seg.update({
            "calendar_verified": False,
            "calendar_basis": "supplied_by_caller_unverified",
            "continuity": "relative_to_supplied_calendar",
            "window_trading_days": end_idx - start_idx + 1,
            "unobserved_trading_days": dates_capped,
            "unobserved_trading_day_count": total,
            "unobserved_trading_days_truncated": truncated,
        })
        if i:
            prev_days = groups[i - 1]
            prev_idx, start = idx[prev_days[-1]], start_idx
            between = [cal["dates"][k] for k in range(prev_idx + 1, start)]
            between_capped, between_total, between_truncated = _capped(
                [_iso(d) for d in between], _UNOBSERVED_CAP)
            seg["break_after_previous"] = {
                "kind": "no_observed_events_on_intervening_trading_days",
                "calendar_basis": "supplied_by_caller_unverified",
                "trading_days_between": start - prev_idx,
                "unobserved_trading_days": between_capped,
                "unobserved_trading_day_count": between_total,
                "unobserved_trading_days_truncated": between_truncated,
                "coverage_gap_possible": True,
            }
        segments.append(seg)
    return segments


# ---------------------------------------------------------------- 主函数
def build_evidence_graph(events: Any, *, klines: Any = None, calendar: Any = None,
                         as_of: Any, generated_at: str | None = None,
                         evidence_cap: int = _EVIDENCE_CAP_DEFAULT) -> dict:
    """显式事件 (+ 可选K线 + 可选交易日历) → 结构化证据图。输入不合法即抛错。"""
    try:
        as_of_date = parse_calendar_date(as_of, field="as_of")
    except GraphInputError as exc:
        raise _fail("invalid_as_of",
                    f"as_of 必须是 YYYYMMDD 或 YYYY-MM-DD 的真实日期, 实得 {as_of!r}",
                    value=repr(as_of)) from exc

    if generated_at is not None:
        ok = isinstance(generated_at, str) and "T" in generated_at
        if ok:
            try:
                datetime.fromisoformat(generated_at)
            except ValueError:
                ok = False
        if not ok:
            raise _fail("invalid_generated_at",
                        f"generated_at 必须是 ISO 时间串(含 T)或 null, 实得 {generated_at!r}",
                        value=repr(generated_at))

    if isinstance(evidence_cap, bool) or not isinstance(evidence_cap, int) or evidence_cap < 1:
        raise _fail("invalid_evidence_cap",
                    f"evidence_cap 必须是 >=1 的整数, 实得 {evidence_cap!r}",
                    value=repr(evidence_cap))

    evs = _normalize_events(events, as_of_date)
    cal = _normalize_calendar(calendar, as_of_date)
    if cal is not None:
        for ev in evs:
            if ev["date"] not in cal["date_set"]:
                raise _fail("event_date_not_in_calendar",
                            f"事件 {ev['event_id']} 的发生日 {ev['date_iso']} 不在已提供的交易日历里",
                            event_id=ev["event_id"], date=ev["date_iso"])
    bars = _normalize_klines(klines, cal, as_of_date)
    bar_index = {(b["instrument_id"], b["date_iso"]): b for b in bars or []
                 if b["cross_source_identity_declared"]}

    observed = sorted({ev["date"] for ev in evs})
    segments = _build_segments(observed, cal)
    segment_of = {day: seg["segment_id"] for seg in segments
                  for day in (date.fromisoformat(d) for d in seg["observed_dates"])}

    # ------------------------------------------------ 聚合桶
    themes: dict[str, dict] = {}
    instruments: dict[str, dict] = {}
    statuses_by_id: dict[str, set[str]] = {}
    ids_by_name: dict[str, set[str]] = {}
    window_themes: dict[tuple[str, str], list[dict]] = {}
    window_instruments: dict[tuple[str, str], list[dict]] = {}
    theme_instrument: dict[tuple[str, str], list[dict]] = {}
    copresence: dict[tuple[str, str, str], list[dict]] = {}
    verified_instruments: set[str] = set()

    for ev in evs:
        theme = ev["theme_text"]
        inst = ev["instrument"]
        verified = inst["identity_status"] == IDENTITY_VERIFIED

        slot = themes.setdefault(theme, {
            "sources": {}, "event_ids": [], "dates": set(), "branches": {},
            "instruments": set(), "segments": set(),
        })
        slot["sources"][ev["source"]] = slot["sources"].get(ev["source"], 0) + 1
        slot["event_ids"].append(ev["event_id"])
        slot["dates"].add(ev["date"])
        slot["segments"].add(segment_of[ev["date"]])
        if ev["branch"]:
            slot["branches"][ev["branch"]] = slot["branches"].get(ev["branch"], 0) + 1
        if verified:
            slot["instruments"].add(inst["id"])

        statuses_by_id.setdefault(inst["id"], set()).add(inst["identity_status"])
        if inst["name"]:
            ids_by_name.setdefault(inst["name"], set()).add(inst["id"])

        if verified:
            verified_instruments.add(inst["id"])
            inode = instruments.setdefault(inst["id"], {
                "names": set(), "bases": set(), "event_ids": [], "dates": set(),
                "themes": set(), "sources": set(), "segments": set(),
            })
            if inst["name"]:
                inode["names"].add(inst["name"])
            if inst["identity_basis"]:
                inode["bases"].add(inst["identity_basis"])
            inode["event_ids"].append(ev["event_id"])
            inode["dates"].add(ev["date"])
            inode["themes"].add(theme)
            inode["sources"].add(ev["source"])
            inode["segments"].add(segment_of[ev["date"]])
            theme_instrument.setdefault((theme, inst["id"]), []).append(ev)

        window_themes.setdefault((segment_of[ev["date"]], theme), []).append(ev)
        if verified:
            window_instruments.setdefault(
                (segment_of[ev["date"]], inst["id"]), []).append(ev)

    # 同日同标的的主题共现(只在已核实标的上成立)
    for ev in evs:
        inst = ev["instrument"]
        if inst["identity_status"] != IDENTITY_VERIFIED:
            continue
        copresence.setdefault(
            (inst["id"], ev["date_iso"], ev["theme_text"]), []).append(ev)

    # ------------------------------------------------ 节点
    nodes: list[dict] = []
    node_counts = {kind: 0 for kind in NODE_KINDS}

    cycle_nodes: list[dict] = []
    if cal is not None:
        for seg in segments:
            seg_themes = sorted({t for (sid, t) in window_themes if sid == seg["segment_id"]})
            seg_instruments = sorted({i for (sid, i) in window_instruments
                                      if sid == seg["segment_id"]})
            seg_events = [ev for ev in evs if segment_of[ev["date"]] == seg["segment_id"]]
            cycle_nodes.append({
                "node_id": f"cycle:{seg['segment_id']}",
                "kind": "cycle",
                "label": f"{seg['start']}~{seg['end']} 观测窗口",
                "segment_id": seg["segment_id"],
                "window": {
                    "calendar_basis": "supplied_by_caller_unverified",
                    "start": seg["start"], "end": seg["end"],
                    "observed_trading_days": seg["observed_day_count"],
                    "trading_days": seg["window_trading_days"],
                    "unobserved_trading_days": seg["unobserved_trading_days"],
                    "unobserved_trading_day_count": seg["unobserved_trading_day_count"],
                },
                "observed_dates": seg["observed_dates"],
                "event_count": len(seg_events),
                "theme_ids": [f"theme:{t}" for t in seg_themes],
                "instrument_ids": [f"instrument:{i}" for i in seg_instruments],
                "interpretation": "OBSERVATION_WINDOW_ONLY",
                "market_cycle_claim": "not_asserted",
            })
        nodes.extend(cycle_nodes)

    theme_nodes: list[dict] = []
    for theme in sorted(themes):
        slot = themes[theme]
        branches = sorted(slot["branches"])
        if not branches:
            branch, branch_status = None, "unknown"
        elif len(branches) == 1:
            branch, branch_status = branches[0], "declared"
        else:
            branch, branch_status = None, "conflicting_declarations"
        dates = sorted(slot["dates"])
        seg_ids = sorted(slot["segments"])
        theme_nodes.append({
            "node_id": f"theme:{theme}",
            "kind": "theme",
            "label": theme,
            "raw_label": theme,
            "sources": dict(sorted(slot["sources"].items())),
            "source_count": len(slot["sources"]),
            "event_count": len(slot["event_ids"]),
            "observed_dates": [_iso(d) for d in dates],
            "first_seen": _iso(dates[0]), "last_seen": _iso(dates[-1]),
            "branch": branch,
            "branch_status": branch_status,
            "instruments": sorted(slot["instruments"]),
            "instrument_count": len(slot["instruments"]),
            "segments": seg_ids,
            "crosses_segment_break": len(seg_ids) > 1,
        })
    theme_nodes.sort(key=lambda n: n["node_id"])
    nodes.extend(theme_nodes)

    # 题材分支只做"调用者声明"的分组汇总, 不推断、不补全
    declared_branches: dict[str, dict] = {}
    unknown_branch: list[str] = []
    conflicting_branch: list[str] = []
    for item in theme_nodes:
        if item["branch_status"] == "declared":
            slot = declared_branches.setdefault(item["branch"], {"themes": [], "events": 0})
            slot["themes"].append(item["label"])
            slot["events"] += item["event_count"]
        elif item["branch_status"] == "conflicting_declarations":
            conflicting_branch.append(item["label"])
        else:
            unknown_branch.append(item["label"])
    branch_summary = {
        "declared": [
            {"branch": name, "themes": sorted(slot["themes"]),
             "theme_count": len(slot["themes"]), "events": slot["events"]}
            for name, slot in sorted(declared_branches.items())
        ],
        "unknown": sorted(unknown_branch),
        "conflicting": sorted(conflicting_branch),
        "inference": "not_performed",
    }

    instrument_nodes: list[dict] = []
    for ident in sorted(instruments):
        slot = instruments[ident]
        dates = sorted(slot["dates"])
        bar_dates = {b["date"] for b in bars or [] if b["instrument_id"] == ident
                     and b["cross_source_identity_declared"]}
        instrument_nodes.append({
            "node_id": f"instrument:{ident}",
            "kind": "instrument",
            "id": ident,
            "display_names": sorted(slot["names"]),
            "identity_status": IDENTITY_VERIFIED,
            "identity_independently_verified": False,
            "identity_bases": sorted(slot["bases"]),
            "event_ids": list(slot["event_ids"]),
            "theme_ids": [f"theme:{t}" for t in sorted(slot["themes"])],
            "sources": sorted(slot["sources"]),
            "first_seen": _iso(dates[0]), "last_seen": _iso(dates[-1]),
            "segments": sorted(slot["segments"]),
            "bars_seen": len(bar_dates) if bar_dates else None,
        })
    nodes.extend(instrument_nodes)

    event_nodes: list[dict] = []
    for ev in evs:
        inst = ev["instrument"]
        verified = inst["identity_status"] == IDENTITY_VERIFIED
        if bars is None or not verified:
            bar_present = None
        else:
            bar_present = True if (inst["id"], ev["date_iso"]) in bar_index else None
        event_nodes.append({
            "node_id": f"event:{ev['event_id']}",
            "kind": "event",
            "event_id": ev["event_id"],
            "date": ev["date_iso"],
            "date_compact": ev["date_compact"],
            "source": ev["source"],
            "theme_id": f"theme:{ev['theme_text']}",
            "theme_text": ev["theme_text"],
            "branch": ev["branch"],
            "branch_status": "declared" if ev["branch"] else "unknown",
            "instrument": {
                "id": inst["id"],
                "name": inst["name"],
                "identity_status": inst["identity_status"],
                "identity_basis": inst["identity_basis"],
                "node_id": f"instrument:{inst['id']}" if verified else None,
            },
            "identity_resolved": verified,
            "identity_independently_verified": False,  # 仅调用方声明，图层不验证来源
            "published_at": ev["published_at"] or "unknown",
            "visibility": ("published_at_known" if ev["visibility"] == "known"
                           else "published_at_unknown"),
            "bar_present": bar_present,
            "bar_identity_association": ("caller_declared_unverified" if bar_present else "unknown"),
            "bar_identity_basis": (bar_index[(inst["id"], ev["date_iso"])]["cross_source_identity_basis"]
                                   if bar_present else None),
            "bar_absence_interpretation": (
                "unknown_not_suspension" if bar_present is False else None),
            "segment_id": segment_of[ev["date"]],
            "reference": ev["reference"],
        })
    event_nodes.sort(key=lambda n: (n["date"], n["event_id"]))
    nodes.extend(event_nodes)

    nodes.sort(key=lambda n: (_KIND_ORDER[n["kind"]], n["node_id"]))
    for item in nodes:
        node_counts[item["kind"]] += 1

    # ------------------------------------------------ 边
    edges: list[dict] = []

    def add_edge(etype: str, src: str, dst: str, *, support: dict,
                 evidence: list[dict]) -> None:
        if etype in {"cycle_contains_theme", "cycle_contains_instrument", "cycle_precedes_cycle"}:
            support = {**support, "calendar_basis": "supplied_by_caller_unverified"}
        evidence = sorted(evidence, key=lambda e: (
            e.get("date") or "", e.get("event_id") or "", e.get("instrument_id") or ""))
        shown, total, truncated = _capped(evidence, evidence_cap)
        edges.append({
            "edge_id": f"{etype}|{src}|{dst}",
            "type": etype,
            "semantics": EDGE_SEMANTICS[etype],
            "src": src,
            "dst": dst,
            "support": support,
            "evidence": shown,
            "evidence_total": total,
            "evidence_truncated": truncated,
            "not_claimed": list(NOT_CLAIMED),
        })

    for ev in evs:
        src = f"event:{ev['event_id']}"
        add_edge("event_in_theme", src, f"theme:{ev['theme_text']}",
                 support={"events": 1, "dates": 1,
                          "first_seen": ev["date_iso"], "last_seen": ev["date_iso"]},
                 evidence=[{"event_id": ev["event_id"], "date": ev["date_iso"],
                            "source": ev["source"]}])
        if ev["instrument"]["identity_status"] == IDENTITY_VERIFIED:
            add_edge("event_on_instrument", src, f"instrument:{ev['instrument']['id']}",
                     support={"events": 1, "dates": 1,
                              "first_seen": ev["date_iso"], "last_seen": ev["date_iso"]},
                     evidence=[{"event_id": ev["event_id"], "date": ev["date_iso"],
                                "source": ev["source"]}])

    for (theme, ident), rows in sorted(theme_instrument.items()):
        dates = sorted({row["date"] for row in rows})
        add_edge("theme_observed_on_instrument", f"theme:{theme}", f"instrument:{ident}",
                 support={"events": len(rows), "dates": len(dates),
                          "sources": sorted({row["source"] for row in rows}),
                          "first_seen": _iso(dates[0]), "last_seen": _iso(dates[-1])},
                 evidence=[{"event_id": row["event_id"], "date": row["date_iso"],
                            "source": row["source"], "instrument_id": ident}
                           for row in rows])

    by_instrument_day: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for (ident, day, theme), rows in copresence.items():
        by_instrument_day.setdefault((ident, day), {}).setdefault(theme, []).extend(rows)
    pair_slots: dict[tuple[str, str], dict] = {}
    for (ident, day), per_theme in sorted(by_instrument_day.items()):
        labels = sorted(per_theme)
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                left, right = labels[i], labels[j]
                rows = per_theme[left] + per_theme[right]
                slot = pair_slots.setdefault((left, right), {
                    "evidence": [], "dates": set(), "instruments": set(), "sources": set()})
                slot["evidence"].append({
                    "date": day, "instrument_id": ident,
                    "event_ids": sorted({row["event_id"] for row in rows})})
                slot["dates"].add(day)
                slot["instruments"].add(ident)
                slot["sources"].update(row["source"] for row in rows)
    for (left, right), slot in sorted(pair_slots.items()):
        dates = sorted(slot["dates"])
        sources = sorted(slot["sources"])
        add_edge("theme_copresent_same_instrument_same_day",
                 f"theme:{left}", f"theme:{right}",
                 support={"dates": len(dates), "instruments": len(slot["instruments"]),
                          "co_occurrences": len(slot["evidence"]),
                          "sources": sources,
                          "crosses_sources": len(sources) > 1,
                          "first_seen": dates[0], "last_seen": dates[-1]},
                 evidence=slot["evidence"])

    window_days = {seg["segment_id"]: seg["window_trading_days"] for seg in segments}
    if cal is not None:
        for (segment_id, theme), rows in sorted(window_themes.items()):
            dates = sorted({row["date"] for row in rows})
            add_edge("cycle_contains_theme", f"cycle:{segment_id}", f"theme:{theme}",
                     support={"events": len(rows), "dates": len(dates),
                              "window_trading_days": window_days[segment_id],
                              "recurrence_within_window": len(dates) > 1,
                              "first_seen": _iso(dates[0]), "last_seen": _iso(dates[-1])},
                     evidence=[{"event_id": row["event_id"], "date": row["date_iso"]}
                               for row in rows])
        for (segment_id, ident), rows in sorted(window_instruments.items()):
            dates = sorted({row["date"] for row in rows})
            add_edge("cycle_contains_instrument", f"cycle:{segment_id}", f"instrument:{ident}",
                     support={"events": len(rows), "dates": len(dates),
                              "window_trading_days": window_days[segment_id],
                              "first_seen": _iso(dates[0]), "last_seen": _iso(dates[-1])},
                     evidence=[{"event_id": row["event_id"], "date": row["date_iso"]}
                               for row in rows])
        for prev, nxt in zip(segments, segments[1:]):
            break_info = nxt["break_after_previous"] or {}
            add_edge("cycle_precedes_cycle",
                     f"cycle:{prev['segment_id']}", f"cycle:{nxt['segment_id']}",
                     support={"trading_days_between": break_info["trading_days_between"],
                              "unobserved_trading_days_between":
                                  break_info["unobserved_trading_day_count"]},
                     evidence=[{"prev_end": prev["end"], "next_start": nxt["start"],
                                "trading_days_between": break_info["trading_days_between"]}])

    edges.sort(key=lambda e: (_EDGE_ORDER[e["type"]], e["src"], e["dst"]))
    edge_counts = {etype: 0 for etype in EDGE_TYPES}
    for edge in edges:
        edge_counts[edge["type"]] += 1

    # ------------------------------------------------ 覆盖 / 诊断 / 未解决
    span_start, span_end = _iso(observed[0]), _iso(observed[-1])
    if cal is None:
        unobserved = {"count": None, "dates": [],
                      "requires": "trading_calendar",
                      "note": "未提供交易日历: 无法判断观测日之间是否还有交易日"}
        continuity = "unknown"
        suppressed = {
            "cycle_contains_theme": {"count": len(themes),
                                     "reason": "requires_trading_calendar"},
            "cycle_contains_instrument": {"count": len(instruments),
                                          "reason": "requires_trading_calendar"},
            "cycle_precedes_cycle": {"count": 0, "reason": "requires_trading_calendar"},
        }
    else:
        observed_set = set(observed)
        in_span = [d for d in cal["dates"]
                   if observed[0] <= d <= observed[-1] and d not in observed_set]
        dates_capped, total, truncated = _capped([_iso(d) for d in in_span], _UNOBSERVED_CAP)
        unobserved = {"count": total, "dates": dates_capped, "requires": None,
                      "calendar_basis": "supplied_by_caller_unverified",
                      "note": "仅相对调用方日历未观测到事件；日历与采集完整性均未核验",
                      "truncated": truncated}
        continuity = "relative_to_supplied_calendar"
        suppressed = {etype: {"count": 0, "reason": None} for etype in
                      ("cycle_contains_theme", "cycle_contains_instrument",
                       "cycle_precedes_cycle")}

    published_unknown = sum(1 for ev in evs if ev["visibility"] != "known")
    identity_unverified = sum(1 for ev in evs
                              if ev["instrument"]["identity_status"] == IDENTITY_UNVERIFIED)
    identity_unknown = sum(1 for ev in evs
                           if ev["instrument"]["identity_status"] == IDENTITY_UNKNOWN)
    theme_branch_unknown = sum(1 for node in theme_nodes
                               if node["branch_status"] == "unknown")

    coverage = {
        "as_of": _iso(as_of_date),
        "event_date_span": {"start": span_start, "end": span_end},
        "observed_dates": {"count": len(observed),
                           "dates": [_iso(d) for d in observed]},
        "calendar": {
            "provided": cal is not None,
            "entries": cal["entries"] if cal else 0,
            "effective_entries": len(cal["dates"]) if cal else 0,
            "excluded_after_as_of": cal["excluded_after_as_of"] if cal else 0,
            "first": cal["first"] if cal else None,
            "last": cal["last"] if cal else None,
            "used_for_continuity": cal is not None,
        },
        "continuity": continuity,
        "unobserved_trading_days_in_span": unobserved,
        "segments": len(segments),
        "unknowns": {
            "published_at_unknown": published_unknown,
            "published_at_known": len(evs) - published_unknown,
            "identity_unverified": identity_unverified,
            "identity_unknown": identity_unknown,
            "theme_branch_unknown": theme_branch_unknown,
            "suspension_status": "unknown",
            "price_adjustment": "unknown",
            "klines": "provided" if bars is not None else "not_provided",
            "event_visibility": "unknown_unless_published_at_provided",
        },
    }

    identity_conflicts = [
        {"instrument_id": ident,
         "statuses": sorted(statuses),
         "has_verified_node": ident in instruments}
        for ident, statuses in sorted(statuses_by_id.items()) if len(statuses) > 1
    ]
    shared_names = [
        {"name": name, "instrument_ids": sorted(ids)}
        for name, ids in sorted(ids_by_name.items())
        if len({i for i in ids if i in instruments}) > 1
    ]
    branch_conflicts = [
        {"theme_text": theme, "branches": sorted(slot["branches"])}
        for theme, slot in sorted(themes.items()) if len(slot["branches"]) > 1
    ]
    multi_source = sorted(theme for theme, slot in themes.items() if len(slot["sources"]) > 1)
    spanning = [
        {"theme_text": theme, "segment_ids": sorted(slot["segments"])}
        for theme, slot in sorted(themes.items()) if len(slot["segments"]) > 1
    ]
    published_before = [
        {"event_id": ev["event_id"], "occurred_on": ev["date_iso"],
         "published_at": ev["published_at"]}
        for ev in evs
        if ev["published_date"] is not None and ev["published_date"] < ev["date"]
    ]

    dup_buckets: dict[tuple, int] = {}
    for ev in evs:
        ref = ev["reference"] or {}
        key = (ev["date_iso"], ev["instrument"]["id"], ev["theme_text"], ev["source"],
               ref.get("file"), ref.get("row"))
        dup_buckets[key] = dup_buckets.get(key, 0) + 1
    duplicate_observations = [
        {"date": key[0], "instrument_id": key[1], "theme_text": key[2],
         "source": key[3], "reference_file": key[4], "reference_row": key[5],
         "count": count}
        for key, count in sorted(dup_buckets.items(), key=lambda item: (
            item[0][0], item[0][1], item[0][2], item[0][3],
            item[0][4] or "", item[0][5] if item[0][5] is not None else -1))
        if count > 1
    ]

    diagnostics = {
        "identity_status_conflicts": identity_conflicts,
        "shared_display_names": shared_names,
        "branch_conflicts": branch_conflicts,
        "themes_with_multiple_sources": multi_source,
        "themes_spanning_segment_breaks": spanning,
        "published_before_occurrence": published_before,
        "duplicate_observation_candidates": duplicate_observations,
        "suppressed_relations": suppressed,
        "ignored_calendar_dates_after_as_of": cal["excluded_dates"] if cal else [],
    }

    without_identity = [
        {"event_id": ev["event_id"], "instrument_id": ev["instrument"]["id"],
         "identity_status": ev["instrument"]["identity_status"],
         "date": ev["date_iso"],
         "reason": ("identity_status_unverified"
                    if ev["instrument"]["identity_status"] == IDENTITY_UNVERIFIED
                    else "identity_status_unknown")}
        for ev in evs if ev["instrument"]["identity_status"] != IDENTITY_VERIFIED
    ]
    without_bar = [
        {"event_id": item["event_id"], "instrument_id": item["instrument"]["id"],
         "date": item["date"], "reason": "bar_or_cross_source_identity_unknown",
         "absence_interpretation": "unknown_not_suspension"}
        for item in event_nodes
        if item["bar_present"] is None
    ]
    unmatched_klines: list[dict] = []
    unmatched_seen: dict[str, dict] = {}
    for row in bars or []:
        if row["instrument_id"] in verified_instruments and row["cross_source_identity_declared"]:
            continue
        slot = unmatched_seen.setdefault(row["instrument_id"], {
            "instrument_id": row["instrument_id"], "date": row["date_iso"],
            "bars": 0, "reason": ("cross_source_identity_basis_missing"
                                   if row["instrument_id"] in verified_instruments
                                   else "identity_not_verified_by_events")})
        slot["bars"] += 1
    unmatched_klines = [unmatched_seen[ident] for ident in sorted(unmatched_seen)]

    unresolved = {
        "events_without_verified_identity": without_identity,
        "events_without_bar": without_bar,
        "klines_without_instrument": unmatched_klines,
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "provenance": {
            "as_of": _iso(as_of_date),
            "created_at": generated_at,
            "events": len(evs),
            "klines": len(bars or []),
            "calendar_dates": len(cal["dates"]) if cal else 0,
            "identity_verification": "caller_asserted_with_basis",
            "prices_emitted": False,
            "retrospective": "unknown_not_asserted",
            "point_in_time_verified": False,
            "hardcoded_case_used": False,
        },
        "claims": dict(CLAIMS),
        "coverage": coverage,
        "segments": segments,
        "cycles": {
            "status": "available" if cal is not None else "unavailable_missing_calendar",
            "count": len(cycle_nodes),
            "basis": "contiguous_relative_to_caller_calendar_unverified",
            "market_cycle_claim": "not_asserted",
        },
        "branches": branch_summary,
        "nodes": nodes,
        "node_counts": node_counts,
        "edges": edges,
        "edge_counts": edge_counts,
        "diagnostics": diagnostics,
        "unresolved": unresolved,
        "limitations": list(LIMITATIONS),
    }
    # claims 是本层唯一允许写出"被否认概念"的地方(值为 not_used/not_asserted 等)
    _assert_no_forbidden_keys({k: v for k, v in payload.items() if k != "claims"})
    return payload


def _assert_no_forbidden_keys(node: Any) -> None:
    if isinstance(node, Mapping):
        leaked = [key for key in node if key in FORBIDDEN_OUTPUT_KEYS]
        if leaked:
            raise ValueError(f"输出含方向/收益/资金类字段: {leaked}")
        for value in node.values():
            _assert_no_forbidden_keys(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _assert_no_forbidden_keys(value)


def assert_no_trading_wording(text: str) -> None:
    """本层叙述门禁: 出现交易措辞即抛错(来源标签原文不走这里)。"""
    hits = [word for word in FORBIDDEN_WORDS if word in (text or "")]
    if hits:
        raise ValueError(f"叙述出现交易措辞: {hits}")
