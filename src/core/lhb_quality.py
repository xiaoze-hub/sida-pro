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
3. **参考源必须同市场才比较。** 本表有 `market` 列 → 按请求市场过滤; 参考表没有
   `market` 列 → 只有在该市场符号形态可核验(CN=6 位数字)时才比较, 否则**不比较**并
   记入 `references_skipped`。跨市场代码被当成缺口是误报, 必须避免。
4. **早写只是风险, 不是残缺结论, 更不是官方发布时间事实。** `first_write_at` 取
   `min(updated_at)`; `_lhb_upsert_rows` 走 `upsert_sql()`, 其 update 列**不含**
   `updated_at`, 所以该值就是"这份快照最早被写下的时刻"。`17:16` 是东财当日榜单
   齐全的**生产经验下界**(非官方发布时间), 首写早于它 → `early_freeze=True` 仅提示
   "可能发布于发布就绪前", 不改变 status。
5. **`unverified` 与 `partial` 必须可区分。** 两者都不能当完整, 但前者"无证据表明
   残缺", 后者"有证据证明残缺": 分别用 `verification`(unverified/disproven)、
   `severity`(caution/incomplete) 与 `label` 表达; `degraded` 只表示**已证不可当完整
   使用**(partial/absent), 不再是"非 complete 一律 degraded"的粗粒度标记。
6. 时间列按 DB 方言解读后再转北京(`src.core.timezone.to_beijing`): SQLite 的
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

# 状态 → (核验态, 严重度, 人读标签)。unverified/partial 都不能当完整, 但语义不同:
# 前者"没证据", 后者"有证据", 不允许用同一个 degraded=True 糊在一起。
_STATUS_META: dict[str, tuple[str, str, str]] = {
    STATUS_COMPLETE: ("authoritative", "ok", "完整(已由权威清单核对)"),
    STATUS_UNVERIFIED: ("unverified", "caution", "未经核对"),
    STATUS_PARTIAL: ("disproven", "incomplete", "已证残缺"),
    STATUS_ABSENT: ("no_snapshot", "missing", "无快照"),
}

# 东财当日完整榜单"发布就绪"时刻(HHMM, 北京)。这是**生产实测经验下界**
# (最早 17:16/稳定 17:30), 不是交易所/东财的官方发布时间公告。
PUBLISH_READY_HHMM = 1716
PUBLISH_READY_BASIS = "17:16 为生产实测的经验下界(非官方发布时间); 只提示早写风险, 不构成残缺结论"

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


