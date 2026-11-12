"""盘前「事件驱动前瞻（双轨制 · 潜伏表）」解析 + 信号明细（2026-11-11）。

背景：盘前分析的潜伏表是 LLM 写的 markdown 表格，正文里只有「受益逻辑」一段文字。
要在页面上看「这只票为什么被挑出来、当时有哪些真实信号」，就不能让 LLM 再编一遍数字 ——
本模块做两件事：

1. `parse_ambush_table(text)`：从报告正文里**解析**那张表（事件/受益板块/代码/名称/受益逻辑/
   强度/埋伏方式/分批计划/止损/目标/证伪条件），拿到结构化行。解析不到就返回空列表，
   不猜、不合成。
2. `ambush_signals(codes)`：按代码取**真实**信号（股性因子、位置与连板），每项都带来源与
   缺失标记。取不到就是 `None`，页面显示「无数据」，绝不用 0 或估算值糊过去。

口径说明：
- 股性分来自 `demon_factors` 表（近一年封板/连板统计的既有口径，非本次新造）。
- 位置/连板由 PG 日线收盘价算：涨停判定用收盘涨幅 ≥9.8%（A股 10% 限制的常见四舍五入边界，
  科创/创业板 20% 的在 `limit_pct` 里区分），连板 = 连续满足涨停的交易日数。
- 止损是否触发只看**收盘价**，不假装知道盘中最低价（那需要分钟线，且盘前报告也没给盘中路径）。
- **时点（PIT）**：盘前报告写于 D 日开盘前，D 日当天收盘、D 日（或之后）重算的股性因子快照
  在那一刻都还不存在。所有「当时信号」只接受 **交易日 < D** 的数据；拿不到就留空 + 写明
  不可得的原因，绝不把事后数据（或今天的快照）当成盘前证据，也不伪造历史快照。

  股性因子的历史重建尤其严格：`demon_factors` 是每股一行的当前快照，`factor_date` 只说明
  「这一行上次在哪天算过」——**快照日早于报告日也不证明当时可见，更不证明数据截至 D-1 完整**。
  因此历史页只认「生成时冻结快照」（raw_data 里保存、且生成时点可验证在 D 日 09:30 前）；
  没有这份凭证就一律 unknown 并写明原因，不拿当前表反推历史。
"""

from __future__ import annotations

import logging
import re
from datetime import date as _D, datetime as _DT, time as _T
from typing import Any, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_BEIJING = ZoneInfo("Asia/Shanghai")
_PREOPEN = _T(9, 30)  # 盘前窗口上界: D 日 09:30 开盘
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_COMPACT_DAY_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)

# 表头列名 → 字段名（报告里列名可能微调，用包含匹配更稳）
_COLUMN_FIELDS: tuple[tuple[str, str], ...] = (
    ("事件", "event"),
    ("受益板块", "sector"),
    ("代码", "code"),
    ("名称", "name"),
    ("受益逻辑", "logic"),
    ("强度", "strength"),
    ("埋伏方式", "method"),
    ("分批", "batch"),
    ("止损", "stop"),
    ("目标", "target"),
    ("证伪", "falsify"),
)

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_TAG_RE = re.compile(r"【([^】]{1,20})】")


def _split_row(line: str) -> list[str]:
    """按未转义的 `|` 切单元格（LLM 偶尔会写 `\\|`）。"""
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw) and raw[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def _clean(v: str | None) -> str:
    if not v:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("**", "").replace("`", "")).strip()


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c is not None) and bool(cells)


def _find_header(lines: list[str], start: int) -> tuple[int, list[str]] | None:
    """从 start 之后找第一张含「代码」的表头。"""
    for i in range(start, min(len(lines), start + 40)):
        line = lines[i]
        if not line.strip().startswith("|"):
            continue
        cells = [_clean(c) for c in _split_row(line)]
        if any("代码" in c for c in cells) and any("名称" in c for c in cells):
            return i, cells
    return None


