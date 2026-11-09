"""龙虎榜日快照完整性定性(`dragon_tiger_daily` 提前冻结残缺, 2026-11-09)。

## 缺陷(已核对的真实代码路径)

`dragon_tiger_daily` 是**读侧请求驱动**落库的:

  `GET /api/market-data/dragon-tiger/{date}`
    → `market_data.dragon_tiger_proxy()`
    → 库里有该日行? `_dragon_tiger_from_db()` 直接返回(**历史日永不回源**)
    → 库里没有: 回源东财 + ftshare, 拉到即 `_lhb_upsert_rows()` 落库

东财**当日完整榜单**最早约 17:16、稳定 17:30 才齐(生产实测 2026-10-19 ~ 11-06),
而请求可以发生在 16:26–16:51(收盘后、发布未完)。那一刻落库的是**残缺榜单**,
之后同一日永远命中"已落库 → 读库"短路, 于是残缺被**永久冻结**, 而响应体仍然只有
`count=N / source=db`, 读侧无从分辨。`_compute_lhb_range()` 里是同一个短路。

所以缺陷不是"数据源缺数据", 而是**残缺快照被当成完整快照展示**。本模块只做
"诚实地给这份快照定性": 不写入、不回源、不修复(回源修复必须显式授权后另行执行)。

## 口径铁律

1. **不得凭股数一致宣称完整。** `dragon_tiger_events` / `lhb_seat_details` 是**独立
   来源**(同样来自东财, 但走独立的接口与落库链路), 不是交易所/东财的官方完整清单。
   它们只能当下界 —— 能**证伪**完整, 不能**证明**完整。故 `status` 只有在调用方
   显式提供**权威完整清单**且实际覆盖时才可能是 `complete`。
2. **缺口必须可证明。** 参考源里有、本快照没有的 symbol 逐个列出(`missing_symbols`),
   而不是"看起来少"。
3. **早冻是一维证据。** `first_write_at` 取 `min(updated_at)`; `_lhb_upsert_rows` 走
   `upsert_sql()`, 其 update 列**不含** `updated_at`, 所以该值就是"这份快照最早被写下
   的时刻"。落在发布就绪窗口之前 → `early_freeze=True`(证据, 不等于已证明完整)。
4. **非 complete 一律 `degraded=True`。** 读侧据此标注, 不允许当完整榜单使用。
5. 时间列按 DB 方言解读后再转北京(`src.core.timezone.to_beijing`): SQLite 的
   `CURRENT_TIMESTAMP` 是 UTC naive, PG(Asia/Shanghai) 的 `now()` 是北京 naive。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone

_CST = timezone(timedelta(hours=8))   # Asia/Shanghai 自 1991 起无夏令时, 固定 +08:00

STATUS_ABSENT = "absent"          # 该日没有任何快照行
STATUS_UNVERIFIED = "unverified"  # 有行, 但没拿到官方完整清单核对 → 不得称完整
STATUS_PARTIAL = "partial"        # 可证明残缺(参考源有而本快照没有)
STATUS_COMPLETE = "complete"      # 仅当权威完整清单已被覆盖

# 东财当日完整榜单"发布就绪"时刻(HHMM, 北京)。生产实测最早 17:16、稳定 17:30;
# 取 17:16 作下界: 早于此首写的一律视为"发布未完就落库"的证据。
PUBLISH_READY_HHMM = 1716

# 独立参考源(表名): 只作下界, 不是官方完整清单。
REFERENCE_TABLES: tuple[str, ...] = ("dragon_tiger_events", "lhb_seat_details")

_REFERENCE_NOTE = "独立来源(非官方完整清单): 只能证伪完整, 不能证明完整"
_MISSING_SAMPLE = 20


# ─────────────────────────── 归一化 ───────────────────────────


def _norm_date(trade_date) -> str:
    """'2026-10-19' / '20261019' / date → '20261019'; 非法 → ''。"""
    if isinstance(trade_date, (date, datetime)):
        return trade_date.strftime("%Y%m%d")
    s = str(trade_date or "").strip().replace("-", "")[:8]
    return s if len(s) == 8 and s.isdigit() else ""


def _norm_symbols(symbols: Iterable) -> set[str]:
    """去后缀 / 去空白 / 丢空值, 统一成 6 位码(不做位数校验以外的事)。"""
    out: set[str] = set()
    for s in symbols or ():
        sym = str(s or "").strip()
        if "." in sym:
            sym = sym.split(".")[0]
        if sym:
            out.add(sym)
    return out


def _as_naive_dt(v) -> datetime | None:
    """时间值(str/datetime) → 北京 naive datetime; 失败返回 None。

    **naive 输入原样保留**(调用方口径约定为北京, 这里不做方言猜测 —— 否则
    `assess_date` 已转过一次方言时会被二次平移); aware 输入统一转 +08:00 再落 naive。
    """
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.astimezone(_CST).replace(tzinfo=None) if v.tzinfo else v
    s = str(v).strip().replace("T", " ").rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(_CST).replace(tzinfo=None) if dt.tzinfo else dt


def _fmt_dt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _hhmm(moment: datetime) -> int:
    return moment.hour * 100 + moment.minute


# ─────────────────────────── 纯函数: 定性 ───────────────────────────


def assess_daily_completeness(
    trade_date,
    *,
    actual_symbols: Iterable = (),
    first_write_at: datetime | str | None = None,
    last_write_at: datetime | str | None = None,
    sources: Mapping[str, int] | None = None,
    references: Mapping[str, Iterable] | None = None,
    expected_symbols: Iterable | None = None,
    expected_source: str = "",
    expected_authoritative: bool = False,
    missing_sample: int = _MISSING_SAMPLE,
    publish_ready_hhmm: int = PUBLISH_READY_HHMM,
    db_error: str = "",
) -> dict:
    """给某交易日的 `dragon_tiger_daily` 快照定性(纯函数, 不碰 IO)。

    - `references`: {参考源名: 该源当日 symbol 集合}, **非权威**, 只当下界;
    - `expected_symbols` + `expected_source` + `expected_authoritative=True`: 调用方
      拿到的**官方完整清单**; 只有它被实际覆盖时 `status` 才是 `complete`。

    返回 dict 可直接 JSON 化; `degraded = status != complete`。
    """
    d = _norm_date(trade_date)
    actual = _norm_symbols(actual_symbols)
    fw = _as_naive_dt(first_write_at)
    lw = _as_naive_dt(last_write_at)
    sample = max(1, int(missing_sample))

    checks: list[dict] = []
    missing_all: set[str] = set()
    for name in sorted(references or {}):
        ref = _norm_symbols(references[name])
        if not ref:
            continue  # 参考源当日无数据 ≠ 快照完整
        missing = ref - actual
        extra = actual - ref
        missing_all |= missing
        checks.append(
            {
                "reference": str(name),
                "reference_symbols": len(ref),
                "missing": len(missing),
                "extra": len(extra),
                "relation": ("equal" if not missing and not extra
                             else "subset" if not extra
                             else "mismatch" if missing else "superset"),
                "authoritative": False,
                "note": _REFERENCE_NOTE,
            }
        )

    early_freeze = False
    if fw and d:
        try:
            td = datetime.strptime(d, "%Y%m%d").date()
        except ValueError:
            td = None
        if td and fw.date() == td and _hhmm(fw) < int(publish_ready_hhmm):
            early_freeze = True

    expected: dict | None = None
    expected_covered = False
    if expected_symbols is not None:
        exp = _norm_symbols(expected_symbols)
        expected_covered = bool(exp) and exp <= actual
        missing_exp = exp - actual
        missing_all |= missing_exp
        expected = {
            "source": expected_source or "未标注",
            "symbols": len(exp),
            "authoritative": bool(expected_authoritative),
            "covered": expected_covered,
            "missing": len(missing_exp),
        }

    exp_authoritative = bool(expected and expected["authoritative"])
    if not actual:
        status = STATUS_ABSENT
    elif exp_authoritative and expected_covered:
        status = STATUS_COMPLETE
    elif missing_all:
        status = STATUS_PARTIAL
    else:
        status = STATUS_UNVERIFIED

    complete = status == STATUS_COMPLETE
    ready = f"{publish_ready_hhmm // 100:02d}:{publish_ready_hhmm % 100:02d}"

    evidence: list[str] = [f"actual_symbols={len(actual)}"]
    if fw:
        evidence.append(f"first_write_at={_fmt_dt(fw)}")
    for c in checks:
        if c["missing"]:
            evidence.append(
                f"missing_vs_{c['reference']}={c['missing']}"
                f"(该源 {c['reference_symbols']} 只)"
            )
    if expected is not None:
        evidence.append(
            f"expected_{expected['source']}: symbols={expected['symbols']} "
            f"authoritative={expected['authoritative']} covered={expected['covered']}"
        )
    if early_freeze:
        evidence.append(
            f"early_freeze(提前冻结): first_write_at={_fmt_dt(fw)} "
            f"< publish_ready={ready}(北京)"
        )
    if db_error:
        evidence.append(f"db_error={db_error}")

    if status == STATUS_COMPLETE:
        src_label = (expected or {}).get("source") or "未标注"
        note = (
            f"已由权威完整清单({src_label})核对覆盖, 可视为完整; "
            "金额等字段口径见各行 source。"
        )
    elif status == STATUS_PARTIAL:
        if exp_authoritative and expected and expected["missing"]:
            note = (
                f"残缺: 权威完整清单({expected['source']})内的 {expected['missing']} 只"
                f"不在本快照中(清单 {expected['symbols']} 只 / 实际 {len(actual)} 只); "
                "不得作为完整榜单展示。"
            )
        else:
            worst = max(
                (c for c in checks if c["missing"]),
                key=lambda c: c["missing"],
            )
            note = (
                f"残缺: 较 {worst['reference']} 少 {worst['missing']} 只"
                f"(该源当日 {worst['reference_symbols']} 只)。"
                "该参考为独立来源、非官方完整清单, 只能证明缺口、不能证明完整; "
                "本日快照**不得**作为完整榜单展示。"
            )
    elif status == STATUS_ABSENT:
        if db_error:
            note = (f"读库失败, 无法判定该日快照({db_error}); "
                    "不据此声明完整, 也不据此声明无上榜。")
        else:
            note = ("该日无 dragon_tiger_daily 快照行(未落库/非交易日); "
                    "无覆盖不等于无上榜。")
    else:
        note = ("未经官方完整清单核对: 与独立来源股数一致**不足以**判定完整"
                "(参考源非官方清单, 且可能同样残缺)。展示时须标注为未经核对。")

    if not complete:
        note += " 回源修复须显式授权(本评估只读, 不写库、不回源)。"

    return {
        "trade_date": d,
        "status": status,
        "complete": complete,
        "degraded": not complete,
        "actual_symbols": len(actual),
        "first_write_at": _fmt_dt(fw),
        "last_write_at": _fmt_dt(lw),
        "publish_ready_after": ready,
        "early_freeze": early_freeze,
        "sources": dict(sorted((sources or {}).items())),
        "references": checks,
        "missing_total": len(missing_all),
        "missing_symbols": sorted(missing_all)[:sample],
        "missing_truncated": len(missing_all) > sample,
        "expected": expected,
        "evidence": evidence,
        "note": note,
    }


# ─────────────────────────── 读库(只读) ───────────────────────────


def _fetch_snapshot(trade_date: str, market: str) -> tuple[list[tuple], str]:
    """读该日快照行 [(symbol, source, updated_at)]; 失败返回 ([], 原因)。"""
    try:
        from sqlalchemy import text

        from src.web.database import engine

        with engine.connect() as conn:
            recs = conn.execute(
                text(
                    "SELECT symbol, source, updated_at FROM dragon_tiger_daily "
                    "WHERE trade_date = :d AND market = :m"
                ),
                {"d": trade_date, "m": market or "CN"},
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in recs], ""
    except Exception as e:  # noqa: BLE001 - 表缺失/库不可达: 如实标 absent
        return [], f"{type(e).__name__}: {e}"


def _fetch_references(trade_date: str) -> dict[str, list[str]]:
    """独立参考源的当日 symbol 集合; 单表不可用就跳过(不当作"完整")。"""
    out: dict[str, list[str]] = {}
    try:
        from sqlalchemy import text

        from src.web.database import engine
    except Exception:  # noqa: BLE001
        return out
    for table in REFERENCE_TABLES:
        try:
            with engine.connect() as conn:
                recs = conn.execute(
                    text(f"SELECT DISTINCT symbol FROM {table} WHERE trade_date = :d"),
                    {"d": trade_date},
                ).fetchall()
        except Exception:  # noqa: BLE001 - 表不存在/结构不同 → 该参考缺席
            continue
        syms = [r[0] for r in recs if r[0]]
        if syms:
            out[table] = syms
    return out


def _to_beijing_naive(dt: datetime | None) -> datetime | None:
    """DB 读出的 naive 时间 → 北京 naive(按方言解读, 见模块 docstring)。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(_CST).replace(tzinfo=None)
    try:
        from src.core.timezone import to_beijing

        return to_beijing(dt).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - 时区工具不可用时按原值(宁可少判早冻)
        return dt


def assess_date(trade_date, market: str = "CN") -> dict:
    """某交易日快照的完整性定性(**只读**: 不回源、不写库)。"""
    d = _norm_date(trade_date)
    if not d:
        return assess_daily_completeness(
            trade_date, actual_symbols=[], db_error="非法交易日"
        )
    rows, err = _fetch_snapshot(d, market)
    refs = _fetch_references(d)
    stamps = [_to_beijing_naive(_as_naive_dt(r[2])) for r in rows if r[2]]
    stamps = [s for s in stamps if s]
    src_counter = Counter(str(r[1] or "unknown") for r in rows)
    return assess_daily_completeness(
        d,
        actual_symbols=[r[0] for r in rows],
        first_write_at=min(stamps) if stamps else None,
        last_write_at=max(stamps) if stamps else None,
        sources=dict(src_counter),
        references=refs,
        db_error=err,
    )


__all__ = [
    "PUBLISH_READY_HHMM",
    "REFERENCE_TABLES",
    "STATUS_ABSENT",
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_UNVERIFIED",
    "assess_date",
    "assess_daily_completeness",
]
