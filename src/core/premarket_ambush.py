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
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

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


def demon_map(db, codes: Iterable[str]) -> dict[str, dict]:
    """从 `demon_factors` 表取股性因子（表缺失/查询失败 → {}，不猜）。"""
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
                "SELECT symbol, name, total, grade, n_sealed, max_streak, participation "
                f"FROM demon_factors WHERE symbol IN ({ph})"
            ),
            params,
        ).mappings().all()
    except Exception as e:  # noqa: BLE001 — 表不存在(SQLite/早期库)也算取不到
        logger.debug("股性因子查询失败: %s", e)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        code = str(r["symbol"]).split(".")[0]
        if code in out:  # 多个市场代码变体时保留第一条（结果确定）
            continue
        out[code] = {
            "score": _num(r["total"]),
            "grade": r["grade"],
            "n_sealed": r["n_sealed"],
            "max_streak": r["max_streak"],
            "participation": r["participation"],
        }
    return out


def price_signals(close_series: list[tuple[str, float]], code: str, as_of: str | None = None) -> dict:
    """由日线收盘序列算位置与连板（序列为升序 [(YYYY-MM-DD, close)]）。

    全部基于**已发生的收盘价**；数据不足时对应字段为 None，不做外推。

    `as_of` 用于按报告日截断 —— 否则拿今天的位置去给一周前的判断当"当时信号"，
    数字是真的但时点错位（比错数字更难发现）。
    """
    if as_of:
        close_series = [(d, c) for d, c in close_series if str(d)[:10] <= str(as_of)[:10]]
    if not close_series or len(close_series) < 2:
        return {"close": None, "limit_up_streak": None, "pct_20d": None, "from_20d_high": None, "note": "日线不足"}
    closes = [c for _, c in close_series]
    last = closes[-1]
    pct = None
    if len(closes) >= 2 and closes[-2]:
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
    base20 = closes[-21] if len(closes) >= 21 else closes[0]
    pct20 = round((last / base20 - 1.0) * 100.0, 2) if base20 else None
    window = closes[-20:] if len(closes) >= 20 else closes
    high20 = max(window) if window else None
    from_high = round((last / high20 - 1.0) * 100.0, 2) if high20 else None
    return {
        "close": round(last, 2),
        "pct": pct,
        "limit_up_streak": streak,
        "pct_20d": pct20,
        "from_20d_high": from_high,
        "note": None,
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


def build_signal_detail(row: dict, *, demon: dict | None, price: dict | None) -> dict:
    """把一行潜伏表 + 真实信号拼成页面要的明细（缺项 = None，让前端显示「无数据」）。"""
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


def _demon_text(d: dict | None) -> str:
    if not d:
        return "无数据"
    parts = []
    if d.get("score") is not None:
        parts.append(f"{d.get('grade') or ''}{_fmt(d.get('score'), 1)}".strip())
    if d.get("n_sealed") is not None:
        parts.append(f"封板{d['n_sealed']}次")
    if d.get("max_streak") is not None:
        parts.append(f"最高{d['max_streak']}连板")
    return " / ".join(parts) if parts else "无数据"


def _price_text(p: dict | None) -> str:
    if not p or p.get("close") is None:
        return p.get("note") if p and p.get("note") else "无数据"
    seg = [f"收盘{_fmt(p.get('close'), 2)}"]
    if p.get("limit_up_streak") is not None:
        seg.append("涨停" if p.get("limit_up_streak") else f"连板{p['limit_up_streak']}")
    if p.get("from_20d_high") is not None:
        seg.append(f"距20日高{_fmt(p.get('from_20d_high'), 1, '%')}")
    if p.get("pct_20d") is not None:
        seg.append(f"20日{_fmt(p.get('pct_20d'), 1, '%')}")
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
            f"| {r['code']} | {r.get('name') or ''} | {_demon_text(sig.get('demon'))} | "
            f"{_price_text(sig.get('price'))} |"
        )
    lines.append("")
    lines.append(
        "> 股性来自既有股性因子表（近一年封板/连板统计）；位置与连板由日线**收盘价**计算，"
        "未用盘中路径。缺数据的格子写「无数据」，不做估算。上表的受益逻辑/埋伏方式/止损仍是 AI 判断，"
        "与本节数据分开看。"
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
                    engine=None, start_day: str, as_of: str | None = None) -> dict[str, dict]:
    """按代码取真实信号。任何一块取不到 → 该块 None（页面/文本显示「无数据」）。

    `demon_by_code` 可由调用方直接给（报告生成时手里已有因子映射，省一次查询）。
    """
    out: dict[str, dict] = {}
    if not codes:
        return out
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
        out[code] = {
            "demon": demon.get(code),
            "price": price_signals(closes_cache.get(code) or [], code, as_of=as_of)
            if engine is not None
            else None,
        }
    return out