def _section_start(text: str) -> int:
    """定位「事件驱动前瞻 / 潜伏表」章节；找不到就用全文（只认含代码的表头）。"""
    for i, line in enumerate(text.splitlines()):
        if "事件驱动前瞻" in line or "潜伏表" in line:
            return i
    return 0


def parse_ambush_table(text: str) -> list[dict]:
    """解析盘前报告里的潜伏表。解析不到 → []（调用方显示「未解析到」）。"""
    if not text:
        return []
    lines = text.splitlines()
    start = _section_start(text)
    found = _find_header(lines, start)
    if found is None and start > 0:
        found = _find_header(lines, 0)
    if found is None:
        return []
    header_idx, header_cells = found

    # 列名 → 下标（列名可能带「（必给）」这类后缀，用包含匹配）
    idx_map: dict[str, int] = {}
    for title, field in _COLUMN_FIELDS:  # (表头中文, 字段名)
        for j, cell in enumerate(header_cells):
            if title in cell:
                idx_map[field] = j
                break

    rows: list[dict] = []
    for line in lines[header_idx + 1 :]:
        if not line.strip().startswith("|"):
            break
        cells = _split_row(line)
        if _is_separator(cells):
            continue
        row: dict[str, Any] = {}
        for field, j in idx_map.items():
            row[field] = _clean(cells[j]) if j < len(cells) else ""
        code_match = _CODE_RE.search(row.get("code") or "")
        if not code_match:
            continue
        name_raw = row.get("name") or ""
        tags = _TAG_RE.findall(name_raw)
        row["code"] = code_match.group(1)
        name_clean = _TAG_RE.sub("", name_raw).strip()
        # 老报告里名称列偶尔写成「002931 锋龙股份」→ 去掉重复的代码前缀
        name_clean = re.sub(rf"^{row['code']}\s*", "", name_clean).strip()
        row["name"] = name_clean or row["code"]
        row["tags"] = [t for t in tags if t]
        rows.append(row)
    return rows


# ── 真实信号 ────────────────────────────────────────────────────────────────


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _limit_pct(code: str) -> float:
    """涨停幅度：创业板(300/301)、科创板(688/689) 为 20%，北交所(8xx/920) 30%，其余 10%。

    只用于「收盘是否算涨停」的判定阈值，不用于撮合或交易规则推断。
    """
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "920", "43")):
        return 30.0
    return 10.0


def _parse_dt(v: Any) -> _DT | None:
    """严格解析时间戳 → 北京时区 naive datetime；认不出 → None。

    只接受 `YYYY-MM-DD`、`YYYYMMDD`、`YYYY-MM-DD[T ]HH:MM[:SS[.ffffff]][Z|±HH:MM]`。
    不做「从任意字符串里抠数字」的宽松解析 —— 那会把 `20269999`、`abc20261110`
    拼成一个看起来合法、实际不存在的日期，比报错更危险。
    """
    if isinstance(v, _DT):
        dt = v
    elif isinstance(v, _D):
        return _DT(v.year, v.month, v.day)
    else:
        s = str(v or "").strip()
        if _DATETIME_RE.match(s):
            try:
                dt = _DT.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            m = _DAY_RE.match(s) or _COMPACT_DAY_RE.match(s)
            if not m:
                return None
            try:
                return _DT(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_BEIJING).replace(tzinfo=None)
    return dt


def _norm_day(v: Any) -> str | None:
    """把合法日/时间戳统一成 `YYYY-MM-DD`；非法/认不出 → None（不猜一个日子）。"""
    dt = _parse_dt(v)
    return dt.date().isoformat() if dt else None


def _demon_row_date(row: dict | None) -> str | None:
    """股性因子快照日（表里是 `factor_date`，YYYYMMDD）。无字段/认不出 → None。"""
    if not isinstance(row, dict):
        return None
    return _norm_day(row.get("factor_date") or row.get("snapshot_date") or row.get("updated_at"))


