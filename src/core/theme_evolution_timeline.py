"""题材演化时间线(纯函数层): 事后标签事件 × 日K 的**精确日期落点**与缺K线清单。

## 这一层是什么

调用者已经核验过的两类输入:

* `events` —— 历史涨停榜的**事后原因标签**事件, 每条必须自带
  `source` 与 `reference{file, line, sha256}`, 能逐条追回原始响应;
* `klines` —— 同源或调用者指定的日K行(`code, date, ohlc, volume`)。

本模块只做三件事: 按日期排序展示、把事件落到**日期完全相同**的那根K线、
落不到的单列清单。**不插值、不补邻近日、不推导收益、不映射代码、不给方向。**

## 硬边界(违反即 bug)

1. **纯函数**: 无 IO、无网络、无数据库、无时钟、不改写入参。`generated_at`
   只能由调用者显式传入。
2. **事后性质**: 涨停原因标签是供应商事后整理的字段。输出固定标
   `retrospective=true` / `published_at="unknown"` / `point_in_time_verified=false`;
   调用者若声称 `retrospective=False` 或给出发布时间, 一律拒绝 —— 这两件事在
   本数据上无法证实, 接受就等于默许把事后信息当盘中信息。
3. **只覆盖涨停榜**: 事件是"当天涨停且被贴了标签的股票", 不是完整题材成分,
   也不是因果。标签宽度只表示当日返回里该原文标签命中几只, **不可相加**。
4. **精确日期才落点**: 某股当日停牌/无K线时, 该事件留在时间线上但 `has_bar=false`,
   并进入 `missing_klines`; 绝不拿前一交易日收盘顶替。
5. **代码原样**: 不补 `.SZ/.SH`、不按名称匹配、不猜证券身份。只有调用者显式传入
   `verified_code_map` 才做映射, 且映射写入 `provenance` 供复核; 非单射即拒绝。
6. **fail closed**: 日期格式/周末、重复事件、重复K线、重复标签、未切分的原始标签、
   引用缺失/摘要非法、K线数值非有限或 OHLC 自相矛盾 —— 全部抛
   `TimelineInputError`, 不做"尽量算"。
7. **无交易语义**: 输出不含方向/收益/目标/仓位字段(`FORBIDDEN_OUTPUT_KEYS`),
   本层叙述过 `assert_no_trading_wording`; 来源标签原文按原样保留、不改写。

## 已知局限(随输出一起返回, 供报告引用)

* 无交易日历: 只拒绝周六/日, 节假日与停牌无法在此判断, 也不计算"连续N日"。
* 无复权、无成交能力、无 ST/停牌状态; K线仅作当日描述, 不产生收益字段。
* 标签来自供应商事后归因, 其含义/口径/修订历史均未独立验证。

## 输入契约(调用者负责"已验证", 本层只做形状与一致性校验)

`events`: `list[dict]`, 逐条:

    {"date": "20261012" | "2026-10-12",   # 必填; 周末/非真实日期即拒
     "code": "002591",                    # 必填; 原样保留, 不去后缀不补后缀
     "name": "恒大高新",                   # 可选; 仅供阅读
     "labels": ["钠电材料", "硅碳负极"],     # 必填; 调用者已按来源切分, 含 '+' 即拒
     "source": "同花顺 dataapi",           # 必填
     "reference": {"file": "…json", "line": 1, "sha256": "<64 hex>"},  # 必填
     "retrospective": true,               # 只接受 true(缺省即 true)
     "published_at": "unknown"}           # 只接受 null/"unknown"/"未知"

`klines`: `list[dict]` —— `{"code", "date", "open", "high", "low", "close", "volume"}`,
数值必须有限、价格为正、成交量非负且 OHLC 自洽; 同一 (code, date) 只能一行。

`verified_code_map`: 可选 `dict[str, str]`, 只在事件代码**不**出现在K线源时生效,
用于调用者已独立核实的跨源身份; 必须单射, 使用情况写入 `provenance`。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

SCHEMA_VERSION = "theme_evo_timeline_v1"
RULE_VERSION = "theme_evo_timeline_v1"

RETROSPECTIVE = True
PUBLISHED_AT_UNKNOWN = "unknown"
SOURCE_KIND = "post_hoc_limit_up_reason_label"

#: 输出里绝不允许出现的字段名(方向/收益/目标/仓位/因果)。
FORBIDDEN_OUTPUT_KEYS = frozenset({
    "signal", "signal_score", "direction", "buy", "sell", "recommend",
    "recommendation", "target_price", "expected_return", "forward_return",
    "future_return", "return_fwd", "alpha", "position", "position_size",
    "weight", "score", "confidence", "caused_by", "causal", "flow",
    "capital_flow", "inflow", "outflow", "net_buy", "qfq", "hfq",
})

#: 本层自己写的叙述里禁止出现的交易措辞(来源标签原文不受此限)。
FORBIDDEN_WORDS = (
    "建议买入", "可买入", "建议卖出", "建议加仓", "建议减仓", "目标价",
    "必买", "推荐", "看多", "看空", "抄底", "追高", "止盈", "止损",
    "加仓", "减仓",
)

#: 供应商把多个题材用 `+` 串在一个字段里; 未切分的原始串不许当单个标签存。
LABEL_SEPARATORS = ("+", "\uff0b")

_DATE_RE = re.compile(r"^(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_ACCEPTED_PUBLISHED_AT = {None, "", "unknown", "未知"}

LIMITATIONS = (
    "事件来源是涨停榜的事后原因标签: 只覆盖当日涨停且被贴标签的股票, 不是完整题材成分, 也不证明因果。",
    "无归因发布时间与修订历史: 输出固定标 published_at=unknown、point_in_time_verified=false, 不可用于盘中回放。",
    "未使用交易日历: 仅拒绝周六/日, 节假日与停牌日不可判断, 也不计算连续交易日的跨度。",
    "K线只做当日描述: 复权口径未提供或未核实，ST/停牌/涨跌停可成交性未知, 不产生收益或方向字段。",
    "本纯函数不读取原始文件或核验证券身份：引用摘要和跨源映射仅为调用方声明。",
    "股票代码按原文保留, 不做交易所后缀推断或名称匹配; 跨源身份只能由调用者以 verified_code_map 显式声明。",
    "缺K线一律列入 missing_klines, 不插值、不用邻近日顶替。",
    '标签宽度是「当日返回里该原文标签命中几只」, 同股多标签不可相加, 也不等于板块涨幅。',
)


class TimelineInputError(ValueError):
    """输入不可用时抛这个(带稳定 code, 便于调用方/测试断言)。"""

    def __init__(self, code: str, message: str, *, detail: Mapping[str, Any] | None = None):
        self.code = code
        self.detail = dict(detail or {})
        super().__init__(f"[{code}] {message}")


def _fail(error_code: str, message: str, **detail: Any) -> "TimelineInputError":
    return TimelineInputError(error_code, message, detail=detail)


# ---------------------------------------------------------------- 日期
def parse_trade_date(value: Any, *, field: str = "date") -> date:
    """严格解析交易日: 只接受 `YYYYMMDD` / `YYYY-MM-DD`, 周末直接拒绝。

    不做"智能"修补(空格、斜杠、缺零、时间戳一律拒绝) —— 日期错位比缺数据更危险。
    """
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


# ---------------------------------------------------------------- 归一化事件
def _normalize_events(events: Any) -> list[dict]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise _fail("invalid_events_container", "events 必须是 list/tuple")
    if not events:
        raise _fail("events_empty", "events 为空: 无已验证事件可排(不伪造时间线)")

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for idx, raw in enumerate(events):
        where = f"events[{idx}]"
        if not isinstance(raw, Mapping):
            raise _fail("invalid_event", f"{where} 必须是 mapping", field=where)

        day = parse_trade_date(raw.get("date"), field=f"{where}.date")
        code = _require_clean_str(raw.get("code"), "invalid_event_code",
                                  f"{where}.code 必须是非空且无空白的字符串", field=f"{where}.code")

        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise _fail("invalid_event_name", f"{where}.name 必须是字符串或 null",
                        field=f"{where}.name")

        labels_raw = raw.get("labels")
        if not isinstance(labels_raw, Sequence) or isinstance(labels_raw, (str, bytes)):
            raise _fail("invalid_labels_type", f"{where}.labels 必须是字符串列表",
                        field=f"{where}.labels")
        if not labels_raw:
            raise _fail("event_without_labels", f"{where}.labels 为空: 标签事件必须有标签",
                        field=f"{where}.labels", code=code)
        labels: list[str] = []
        for item in labels_raw:
            if not isinstance(item, str) or not item.strip() or item != item.strip():
                raise _fail("invalid_label", f"{where}.labels 含非法项 {item!r}",
                            field=f"{where}.labels", value=repr(item))
            if any(sep in item for sep in LABEL_SEPARATORS):
                raise _fail("label_not_split",
                            f"{where}.labels 未切分(含 '+'): {item!r}",
                            field=f"{where}.labels", value=item)
            labels.append(item)
        if len(set(labels)) != len(labels):
            raise _fail("duplicate_label", f"{where}.labels 有重复标签: {labels}",
                        field=f"{where}.labels", code=code)

        source = _require_clean_str(raw.get("source"), "invalid_source",
                                    f"{where}.source 必须是非空来源标识", field=f"{where}.source")

        if "retrospective" in raw and raw["retrospective"] is not True:
            raise _fail("unverifiable_retrospective_claim",
                        f"{where}.retrospective={raw['retrospective']!r}: 事后标签不能声称实时可得",
                        field=f"{where}.retrospective", code=code)
        if "published_at" in raw:
            claimed = raw["published_at"]
            if not (claimed is None or (isinstance(claimed, str)
                                        and claimed.strip().lower() in _ACCEPTED_PUBLISHED_AT)):
                raise _fail("unverifiable_published_at",
                            f"{where}.published_at={claimed!r}: 源无发布时间, 不接受自报时刻",
                            field=f"{where}.published_at", code=code)

        reference = raw.get("reference")
        if not isinstance(reference, Mapping):
            raise _fail("missing_reference",
                        f"{where}.reference 缺失: 每条事件必须能追回文件/行/摘要",
                        field=f"{where}.reference", code=code)
        ref_file = _require_clean_str(reference.get("file"), "invalid_reference_file",
                                      f"{where}.reference.file 必须是非空相对路径",
                                      field=f"{where}.reference.file")
        ref_line = reference.get("line")
        if isinstance(ref_line, bool) or not isinstance(ref_line, int) or ref_line < 1:
            raise _fail("invalid_reference_line",
                        f"{where}.reference.line 必须是 >=1 的整数, 实得 {ref_line!r}",
                        field=f"{where}.reference.line", value=repr(ref_line))
        ref_sha = reference.get("sha256")
        if not isinstance(ref_sha, str) or not _SHA_RE.match(ref_sha):
            raise _fail("invalid_reference_sha256",
                        f"{where}.reference.sha256 必须是 64 位小写十六进制, 实得 {ref_sha!r}",
                        field=f"{where}.reference.sha256", value=repr(ref_sha))

        key = (day.strftime("%Y%m%d"), code)
        if key in seen:
            raise _fail("duplicate_event",
                        f"重复事件(date={key[0]}, code={code}): 一日一码只能一条",
                        date=key[0], code=code)
        seen.add(key)

        out.append({
            "date": day, "date_compact": key[0], "code": code, "name": name,
            "labels": labels, "source": source,
            "reference": {"file": ref_file, "line": ref_line, "sha256": ref_sha},
            "retrospective": RETROSPECTIVE, "published_at": PUBLISHED_AT_UNKNOWN,
        })
    return out


# ---------------------------------------------------------------- 归一化K线
def _normalize_klines(klines: Any) -> list[dict]:
    if not isinstance(klines, Sequence) or isinstance(klines, (str, bytes)):
        raise _fail("invalid_klines_container", "klines 必须是 list/tuple")
    if not klines:
        raise _fail("klines_empty", "klines 为空: 无K线可落点(不伪造行情)")

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for idx, raw in enumerate(klines):
        where = f"klines[{idx}]"
        if not isinstance(raw, Mapping):
            raise _fail("invalid_kline", f"{where} 必须是 mapping", field=where)
        code = _require_clean_str(raw.get("code"), "invalid_kline_code",
                                  f"{where}.code 必须是非空且无空白的字符串", field=f"{where}.code")
        day = parse_trade_date(raw.get("date"), field=f"{where}.date")

        open_ = _require_number(raw.get("open"), f"{where}.open")
        high = _require_number(raw.get("high"), f"{where}.high")
        low = _require_number(raw.get("low"), f"{where}.low")
        close = _require_number(raw.get("close"), f"{where}.close")
        volume = _require_number(raw.get("volume"), f"{where}.volume")

        if min(open_, high, low, close) <= 0:
            raise _fail("invalid_kline_value", f"{where} 价格必须为正: {raw!r}", field=where)
        if volume < 0:
            raise _fail("invalid_kline_value", f"{where}.volume 不能为负: {volume}", field=where)
        if high < max(open_, close, low) or low > min(open_, close):
            raise _fail("invalid_kline_value",
                        f"{where} OHLC 自相矛盾: o={open_} h={high} l={low} c={close}",
                        field=where)

        key = (code, day.strftime("%Y%m%d"))
        if key in seen:
            raise _fail("duplicate_kline",
                        f"重复K线(code={code}, date={key[1]}): 一码一日只能一行",
                        code=code, date=key[1])
        seen.add(key)
        out.append({"code": code, "date": day, "date_compact": key[1],
                    "bar": {"open": open_, "high": high, "low": low,
                            "close": close, "volume": volume}})
    return out


def _normalize_code_map(mapping: Any) -> dict[str, str]:
    if mapping is None:
        return {}
    if not isinstance(mapping, Mapping):
        raise _fail("invalid_code_map", "verified_code_map 必须是 dict")
    out: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not key or key != key.strip():
            raise _fail("invalid_code_map", f"verified_code_map 键非法: {key!r}", key=repr(key))
        if not isinstance(value, str) or not value or value != value.strip():
            raise _fail("invalid_code_map", f"verified_code_map 值非法: {value!r}",
                        key=key, value=repr(value))
        out[key] = value
    values = list(out.values())
    if len(set(values)) != len(values):
        raise _fail("ambiguous_code_map",
                    "verified_code_map 必须单射: 两个事件代码不能映射到同一标的")
    return out


# ---------------------------------------------------------------- 主函数
def build_timeline(events: Any, klines: Any, *,
                   verified_code_map: Mapping[str, str] | None = None,
                   generated_at: str | None = None) -> dict:
    """事件 × K线 → 时间线/落点/缺K线清单/溯源引用。输入不合法即抛错。"""
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

    evs = _normalize_events(events)
    bars = _normalize_klines(klines)
    code_map = _normalize_code_map(verified_code_map)

    universe = {b["code"] for b in bars}
    # 显式映射与同码连接必须整体单射；仅检查映射表本身不够。
    targets: dict[str, str] = {}
    for source_code in {ev["code"] for ev in evs}:
        target = source_code if source_code in universe else code_map.get(source_code)
        if target is not None and target in targets and targets[target] != source_code:
            raise _fail("ambiguous_code_map", "多个来源代码指向同一K线代码，不能确认证券身份")
        if target is not None:
            targets[target] = source_code
    bar_index = {(b["code"], b["date_compact"]): b["bar"] for b in bars}

    landings = {"total": 0, "exact": 0, "mapped": 0, "unresolved": 0}
    mapped_codes: set[str] = set()
    missing: list[dict] = []
    by_day: dict[str, list[dict]] = {}

    for ev in evs:
        code = ev["code"]
        if code in universe:
            kline_code, join = code, "exact"
        elif code in code_map:
            kline_code, join = code_map[code], "caller_supplied_map"
            mapped_codes.add(code)
        else:
            kline_code, join = None, "unresolved"

        key = (kline_code, ev["date_compact"]) if kline_code else None
        bar = bar_index.get(key) if key else None
        has_bar = bar is not None

        landings["total"] += 1
        if has_bar:
            landings["exact" if join == "exact" else "mapped"] += 1
        else:
            landings["unresolved"] += 1
            reason = ("no_bar_on_exact_date" if kline_code in universe
                      else "code_not_in_kline_source")
            missing.append({"date": ev["date"].isoformat(), "code": code,
                            "kline_code": kline_code or "", "reason": reason})

        by_day.setdefault(ev["date_compact"], []).append({
            "date": ev["date"].isoformat(), "code": code, "name": ev["name"],
            "labels": list(ev["labels"]), "code_join": join,
            "kline_code": kline_code, "has_bar": has_bar,
            "bar": dict(bar) if bar else None,
            "source": ev["source"], "retrospective": RETROSPECTIVE,
            "published_at": PUBLISHED_AT_UNKNOWN,
            "reference": dict(ev["reference"]),
        })

    timeline: list[dict] = []
    prev_date: date | None = None
    for compact in sorted(by_day):
        rows = sorted(by_day[compact], key=lambda r: r["code"])
        day = rows[0]["date"]
        parsed = date.fromisoformat(day)
        widths: dict[str, dict] = {}
        for row in rows:
            for label in row["labels"]:
                slot = widths.setdefault(label, {"label": label, "stocks": 0, "codes": []})
                slot["codes"].append(row["code"])
        label_rows = []
        for label in sorted(widths, key=lambda x: (-len(widths[x]["codes"]), x)):
            slot = widths[label]
            codes = sorted(set(slot["codes"]))
            label_rows.append({"label": label, "stocks": len(codes), "codes": codes})
        present = sum(1 for r in rows if r["has_bar"])
        timeline.append({
            "date": day, "date_compact": compact,
            "previous_event_date": prev_date.isoformat() if prev_date else None,
            "gap_calendar_days": (parsed - prev_date).days if prev_date else None,
            "events_total": len(rows), "bars_present": present,
            "bars_missing": len(rows) - present,
            "labels": label_rows, "events": rows,
        })
        prev_date = parsed

    # 标签时间线: 只报每日命中只数; 跨标签不可相加
    label_days: set[str] = {
        label for entry in timeline for row in entry["events"] for label in row["labels"]
    }
    label_timeline = []
    for label in sorted(label_days):
        per_day: dict[str, int] = {}
        for entry in timeline:
            hits = {r["code"] for r in entry["events"] if label in r["labels"]}
            if hits:
                per_day[entry["date_compact"]] = len(hits)
        label_timeline.append({
            "label": label,
            "dates": [{"date": d, "date_compact": d, "stocks": per_day[d]}
                      for d in sorted(per_day)],
            "stock_days": sum(per_day.values()),
        })

    references = []
    ref_seen: set[tuple] = set()
    for entry in timeline:
        for row in entry["events"]:
            r = row["reference"]
            key = (row["date"], row["code"], r["file"], r["line"], r["sha256"], row["source"])
            if key in ref_seen:
                continue
            ref_seen.add(key)
            references.append({
                "date": row["date"], "code": row["code"], "source": row["source"],
                "file": r["file"], "line": r["line"], "sha256": r["sha256"],
                "retrospective": RETROSPECTIVE, "published_at": PUBLISHED_AT_UNKNOWN,
            })

    unused_map = sorted(set(code_map) - mapped_codes)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "provenance": {
            "retrospective": RETROSPECTIVE,
            "published_at": PUBLISHED_AT_UNKNOWN,
            "point_in_time_verified": False,
            "source_kind": SOURCE_KIND,
            "reference_integrity": "not_verified_by_timeline",
            "identity_mapping_verification": "caller_asserted_unverified",
            "price_adjustment": "unknown",
            "created_at": generated_at,
            "trading_calendar": "not_used",
            "events": len(evs),
            "klines": len(bars),
            "dates": len(timeline),
            "codes": len({e["code"] for e in evs}),
            "code_join": {
                "exact": sum(1 for t in timeline for e in t["events"] if e["code_join"] == "exact"),
                "caller_supplied_map": sum(
                    1 for t in timeline for e in t["events"] if e["code_join"] == "caller_supplied_map"),
                "unresolved": sum(
                    1 for t in timeline for e in t["events"] if e["code_join"] == "unresolved"),
            },
            "verified_code_map": {
                "entries": len(code_map), "used": len(mapped_codes),
                "unused": len(unused_map),
            },
            "contains_trading_advice": False,
            "contains_direction": False,
            "contains_price_target": False,
            "limits": list(LIMITATIONS),
        },
        "timeline": timeline,
        "landings": landings,
        "missing_klines": sorted(missing, key=lambda m: (m["date"], m["code"])),
        "references": references,
        "label_timeline": label_timeline,
        "limitations": list(LIMITATIONS),
    }
    _assert_no_forbidden_keys(payload)
    return payload


def _assert_no_forbidden_keys(node: Any) -> None:
    if isinstance(node, Mapping):
        leaked = [k for k in node if k in FORBIDDEN_OUTPUT_KEYS]
        if leaked:
            raise ValueError(f"输出含方向/收益类字段: {leaked}")
        for value in node.values():
            _assert_no_forbidden_keys(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _assert_no_forbidden_keys(value)


def assert_no_trading_wording(text: str) -> None:
    hits = [w for w in FORBIDDEN_WORDS if w in (text or "")]
    if hits:
        raise ValueError(f"叙述出现交易措辞: {hits}")


def render_markdown(payload: Mapping[str, Any]) -> str:
    """渲染成可读报告。来源标签原文按原样引用; 本层叙述逐段过措辞门禁。"""
    lines: list[str] = []

    def add(text: str = "") -> None:
        assert_no_trading_wording(text)
        lines.append(text)

    def add_raw(text: str) -> None:
        lines.append(text)

    prov = payload["provenance"]
    add(f"# 题材演化时间线(事后标签) · {len(payload['timeline'])} 个事件日")
    add()
    add(f"- 规则版本: `{payload['rule_version']}`; 事件 {prov['events']} 条 / "
        f"K线 {prov['klines']} 行 / 覆盖 {prov['dates']} 个日期")
    add(f"- 事件性质: **事后**归因标签(`retrospective=true`), 发布时间 "
        f"`{prov['published_at']}`; 时点可回放性 **未证实**"
        f"(`point_in_time_verified=false`)")
    add("- 落点规则: 只有事件日期与该股K线日期**完全相同**才算落点; "
        "缺K线一律列入下方清单, 不插值、不用邻近日顶替。")
    add(f"- 落点统计: 合计 {payload['landings']['total']} 条 → 同码 "
        f"{payload['landings']['exact']} / 调用方声明映射 {payload['landings']['mapped']} / "
        f"未落点 {payload['landings']['unresolved']}")
    add()

    add("## 逐日时间线")
    add()
    for entry in payload["timeline"]:
        gap = entry["gap_calendar_days"]
        gap_note = "首个事件日" if gap is None else f"距上一事件日 {gap} 个自然日"
        add(f"### {entry['date']} ({gap_note})")
        add()
        add(f"- 事件 {entry['events_total']} 条: 落点 {entry['bars_present']} / "
            f"缺K线 {entry['bars_missing']}")
        if entry["labels"]:
            shown = entry["labels"][:12]
            rest = len(entry["labels"]) - len(shown)
            add(f"- 当日标签宽度(该原文标签命中几只, 同股多标签不可相加; "
                f"共 {len(entry['labels'])} 个不同标签, 此处按命中只数取前 {len(shown)}):")
            widths = ", ".join(f"{item['label']}={item['stocks']}" for item in shown)
            if rest > 0:
                widths += f" …另 {rest} 个"
            add_raw(f"  - {widths}")
        add()
        add("| 股票代码 | 名称 | 标签(来源原文) | 落点 | K线日期 |")
        add("|---|---|---|---|---|")
        for row in entry["events"]:
            hit = "已落点" if row["has_bar"] else "缺K线"
            kdate = row["date"] if row["has_bar"] else "—"
            add_raw(f"| `{row['code']}` | {row['name'] or '—'} | "
                    f"{' / '.join(row['labels'])} | {hit} | {kdate} |")
        add()

    add("## 缺K线清单(不插值)")
    add()
    if not payload["missing_klines"]:
        add("无: 全部事件都在同日K线上落点。")
    else:
        add(f"共 {len(payload['missing_klines'])} 条事件在事件日期上找不到对应K线:")
        add()
        add("| 日期 | 股票代码 | 映射后代码 | 原因 |")
        add("|---|---|---|---|")
        for miss in payload["missing_klines"]:
            add(f"| {miss['date']} | `{miss['code']}` | "
                f"{('`' + miss['kline_code'] + '`') if miss['kline_code'] else '—'} "
                f"| {miss['reason']} |")
    add()

    add("## 溯源引用（调用方提供的文件 / 索引 / 摘要 / 来源）")
    add("索引及摘要未由本函数核验；索引的具体含义以调用方数据契约为准。")
    add()
    add("| 日期 | 股票代码 | 来源 | 文件 | 索引（未核验） | sha256 |")
    add("|---|---|---|---|---|---|")
    for ref in payload["references"][:50]:
        add(f"| {ref['date']} | `{ref['code']}` | {ref['source']} | `{ref['file']}` "
            f"| {ref['line']} | `{ref['sha256'][:16]}…` |")
    if len(payload["references"]) > 50:
        add(f"| … | 共 {len(payload['references'])} 条, 余下见载荷 |  |  |  |  |")
    add()

    add("## 局限")
    add()
    for item in payload["limitations"]:
        add(f"- {item}")
    add()

    add("## 声明")
    add()
    add("- 本页是公开市场数据的**事后描述**, 不构成投资建议, 不含方向、收益或目标。")
    add("- 所有缺失一律标 unknown 并列清单; 未列出的不代表已核实。")
    add()
    return "\n".join(lines)