def _is_cn_symbol(s) -> bool:
    """CN 符号形态: 6 位数字(可带 .SZ/.SH/.BJ 后缀)。用于无 market 列参考表的同市场核验。"""
    sym = str(s or "").strip()
    if "." in sym:
        sym = sym.split(".")[0]
    return len(sym) == 6 and sym.isdigit()


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
    market: str = "CN",
    actual_symbols: Iterable = (),
    first_write_at: datetime | str | None = None,
    last_write_at: datetime | str | None = None,
    sources: Mapping[str, int] | None = None,
    references: Mapping[str, Iterable] | None = None,
    reference_meta: Mapping[str, Mapping] | None = None,
    references_skipped: Mapping[str, str] | None = None,
    expected_symbols: Iterable | None = None,
    expected_source: str = "",
    expected_authoritative: bool = False,
    missing_sample: int = _MISSING_SAMPLE,
    publish_ready_hhmm: int = PUBLISH_READY_HHMM,
    db_error: str = "",
) -> dict:
    """给某交易日的 `dragon_tiger_daily` 快照定性(纯函数, 不碰 IO)。

    - `references`: {参考源名: 该源**同市场** symbol 集合}, **非权威**, 只当下界;
    - `reference_meta`: {参考源名: {"market": "CN", "market_verified": True}},
      声明市场与核验方式; 声明市场与 `market` 不同 → 该参考不参与比较(跨市场不比较);
    - `references_skipped`: {参考源名: 不比较的原因}, 原样透出, 不静默丢弃;
    - `expected_symbols` + `expected_source` + `expected_authoritative=True`: 调用方
      拿到的**官方完整清单**; 只有它被实际覆盖时 `status` 才是 `complete`。

    返回 dict 可直接 JSON 化。`complete` 只在权威清单覆盖时为真;
    `degraded` 表示**已证不可当完整使用**(partial/absent), 与"仅未经核对"(unverified)
    严格区分。
    """
    d = _norm_date(trade_date)
    market = str(market or "CN").strip().upper()
    actual = _norm_symbols(actual_symbols)
    fw = _as_naive_dt(first_write_at)
    lw = _as_naive_dt(last_write_at)
    sample = max(1, int(missing_sample))
    meta_in = reference_meta or {}
    skipped: dict[str, str] = {str(k): str(v) for k, v in (references_skipped or {}).items()}

    checks: list[dict] = []
    missing_all: set[str] = set()
    for name in sorted(references or {}):
        ref = _norm_symbols(references[name])
        if not ref:
            continue  # 参考源当日无数据 ≠ 快照完整
        meta = meta_in.get(name) or {}
        ref_market = str(meta.get("market") or "").strip().upper()
        if ref_market and market and ref_market != market:
            # 跨市场: 同一天两个市场的代码集不可比, 拿来做缺口会误报。
            skipped[name] = f"参考市场 {ref_market} ≠ 请求市场 {market}, 不比较"
            continue
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
                "market": ref_market or None,
                "market_verified": bool(meta.get("market_verified")),
                "market_scope": str(meta.get("scope") or "declared"),
                "note": _REFERENCE_NOTE,
            }
        )

    early_freeze = False
    early_freeze_note: str | None = None
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
    verification, severity, label = _STATUS_META[status]
    ready = f"{publish_ready_hhmm // 100:02d}:{publish_ready_hhmm % 100:02d}"

    evidence: list[str] = [f"actual_symbols={len(actual)}", f"market={market}"]
    warnings: list[str] = []
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
        early_freeze_note = (
            f"早写风险: first_write_at={_fmt_dt(fw)} 早于经验下界 {ready}(北京); "
            f"{PUBLISH_READY_BASIS}。"
        )
        evidence.append(early_freeze_note)
        warnings.append(
            f"early_freeze_risk: 首写 {_fmt_dt(fw)} 早于发布就绪经验下界 {ready}, "
            "该日快照可能在发布未完时落库 —— 风险提示, 不构成残缺结论, 也非官方发布时间。"
        )
    for name, why in sorted(skipped.items()):
        evidence.append(f"reference_skipped[{name}]={why}")
    if db_error:
        evidence.append(f"db_error={db_error}")

    worst_refs = [c for c in checks if c["missing"]]
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
        elif worst_refs:
            worst = max(worst_refs, key=lambda c: c["missing"])
            note = (
                f"残缺: 较 {worst['reference']} 少 {worst['missing']} 只"
                f"(该源当日 {worst['reference_symbols']} 只)。"
                "该参考为独立来源、非官方完整清单, 只能证明缺口、不能证明完整; "
                "本日快照**不得**作为完整榜单展示。"
            )
        else:
            # expected 非权威但含实际缺失的标的(原先会 max([]) 崩溃)。参考源非权威
            # 不等于缺口不存在: 它含本快照没有的真实标的, 就是残缺少一只的证据。
            exp = expected or {}
            note = (
                f"残缺: 非官方参考清单({exp.get('source') or '未标注'})内的 "
                f"{exp.get('missing', 0)} 只不在本快照中(清单 {exp.get('symbols', 0)} 只 / "
                f"实际 {len(actual)} 只)。该清单非官方完整清单, 只能证明缺口、不能证明完整; "
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
        note = ("未经官方清单核对: 与独立来源股数一致**不足以**判定完整"
                "(参考源非官方清单, 可能同样不完整)。无证据表明缺漏, 但展示时须标注为未经核对。")
        if skipped:
            note += " 另有参考源未参与比较(原因见 references_skipped, 不静默忽略)。"

    if status in (STATUS_PARTIAL, STATUS_UNVERIFIED):
        note += " 回源修复须显式授权(本评估只读, 不写库、不回源)。"
    if early_freeze_note:
        # 早写风险要"突出": 放到 note 开头, 不能只埋在 evidence 里。
        note = f"{early_freeze_note} {note}"

    return {
        "trade_date": d,
        "market": market,
        "status": status,
        "complete": complete,
        "degraded": status in (STATUS_PARTIAL, STATUS_ABSENT),
        "verification": verification,
        "severity": severity,
        "label": label,
        "actual_symbols": len(actual),
        "first_write_at": _fmt_dt(fw),
        "last_write_at": _fmt_dt(lw),
        "publish_ready_after": ready,
        "publish_ready_basis": PUBLISH_READY_BASIS,
        "early_freeze": early_freeze,
        "early_freeze_risk": early_freeze,
        "early_freeze_note": early_freeze_note,
        "sources": dict(sorted((sources or {}).items())),
        "references": checks,
        "references_skipped": dict(sorted(skipped.items())),
        "missing_total": len(missing_all),
        "missing_symbols": sorted(missing_all)[:sample],
        "missing_truncated": len(missing_all) > sample,
        "expected": expected,
        "evidence": evidence,
        "warnings": warnings,
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


def _fetch_references(
    trade_date: str, market: str = "CN"
) -> tuple[dict[str, list[str]], dict[str, dict], dict[str, str]]:
    """独立参考源的**同市场**当日 symbol 集合。

    返回 (references, reference_meta, references_skipped)。规则:
    - 参考表有 `market` 列 → 只取请求市场的行(`market_verified=True`);
    - 参考表没有 `market` 列 → 只有 CN 市场且全部代码是 6 位数字(形态可核验)时才比较
      (`market_verified=False`, scope=symbol_shape); 否则**不比较**并记录原因。
      拿跨市场代码当缺口是误报, 宁可不比。
    - 单表不可用/无行 → 跳过(不当作"完整")。
    """
    refs: dict[str, list[str]] = {}
    meta: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    market = str(market or "CN").strip().upper()
    try:
        from sqlalchemy import text

        from src.web.database import engine
    except Exception:  # noqa: BLE001
        return refs, meta, {"*": "数据库不可用"}

    for table in REFERENCE_TABLES:
        try:
            with engine.connect() as conn:
                res = conn.execute(
                    text(f"SELECT * FROM {table} WHERE trade_date = :d"),
                    {"d": trade_date},
                )
                keys = {str(k).lower() for k in res.keys()}
                rows = res.fetchall()
        except Exception as e:  # noqa: BLE001 - 表不存在/结构不同 → 该参考缺席
            skipped[table] = f"不可读({type(e).__name__})"
            continue
        if not rows:
            continue  # 参考源当日无数据 ≠ 快照完整

        if "market" in keys:
            same = [
                r._mapping.get("symbol")
                for r in rows
                if str(r._mapping.get("market") or "CN").strip().upper() == market
            ]
            syms = [s for s in same if s]
            if not syms:
                skipped[table] = f"该表当日无 {market} 市场行, 不跨市场比较"
                continue
            refs[table] = [str(s) for s in syms]
            meta[table] = {"market": market, "market_verified": True, "scope": "market_column"}
            continue

        # 无 market 列: 只能按代码形态核验同市场
        raw = [str(r._mapping.get("symbol") or "").strip() for r in rows]
        raw = [s for s in raw if s]
        if market != "CN":
            skipped[table] = f"无 market 列, {market} 市场无法核验同市场, 不比较"
            continue
        bad = [s for s in raw if not _is_cn_symbol(s)]
        if bad:
            skipped[table] = (
                f"无 market 列且 {len(bad)} 个代码非 6 位 CN 形态(如 {bad[0]}), "
                "无法核验同市场, 不比较"
            )
            continue
        refs[table] = raw
        meta[table] = {"market": "CN", "market_verified": False, "scope": "symbol_shape"}
    return refs, meta, skipped


def beijing_write_window(values: Iterable) -> tuple[datetime | None, datetime | None]:
    """DB 时间列集合 → (首写, 末写) 北京 naive; 按方言解读, 空集合 → (None, None)。"""
    stamps = [_to_beijing_naive(_as_naive_dt(v)) for v in values or ()]
    stamps = [s for s in stamps if s]
    return (min(stamps), max(stamps)) if stamps else (None, None)


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
    market = str(market or "CN").strip().upper()
    d = _norm_date(trade_date)
    if not d:
        return assess_daily_completeness(
            trade_date, market=market, actual_symbols=[], db_error="非法交易日"
        )
    rows, err = _fetch_snapshot(d, market)
    refs, meta, skipped = _fetch_references(d, market)
    fw, lw = beijing_write_window([r[2] for r in rows if r[2]])
    src_counter = Counter(str(r[1] or "unknown") for r in rows)
    return assess_daily_completeness(
        d,
        market=market,
        actual_symbols=[r[0] for r in rows],
        first_write_at=fw,
        last_write_at=lw,
        sources=dict(src_counter),
        references=refs,
        reference_meta=meta,
        references_skipped=skipped,
        db_error=err,
    )


def assess_events_date(
    trade_date,
    market: str = "CN",
    *,
    event_symbols: Iterable = (),
    first_write_at: datetime | str | None = None,
    last_write_at: datetime | str | None = None,
    daily_symbols: Iterable | None = None,
    db_error: str = "",
) -> dict:
    """给 `dragon_tiger_events`(archive/events 页面实际读的表)做**独立**定性。

    与 `assess_daily_completeness` 的区别: 本表是定时回源落库的独立来源, 没有可用的
    官方完整清单 → **永不声明完整**(`complete=False`, `completeness_claimed=False`,
    `verified_against_official=False`), 也不因"没核对"就谎报残缺:
    - 有行 → `status=unverified` / `severity=caution` / `degraded=False`(无残缺证据);
    - 无行 → `status=absent` / `severity=missing` / `degraded=True`(确实没数据)。

    `daily_symbols` 给了就做**双向交叉提示**(仅供人看: 两张表都不是官方清单, 差异不能
    判定任何一方完整), 不用它推 status。
    """
    d = _norm_date(trade_date)
    market = str(market or "CN").strip().upper()
    ev = _norm_symbols(event_symbols)
    fw = _as_naive_dt(first_write_at)
    lw = _as_naive_dt(last_write_at)

    status = STATUS_ABSENT if not ev else STATUS_UNVERIFIED
    complete = False
    verification = "no_snapshot" if status == STATUS_ABSENT else "unverified"
    severity = "missing" if status == STATUS_ABSENT else "caution"
    label = "无快照" if status == STATUS_ABSENT else "未经核对"
    ready = f"{PUBLISH_READY_HHMM // 100:02d}:{PUBLISH_READY_HHMM % 100:02d}"

    early_freeze = False
    early_freeze_note: str | None = None
    warnings: list[str] = []
    evidence: list[str] = [f"table=dragon_tiger_events", f"market={market}", f"actual_symbols={len(ev)}"]
    if fw:
        evidence.append(f"first_write_at={_fmt_dt(fw)}")
    try:
        td = datetime.strptime(d, "%Y%m%d").date() if d else None
    except ValueError:
        td = None
    if fw and td and fw.date() == td and _hhmm(fw) < PUBLISH_READY_HHMM:
        early_freeze = True
        early_freeze_note = (
            f"早写风险: created_at 最早 {_fmt_dt(fw)} 早于经验下界 {ready}(北京); "
            f"{PUBLISH_READY_BASIS}。"
        )
        evidence.append(early_freeze_note)
        warnings.append(
            f"early_freeze_risk: 最早写入 {_fmt_dt(fw)} 早于发布就绪经验下界 {ready}, "
            "该表可能在发布未完时回源落库 —— 风险提示, 不构成残缺结论, 也非官方发布时间。"
        )

    cross_check: dict | None = None
    if daily_symbols is not None:
        daily = _norm_symbols(daily_symbols)
        cross_check = {
            "daily_symbols": len(daily),
            "only_in_events": len(ev - daily),
            "only_in_daily": len(daily - ev),
            "note": (
                "双向差异仅作交叉提示: dragon_tiger_events 与 dragon_tiger_daily "
                "均非官方完整清单, 不能据此判定任何一方完整"
            ),
        }

    if status == STATUS_ABSENT:
        note = "该日 dragon_tiger_events 无行(定时回源未覆盖/非交易日); 无覆盖不等于无上榜。"
    else:
        note = (
            "dragon_tiger_events 为定时回源落库的独立来源, 无官方完整清单可核对: "
            "不声明完整, 也无证据表明残缺。"
        )
    if db_error:
        note += f" (读库异常: {db_error})"
        evidence.append(f"db_error={db_error}")
    if early_freeze_note:
        note = f"{early_freeze_note} {note}"

    return {
        "table": "dragon_tiger_events",
        "trade_date": d,
        "market": market,
        "status": status,
        "complete": complete,
        "degraded": status == STATUS_ABSENT,
        "verification": verification,
        "severity": severity,
        "label": label,
        "completeness_claimed": False,
        "verified_against_official": False,
        "actual_symbols": len(ev),
        "first_write_at": _fmt_dt(fw),
        "last_write_at": _fmt_dt(lw),
        "publish_ready_after": ready,
        "publish_ready_basis": PUBLISH_READY_BASIS,
        "early_freeze": early_freeze,
        "early_freeze_risk": early_freeze,
        "early_freeze_note": early_freeze_note,
        "cross_check": cross_check,
        "evidence": evidence,
        "warnings": warnings,
        "note": note,
    }


__all__ = [
    "PUBLISH_READY_BASIS",
    "PUBLISH_READY_HHMM",
    "REFERENCE_TABLES",
    "STATUS_ABSENT",
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_UNVERIFIED",
    "assess_date",
    "assess_daily_completeness",
    "assess_events_date",
    "beijing_write_window",
]