def _filter_demon_rows(
    factors: dict[str, dict], cutoff: str, *, preopen_generation: bool
) -> dict:
    """按报告日筛因子行；`preopen_generation` 决定「快照日 == D」是否可接受。

    - 生成时点可验证在 D 日 09:30 前: factor_date == D 只可能是当天早上算的，
      那一刻确实可见（但**不保证**数据已截至 D-1，所以页面必须显示快照日）。
    - 其他情况（历史重建/收盘后重生成/时点不可验证）: 只接受 factor_date < D。
    """
    kept: dict[str, dict] = {}
    notes: dict[str, str] = {}
    dropped: list[str] = []
    unknown: list[str] = []
    for sym, row in (factors or {}).items():
        code = str(sym).split(".")[0]
        d = _demon_row_date(row if isinstance(row, dict) else None)
        if d is None:
            dropped.append(code)
            unknown.append(code)
            notes[code] = "股性因子快照无有效快照日, 无法确认盘前可得, 不展示"
            continue
        if d > cutoff:
            dropped.append(code)
            notes[code] = f"股性因子快照日 {d} 晚于报告日 {cutoff}, 不可能盘前可见"
            continue
        if d == cutoff and not preopen_generation:
            dropped.append(code)
            notes[code] = (
                f"股性因子快照日 {d} 与报告日相同, 但无法证明是盘前生成, 不展示"
            )
            continue
        kept[code] = {**row, "factor_date": d} if isinstance(row, dict) else row
    kept_dates = [d for d in (_demon_row_date(r) for r in kept.values()) if d]
    return {
        "kept": kept,
        "asof": max(kept_dates) if kept_dates else None,
        "dropped": dropped,
        "dropped_unknown_date": unknown,
        "notes": notes,
    }


def filter_demon_for_generation(
    factors: dict[str, dict], report_day: str, *, generated_at: Any = None
) -> dict:
    """报告**生成侧**的可见性筛：判定生成那一刻哪些因子行已经存在。

    这不是「历史重建」。生成侧手里握着当时的快照与实际生成时间；若生成时点在 D 日
    09:30 前，当天早上算的 factor_date == D 行在那一刻已经存在（可见），只是数据截止
    未必是 D-1（可能更早），所以只保留并**标明快照日**，不宣称完整。
    生成时点缺失或不早于 09:30 → 只接受 factor_date < D（保守，防收盘后重生成泄漏）。
    """
    cutoff = _norm_day(report_day)
    gen = _parse_dt(generated_at) if generated_at is not None else None
    preopen = bool(
        gen is not None
        and cutoff is not None
        and gen.date().isoformat() == cutoff
        and gen.time() < _PREOPEN
    )
    if cutoff is None:
        notes = {
            str(sym).split(".")[0]: f"报告日无法解析({report_day!r}), 无法确认盘前时点, 不展示"
            for sym in (factors or {})
        }
        return {
            "kept": {},
            "asof": None,
            "dropped": list(notes),
            "dropped_unknown_date": [],
            "notes": notes,
            "preopen": False,
        }
    out = _filter_demon_rows(factors or {}, cutoff, preopen_generation=preopen)
    out["preopen"] = preopen
    return out


def _demon_signal_row(row: dict) -> dict:
    """快照原始行(`total`/`grade`…) → 页面/接口用的信号形状(`score`…)，保留快照日。"""
    score = row.get("score")
    if score is None:
        score = row.get("total")
    return {
        "score": _num(score),
        "grade": row.get("grade"),
        "n_sealed": row.get("n_sealed"),
        "max_streak": row.get("max_streak"),
        "participation": row.get("participation"),
        "factor_date": row.get("factor_date"),
    }


def demon_evidence_from_record(raw_data: Any, report_day: str) -> dict:
    """从历史报告的 `raw_data` 取「生成时冻结的股性因子快照」，并验证它确实是盘前。

    为什么不能直接读 `demon_factors` 表：那是每股一行的当前快照，`factor_date` 早于
    报告日既不能证明生成时可见（可能是后来重算但因子日没变），也不能证明数据截至 D-1
    完整。唯一可验证的凭证是生成时写进 raw_data 的冻结副本 + 可解析的生成时间戳，
    且该时间必须落在报告日 09:30 之前。没有凭证 → `verified=False`，调用方一律 unknown。
    """
    result: dict = {
        "verified": False,
        "kept": {},
        "notes": {},
        "dropped": [],
        "frozen_at": None,
        "asof": None,
        "reason": None,
    }
    cutoff = _norm_day(report_day)
    if cutoff is None:
        result["reason"] = f"报告日无法解析({report_day!r}), 无法确认盘前时点"
        return result
    snap = raw_data.get("demon_factors") if isinstance(raw_data, dict) else None
    if not isinstance(snap, dict) or not snap:
        result["reason"] = "该期报告未保存生成时的股性因子冻结快照, 无盘前凭证, 不展示"
        return result
    stamp = raw_data.get("demon_factors_frozen_at") or raw_data.get("timestamp")
    gen = _parse_dt(stamp)
    if gen is None:
        result["reason"] = "冻结快照缺少可解析的生成时点, 无法证明盘前可见, 不展示"
        return result
    if gen.date().isoformat() != cutoff or gen.time() >= _PREOPEN:
        result["reason"] = (
            f"生成时点 {gen.isoformat()} 不在报告日 {cutoff} 盘前(09:30 前), "
            "冻结快照含当日事件, 无法证明盘前可见"
        )
        return result
    filtered = _filter_demon_rows(snap, cutoff, preopen_generation=True)
    result.update(
        verified=True,
        kept={code: _demon_signal_row(row) for code, row in filtered["kept"].items()},
        notes=filtered["notes"],
        dropped=filtered["dropped"],
        asof=filtered["asof"],
        frozen_at=gen.isoformat(),
    )
    if not filtered["kept"]:
        result["reason"] = "冻结快照中没有可确认盘前可见的股性因子行"
    return result


def demon_map(db, codes: Iterable[str], *, cutoff: str | None = None) -> dict[str, dict]:
    """从 `demon_factors` 表取股性因子（表缺失/查询失败 → {}，不猜）。

    `cutoff` 给了就只保留**快照日 < cutoff** 的行（盘前口径，见模块 docstring 的时点说明）；
    拿不到快照日的行同样剔除 —— 证明不了「当时可见」就不展示。
    """
    symbols = [c for c in {str(c) for c in codes} if c]
    if not symbols:
        return {}
    from sqlalchemy import text

    # 库里 symbol 可能是 600183 也可能是 600183.SH，两种都匹配（结果按 code 去重）
    variants = set(symbols)
    variants.update(f"{s}.SH" for s in symbols if s.startswith(("6", "9")))
    variants.update(f"{s}.SZ" for s in symbols if s.startswith(("0", "3")))
    params = {f"s{i}": v for i, v in enumerate(sorted(variants))}
    ph = ",".join(f":s{i}" for i in range(len(params)))
    try:
        rows = db.execute(
            text(
                "SELECT factor_date, symbol, name, total, grade, n_sealed, max_streak, participation "
                f"FROM demon_factors WHERE symbol IN ({ph})"
            ),
            params,
        ).mappings().all()
    except Exception as e:  # noqa: BLE001 — 表不存在(SQLite/早期库)也算取不到
        logger.debug("股性因子查询失败: %s", e)
        return {}
    cut = _norm_day(cutoff) if cutoff else None
    if cutoff and cut is None:
        logger.debug("股性因子 cutoff 无法解析(%r), 全部按不可得处理", cutoff)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        code = str(r["symbol"]).split(".")[0]
        if code in out:  # 多个市场代码变体时保留第一条（结果确定）
            continue
        factor_date = _norm_day(r["factor_date"])
        if cut is not None and (factor_date is None or factor_date >= cut):
            continue
        out[code] = {
            "score": _num(r["total"]),
            "grade": r["grade"],
            "n_sealed": r["n_sealed"],
            "max_streak": r["max_streak"],
            "participation": r["participation"],
            "factor_date": factor_date,
        }
    return out


def price_signals(
    close_series: list[tuple[str, float]],
    code: str,
    as_of: str | None = None,
    before: str | None = None,
) -> dict:
    """由日线收盘序列算位置与连板（序列为升序 [(YYYY-MM-DD, close)]）。

    全部基于**已发生的收盘价**；数据不足时对应字段为 None，不做外推。

    `before` 是**盘前口径**（严格小于）：D 日的盘前报告只能用 trade_date < D 的收盘，
    因为 D 日收盘在开盘前根本不存在。返回带 `close_date`（真实用到的最后一根收盘日），
    页面/正文必须显示这个真实时点，别让读者以为是报告日收盘。

    `as_of` 是旧口径（含当日）—— 只为兼容既有调用方保留；盘前路径一律用 `before`。

    20 日指标必须**足 21 根收盘**（20 个交易日涨跌需要 21 个收盘点）；不足就留空，
    不拿更短的窗口冒充「20 日」。
    """
    clean: list[tuple[str, float]] = []
    for d, c in close_series or []:
        day = _norm_day(d)
        if day is None:
            continue  # 日期认不出 → 这根不可用(不拼假日期)
        try:
            val = float(c)
        except (TypeError, ValueError):
            continue
        clean.append((day, val))

    if as_of:
        cut_as = _norm_day(as_of)
        if cut_as is None:
            return {
                "close": None,
                "close_date": None,
                "limit_up_streak": None,
                "pct_20d": None,
                "from_20d_high": None,
                "note": f"报告日无法解析({as_of!r}), 无法确认时点",
            }
        clean = [(d, c) for d, c in clean if d <= cut_as]

    cut = _norm_day(before) if before else None
    if before and cut is None:
        # 报告日认不出来 ⇒ 无法确定"哪些收盘在报告前", 一律不给数字(不默认放行)
        return {
            "close": None,
            "close_date": None,
            "limit_up_streak": None,
            "pct_20d": None,
            "from_20d_high": None,
            "note": f"报告日无法解析({before!r}), 无法确认盘前时点",
        }
    if cut:
        clean = [(d, c) for d, c in clean if d < cut]

    if len(clean) < 2:
        return {
            "close": None,
            "close_date": None,
            "limit_up_streak": None,
            "pct_20d": None,
            "from_20d_high": None,
            "note": (f"日线不足(报告日 {cut} 之前不足两根收盘)" if cut else "日线不足"),
        }
    closes = [c for _, c in clean]
    last = closes[-1]
    close_date = clean[-1][0]
    pct = None
    if closes[-2]:
        pct = round((last / closes[-2] - 1.0) * 100.0, 2)
    # 连板：从最后一根往前数收盘涨停
    limit = _limit_pct(code)
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        prev = closes[i - 1]
        if not prev:
            break
        chg = (closes[i] / prev - 1.0) * 100.0
        if chg >= limit - 0.3:  # 收盘涨幅接近涨停上限（含四舍五入误差）
            streak += 1
        else:
            break
    has_20d = len(closes) >= 21
    base20 = closes[-21] if has_20d else None
    pct20 = round((last / base20 - 1.0) * 100.0, 2) if base20 else None
    high20 = max(closes[-20:]) if has_20d else None
    from_high = round((last / high20 - 1.0) * 100.0, 2) if high20 else None
    notes: list[str] = []
    if cut:
        # 真实时点上屏: 这是「截至哪一天的收盘」, 不是报告日收盘
        notes.append(f"盘前口径: 截至 {close_date} 收盘(报告日 {cut} 收盘不计入)")
    if not has_20d:
        notes.append(f"20日指标需≥21根收盘, 当前{len(closes)}根, 已留空")
    return {
        "close": round(last, 2),
        "close_date": close_date,
        "pct": pct,
        "limit_up_streak": streak,
        "pct_20d": pct20,
        "from_20d_high": from_high,
        "note": "；".join(notes) or None,
    }


def _parse_stop_price(stop_text: str | None) -> float | None:
    """止损列只在写了**具体价位数**时才算；「-5%」「跌破5日线」这类条件式一律 None。"""
    if not stop_text:
        return None
    s = str(stop_text)
    if "%" in s or "线" in s or "跌破" in s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    v = _num(m.group(1))
    return v if v and v > 0 else None


def build_signal_detail(
    row: dict, *, demon: dict | None, price: dict | None, demon_note: str | None = None
) -> dict:
    """把一行潜伏表 + 真实信号拼成页面要的明细（缺项 = None，让前端显示「无数据」）。

    `demon_note` 说明股性因子为什么不可用（盘前时点不可证明）—— 页面/正文据此解释，
    不把缺失当 0，也不拿当前快照冒充当时信号。
    """
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "tags": row.get("tags") or [],
        "event": row.get("event") or "",
        "sector": row.get("sector") or "",
        "logic": row.get("logic") or "",
        "strength": row.get("strength") or "",
        "method": row.get("method") or "",
        "batch": row.get("batch") or "",
        "stop": row.get("stop") or "",
        "stop_price": _parse_stop_price(row.get("stop")),
        "target": row.get("target") or "",
        "falsify": row.get("falsify") or "",
        "signals": {
            "demon": demon or None,
            "price": price or None,
            "demon_note": demon_note or None,
        },
    }


# ── 明细块（确定性文本，可插入报告正文） ──────────────────────────────


def _fmt(v: Any, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "无数据"
    try:
        return f"{float(v):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "无数据"


def _demon_text(d: dict | None, note: str | None = None) -> str:
    if not d:
        # 拿不到 ≠ 0 分: 把「为什么拿不到」写出来（盘前不可得 / 无冻结凭证）
        return f"无数据({note})" if note else "无数据"
    score = d.get("score")
    if score is None:
        score = d.get("total")  # 生成时快照原始行用 total
    parts = []
    if score is not None:
        parts.append(f"{d.get('grade') or ''}{_fmt(score, 1)}".strip())
    if d.get("n_sealed") is not None:
        parts.append(f"封板{d['n_sealed']}次")
    if d.get("max_streak") is not None:
        parts.append(f"最高{d['max_streak']}连板")
    if d.get("factor_date"):
        parts.append(f"快照日{d['factor_date']}")
    return " / ".join(parts) if parts else "无数据"


def _price_text(p: dict | None) -> str:
    if not p or p.get("close") is None:
        return p.get("note") if p and p.get("note") else "无数据"
    # 收盘后面带真实交易日: 盘前视角下这是「上一交易日收盘」, 不是报告日收盘
    closing = f"收盘{_fmt(p.get('close'), 2)}"
    if p.get("close_date"):
        closing += f"({p['close_date']})"
    seg = [closing]
    if p.get("limit_up_streak") is not None:
        seg.append("涨停" if p.get("limit_up_streak") else f"连板{p['limit_up_streak']}")
    if p.get("from_20d_high") is not None:
        seg.append(f"距20日高{_fmt(p.get('from_20d_high'), 1, '%')}")
    if p.get("pct_20d") is not None:
        seg.append(f"20日{_fmt(p.get('pct_20d'), 1, '%')}")
    # 20 日指标因不足 21 根而留空时, 把原因一并写出来(不留给读者猜「为什么没有」)
    if p.get("pct_20d") is None and p.get("from_20d_high") is None and p.get("note"):
        seg.append(p["note"])
    return " · ".join(seg)


def build_signal_block(rows: list[dict], signals: dict[str, dict]) -> str:
    """生成 markdown 明细块（确定性文本，不含 AI 措辞）。"""
    if not rows:
        return ""
    lines = [
        "### 潜伏表 · 信号明细（数据口径，非 AI 生成）",
        "",
        "| 代码 | 名称 | 股性因子 | 位置 / 连板 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        sig = signals.get(r["code"]) or {}
        lines.append(
            f"| {r['code']} | {r.get('name') or ''} | "
            f"{_demon_text(sig.get('demon'), sig.get('demon_note'))} | "
            f"{_price_text(sig.get('price'))} |"
        )
    lines.append("")
    lines.append(
        "> 股性来自既有股性因子表（近一年封板/连板统计）；位置与连板由日线**收盘价**计算，"
        "未用盘中路径。**盘前口径**：位置只用报告日之前的收盘（括号里是真实收盘日）；"
        "股性因子只在**有生成时冻结、且生成时点在报告日 09:30 前**的快照时给出（并标快照日，"
        "不保证数据已截至报告日前一交易日），否则写「无数据」并注明原因。"
        "缺数据不做估算。上表的受益逻辑/埋伏方式/止损仍是 AI 判断，与本节数据分开看。"
    )
    return "\n".join(lines)


def insert_after_table(text: str, block: str) -> str:
    """把明细块插在潜伏表最后一行之后（找不到表就原样返回，绝不改动别的内容）。"""
    if not text or not block:
        return text
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "事件驱动前瞻" in line or "潜伏表" in line:
            start = i
            break
    if start is None:
        return text
    header_idx = None
    for i in range(start, min(len(lines), start + 40)):
        if lines[i].strip().startswith("|") and "代码" in lines[i]:
            header_idx = i
            break
    if header_idx is None:
        return text
    end = header_idx
    for i in range(header_idx + 1, len(lines)):
        if lines[i].strip().startswith("|"):
            end = i
            continue
        break
    return "\n".join(lines[: end + 1] + ["", block] + lines[end + 1 :])


def collect_signals(codes: list[str], *, demon_by_code: dict[str, dict] | None = None,
                    demon_notes: dict[str, str] | None = None,
                    verified_frozen: bool = False,
                    engine=None, start_day: str, report_day: str | None = None,
                    as_of: str | None = None) -> dict[str, dict]:
    """按代码取真实信号。任何一块取不到 → 该块 None（页面/文本显示「无数据」）。

    `report_day`（YYYY-MM-DD）是**盘前时点**：报告日 D 的盘前报告只能看 trade_date < D 的
    收盘。股性因子更严格 —— 只有调用方通过 `demon_evidence_from_record()`（历史）或
    `filter_demon_for_generation()`（生成时）验证过的冻结快照，才能以 `verified_frozen=True`
    传进来；否则本函数**不读** `demon_factors` 当前表，一律 unknown 并写明原因（不造历史）。
    不给 `report_day` 时保持历史调用行为（不做时点推断）。
    """
    out: dict[str, dict] = {}
    if not codes:
        return out
    notes: dict[str, str] = dict(demon_notes or {})
    if report_day:
        if verified_frozen:
            demon = demon_by_code or {}
        else:
            # PIT 模式: 没验证过的映射/当前表都不许当盘前信号
            demon = {}
            for sym in (demon_by_code or {}):
                code = str(sym).split(".")[0]
                notes.setdefault(
                    code, "股性因子无生成时冻结凭证, 盘前不可得(不用当前表/快照日重建历史)"
                )
    else:
        demon = demon_by_code
        if demon is None and engine is not None:
            try:
                with engine.connect() as conn:
                    demon = demon_map(conn, codes)
            except Exception as e:  # noqa: BLE001
                logger.debug("股性因子取数失败: %s", e)
                demon = {}
        demon = demon or {}

    closes_cache: dict[str, list] = {}
    if engine is not None:
        from src.core.decision_log import _close_series_pg

        for code in codes:
            try:
                closes_cache[code] = _close_series_pg(engine, code, start_day)
            except Exception as e:  # noqa: BLE001
                logger.debug("日线取数失败 %s: %s", code, e)
                closes_cache[code] = []

    for code in codes:
        row = demon.get(code)
        note = notes.get(code)
        if report_day and row is None and not note:
            note = "该股无生成时冻结的股性因子快照, 盘前不可得"
        out[code] = {
            "demon": row,
            "demon_note": None if row is not None else note,
            "price": price_signals(closes_cache.get(code) or [], code, as_of=as_of, before=report_day)
            if engine is not None
            else None,
        }
    return out
