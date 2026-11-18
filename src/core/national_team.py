"""国家队 / 机构持仓（汇金 · 证金 · 外汇局 · 社保 · 险资）采集与读取（2026-11-18）。

## 数据来源（实测可用，非推测）

东方财富 `datacenter-web` 两张股东报表：

| 报表 | 含义 | 备注 |
|---|---|---|
| `RPT_F10_EH_FREEHOLDERS` | 十大**流通**股东 | 覆盖更全（进不了十大股东但进了十大流通股东的也会出现） |
| `RPT_F10_EH_HOLDERS` | 十大股东 | 多一列 `HOLD_NUM_RATIO`（持股比例），银行类大股东口径 |

源端**不支持 `like`**（`filter=(HOLDER_NAME like "%汇金%")` → `不支持like查询`），
但支持**精确等值**与 `in (...)`。

## 采集的两条路径（2026-11-18 加入社保/险资后重构）

1. **全市场扫描 `scan_period`**：按报告期拉整张表（一期约 5.6 万行 / `pageSize=2000` 29 页），
   本地按**名称规则**归类（汇金 / 证金 / 外汇局 / 社保 / 险资）。这是唯一能拿到
   「社保基金各组合」「保险公司各产品账户」的口径 —— 这些名字事先不可枚举（社保组合上百个、
   险资产品账户数百个），只能先从全量里认出来。
2. **定向采集 `sync_targeted`**：用扫描产出的 `national_team_holder_registry` 名单，
   按 `in (...)` 每期每表几次请求即可刷新 —— 日常/手动刷新走这条，秒级完成。

调度器在**新报告期出现时**跑一次全市场扫描（后台线程，几分钟），之后每日检查只做定向刷新。

## 口径与硬限制（页面必须显式标注，不得省略）

1. **只有进入前十大股东 / 十大流通股东的持仓可见**，第 11 名以后不可见（披露规则，不是采集缺陷）。
2. **没有基金持有人报表**：汇金真正的重仓是沪深300 等宽基 ETF，只在基金半年报/年报的
   「前十大持有人」里披露，本模块（以及东财任何 datacenter 报表）都拿不到 —— 本表是
   **个股直投**口径，不是国家队总资产。
3. **A+H 混行**：建行等 `SHARES_TYPE` 为 `A股,H股`，A/H 未拆分；`market_cap` 直接取源端
   `HOLDER_MARKET_CAP`，不自行乘价重算（避免口径漂移）。
4. **报告期口径，不是实时持仓**：`report_date` 来自源端 `END_DATE`，季报/年报披露节奏。
5. **险资含控股股东自有账户**：中国人寿、中国平安等集团本级自有资金账户也计入险资，
   所以险资合计里含「平安自有资金持平安」这类控股持仓，不是纯二级市场买入。

## 不编造

- 上游失败（网络/配置/解析）→ 抛 `NationalTeamFetchError`，**不写 0 行**；
- 上游明确「返回数据为空」→ 该报告期该表记为 0 行；
- 单行缺 `hold_num` / 代码非法 → 丢弃并计数，全部丢弃则整批失败（不是「没持仓」）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.db.dialect import upsert_sql

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

BASE_URL = "http://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE = "eastmoney_datacenter"

TABLE = "national_team_holdings"
REGISTRY_TABLE = "national_team_holder_registry"

#: 两张报表 → 落库的 report_kind
REPORT_TABLES: dict[str, str] = {
    "float": "RPT_F10_EH_FREEHOLDERS",  # 十大流通股东
    "total": "RPT_F10_EH_HOLDERS",  # 十大股东（含持股比例）
}
REPORT_KIND_LABEL: dict[str, str] = {"float": "十大流通股东", "total": "十大股东"}

#: 机构分组（页面按组汇总；组内可含多个具体持有人账户）
GROUPS: dict[str, dict[str, str]] = {
    "huijin": {"label": "汇金系", "desc": "中央汇金投资 + 中央汇金资产管理"},
    "csf": {"label": "证金公司", "desc": "中国证券金融股份有限公司"},
    "safe": {"label": "外汇局三平台", "desc": "梧桐树 / 凤山 / 坤藤"},
    "ssb": {"label": "社保基金", "desc": "全国社保基金各组合 + 理事会转持户"},
    "insurance": {"label": "险资", "desc": "保险公司及产品账户（含控股股东自有账户）"},
}
GROUP_ORDER: tuple[str, ...] = ("huijin", "csf", "ssb", "insurance", "safe")

#: 已知主体（用于定向采集的种子；扫描会补齐社保组合 / 险资产品账户）
HOLDERS: tuple[dict[str, str], ...] = (
    {"key": "huijin_invest", "name": "中央汇金投资有限责任公司", "group": "汇金·投资", "group_key": "huijin"},
    {"key": "huijin_asset", "name": "中央汇金资产管理有限责任公司", "group": "汇金·资管", "group_key": "huijin"},
    {"key": "csf", "name": "中国证券金融股份有限公司", "group": "证金公司", "group_key": "csf"},
    {"key": "wutongshu", "name": "梧桐树投资平台有限责任公司", "group": "外汇局·梧桐树", "group_key": "safe"},
    {"key": "fengshan", "name": "北京凤山投资有限责任公司", "group": "外汇局·凤山", "group_key": "safe"},
    {"key": "kunteng", "name": "北京坤藤投资有限责任公司", "group": "外汇局·坤藤", "group_key": "safe"},
)

HOLDER_BY_KEY: dict[str, dict[str, str]] = {h["key"]: h for h in HOLDERS}
HOLDER_BY_NAME: dict[str, dict[str, str]] = {h["name"]: h for h in HOLDERS}

#: 险资名称规则（社保/汇金等先判，避免误收）
_INSURANCE_MARKERS: tuple[str, ...] = (
    "人寿保险",
    "财产保险",
    "保险股份有限公司",
    "保险(集团)",
    "保险（集团）",
    "养老保险",
    "再保险",
)

#: 落库列（除主键 `report_date` / `report_kind` / `holder_key` / `stock_code` 外）
FIELDS: tuple[str, ...] = (
    "holder_name",
    "holder_group",
    "group_key",
    "stock_name",
    "hold_num",
    "hold_ratio",
    "market_cap",
    "holder_rank",
    "change_type",
    "shares_type",
    "snapshot_at",
    "source",
)
KEY_COLS: tuple[str, ...] = ("report_date", "report_kind", "holder_key", "stock_code")
ALL_COLS: tuple[str, ...] = KEY_COLS + FIELDS

CALIBER_NOTE = (
    "口径：东方财富十大股东/十大流通股东（季报披露），只含进入前十大的持仓；"
    "不含宽基 ETF 持仓（基金半年报/年报「前十大持有人」另计）；A+H 行未拆分；"
    "险资含保险公司控股股东自有账户。"
)

HOLDER_SCOPE_NOTE = (
    "覆盖：中央汇金（投资/资管）、证金公司、全国社保基金各组合、险资（保险公司及产品账户）、"
    "外汇局三平台（梧桐树/凤山/坤藤）。合计是个股直投口径，不等于国家队总资产。"
)


class NationalTeamFetchError(RuntimeError):
    """上游取数失败（网络/配置/解析）。绝不降级成「无持仓」。"""


# ────────────────────────── 归类 ──────────────────────────


def classify_holder(name: str | None) -> str | None:
    """按名称把持有人归到机构组；不匹配返回 None（表示与国家队/机构无关）。

    顺序有意义：先排汇金/证金/外汇局/社保，再判险资 —— 否则会被「保险」这类泛词误收。
    """
    n = (name or "").strip()
    if not n:
        return None
    if n.startswith("中央汇金"):
        return "huijin"
    if n == "中国证券金融股份有限公司":
        return "csf"
    if ("梧桐树投资平台" in n) or n.startswith("北京凤山投资") or n.startswith("北京坤藤投资"):
        return "safe"
    if n.startswith("全国社保基金") or n.startswith("全国社会保障基金"):
        return "ssb"
    if any(m in n for m in _INSURANCE_MARKERS):
        return "insurance"
    return None


def holder_key_for(name: str, group_key: str) -> str:
    """稳定短键：组前缀 + 名称哈希（避免长中文名超过列宽，也让同名主体跨期可对齐）。"""
    digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:12]
    return f"{group_key}:{digest}"


# ────────────────────────── HTTP ──────────────────────────


def _http_json(
    params: Mapping[str, str],
    *,
    http: Callable[..., Any] | None = None,
    tries: int = 4,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """请求 datacenter-web 并解析 JSON，带重试。

    实测坑：大页偶发 `http.client.IncompleteRead`（chunked 断流），必须重试。
    源端故障时返回 `success:false` + `message`，由调用方区分
    「报表配置不存在 / 返回数据为空 / 其他错误」。
    """
    url = BASE_URL + "?" + urllib.parse.urlencode(dict(params))
    last: Exception | None = None
    for attempt in range(tries):
        try:
            if http is not None:
                payload = http(url, timeout)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 sida"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    payload = resp.read()
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", "ignore")
            if isinstance(payload, str):
                return json.loads(payload)
            if isinstance(payload, dict):
                return payload
            raise NationalTeamFetchError(f"上游返回类型异常: {type(payload)!r}")
        except NationalTeamFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 — 断流/超时/JSON 截断统一重试
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise NationalTeamFetchError(f"上游请求失败（已重试 {tries} 次）: {last}")


def _rows_once(
    report_table: str,
    filt: str,
    page: int,
    *,
    http: Callable[..., Any] | None,
    page_size: int,
    sort: str = "END_DATE,HOLDER_MARKET_CAP",
    sort_types: str = "-1,-1",
) -> tuple[list[dict[str, Any]], int, bool]:
    params = {
        "reportName": report_table,
        "columns": "ALL",
        "pageSize": str(page_size),
        "pageNumber": str(page),
        "filter": filt,
        "sortColumns": sort,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    data = _http_json(params, http=http)
    if not data.get("success"):
        message = str(data.get("message") or "")
        if "返回数据为空" in message:
            return [], 0, True
        raise NationalTeamFetchError(f"{report_table} → {message or 'success=false'}")
    result = data.get("result") or {}
    return list(result.get("data") or []), int(result.get("pages") or 0), True


def _query_rows(
    report_table: str,
    holder_name: str,
    report_date: str | None = None,
    *,
    http: Callable[..., Any] | None = None,
    page_size: int = 500,
    max_pages: int = 40,
) -> list[dict[str, Any]]:
    """定向取单主体（保留给兼容/调试）；`report_date=None` 时按 END_DATE 倒序。"""
    filt = f'(HOLDER_NAME="{holder_name}")'
    if report_date:
        filt += f"(END_DATE='{report_date}')"
    rows: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        batch, pages, _ = _rows_once(report_table, filt, page, http=http, page_size=page_size)
        if not batch:
            break
        rows.extend(batch)
        if page >= max(pages, 1):
            break
        page += 1
        time.sleep(0.15)
    return rows


def _query_names_in(
    report_table: str,
    names: Sequence[str],
    report_date: str,
    *,
    http: Callable[..., Any] | None = None,
    chunk: int = 40,
    page_size: int = 500,
    max_pages: int = 40,
) -> list[dict[str, Any]]:
    """一次取一组主体（`in (...)`），按 chunk 分批避免 URL 过长。

    ⚠️ 源端语法限制（实测）：`in (...)` **不能**与 `(END_DATE='...')` 并列 ——
    并上去不会报错，而是把日期条件**静默忽略**（返回全部报告期）。所以这里只查
    `in`，按 `END_DATE DESC` 排序后翻页，遇到整页都早于目标期就停；日期仍由
    `normalize_rows` 逐行校验（不匹配的直接丢弃），不靠上游保证。
    """
    out: list[dict[str, Any]] = []
    uniq = [n for n in dict.fromkeys(names) if n]
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        # ⚠️ 逗号后**不能带空格**：源端 ANTLR 解析器报 InputMismatchException（实测）
        quoted = ",".join('"' + n.replace('"', "") + '"' for n in part)
        filt = f"(HOLDER_NAME in ({quoted}))"
        page = 1
        while page <= max_pages:
            batch, pages, _ = _rows_once(report_table, filt, page, http=http, page_size=page_size)
            if not batch:
                break
            out.extend(batch)
            dates = [(str(r.get("END_DATE") or "")[:10]) for r in batch]
            # 整页都早于目标期 ⇒ 后续页只会更早（排序为 END_DATE DESC），提前收工
            if dates and max(dates) < report_date:
                break
            if page >= max(pages, 1):
                break
            page += 1
            time.sleep(0.1)
        time.sleep(0.1)
    return out



def _fetch_all_pages(
    report_table: str,
    filt: str,
    *,
    http: Callable[..., Any] | None = None,
    page_size: int = 2000,
    max_pages: int = 80,
    workers: int = 4,
) -> list[dict[str, Any]]:
    """把筛选条件下的所有页拉全（第 1 页定总数，其余并行）。

    单页在上游要几秒（海外链路实测 5-20s），串行 29 页会拖到十几分钟；
    页与页之间互不依赖，4 路并发把全市场扫描压到可接受的时间（单页偶发 60-70s 抖动来自上游，不是并发导致）。
    """
    from concurrent.futures import ThreadPoolExecutor

    first, pages, _ = _rows_once(report_table, filt, 1, http=http, page_size=page_size)
    if not first:
        return []
    total = max(1, min(int(pages or 1), max_pages))
    rows = list(first)
    if total == 1:
        return rows

    def _got(n: int) -> list[dict[str, Any]]:
        batch, _p, _ok = _rows_once(report_table, filt, n, http=http, page_size=page_size)
        return batch

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for batch in pool.map(_got, range(2, total + 1)):
            rows.extend(batch)
    return rows


# ────────────────────────── 解析 ──────────────────────────


def _norm_date(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()[:10]
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    return s


def _norm_code(raw: Any) -> str | None:
    """只接受 ASCII 6 位数字（防全角/带后缀被「修复」成合法代码）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if len(s) != 6 or not s.isascii() or not s.isdigit():
        return None
    return s


def _num(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace(",", "")
    if not s or s in {"-", "--", "None", "null"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_rows(
    raw_rows: Iterable[Mapping[str, Any]],
    *,
    holder: Mapping[str, str] | None = None,
    report_kind: str,
    report_date: str,
    snapshot_at: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """把源行归一化成落库行。

    `holder` 为空时按名称规则自动归类（全市场扫描路径）；归类不出的一律丢弃。
    """
    out: list[dict[str, Any]] = []
    stats = {"raw": 0, "kept": 0, "dropped_code": 0, "dropped_value": 0, "dropped_date": 0,
             "dropped_unclassified": 0}
    seen: set[tuple[str, str]] = set()
    for row in raw_rows:
        stats["raw"] += 1
        if _norm_date(row.get("END_DATE")) != report_date:
            stats["dropped_date"] += 1
            continue
        code = _norm_code(row.get("SECURITY_CODE"))
        if code is None:
            stats["dropped_code"] += 1
            continue
        hold_num = _num(row.get("HOLD_NUM"))
        if hold_num is None or hold_num <= 0:
            stats["dropped_value"] += 1
            continue

        name = str(row.get("HOLDER_NAME") or "").strip()
        if holder is not None:
            group_key = holder["group_key"]
            label = holder["group"]
            key = holder["key"]
        else:
            group_key = classify_holder(name) or ""
            if not group_key:
                stats["dropped_unclassified"] += 1
                continue
            label = GROUPS[group_key]["label"]
            key = holder_key_for(name, group_key)
        if (key, code) in seen:
            continue
        seen.add((key, code))

        rank = _num(row.get("HOLDER_RANK"))
        out.append(
            {
                "report_date": report_date,
                "report_kind": report_kind,
                "holder_key": key,
                "stock_code": code,
                "holder_name": name or label,
                "holder_group": label,
                "group_key": group_key,
                "stock_name": (str(row.get("SECURITY_NAME_ABBR") or "").strip() or None),
                "hold_num": hold_num,
                "hold_ratio": _num(row.get("HOLD_NUM_RATIO")),
                "market_cap": _num(row.get("HOLDER_MARKET_CAP")),
                "holder_rank": int(rank) if rank is not None else None,
                "change_type": (
                    str(row.get("HOLD_NUM_CHANGE") or "").strip()
                    or str(row.get("HOLDER_STATE_NEW") or "").strip()
                    or None
                ),
                "shares_type": (str(row.get("SHARES_TYPE") or "").strip() or None),
                "snapshot_at": snapshot_at,
                "source": SOURCE,
            }
        )
        stats["kept"] += 1
    return out, stats


# ────────────────────────── 写入 ──────────────────────────


def _upsert(conn, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    sql = upsert_sql(TABLE, ALL_COLS, KEY_COLS, FIELDS)
    conn.execute(text(sql), [dict(r) for r in rows])
    return len(rows)


def _replace_period(conn, report_date: str, report_kind: str, rows: Sequence[Mapping[str, Any]]) -> int:
    """整期整表替换：全市场扫描是该期的完整真值，直接清掉旧行避免残留/重复键。"""
    conn.execute(
        text(f"DELETE FROM {TABLE} WHERE report_date = :rd AND report_kind = :rk"),
        {"rd": report_date, "rk": report_kind},
    )
    return _upsert(conn, rows)


def _delete_stale(conn, report_date: str, report_kind: str, holder_key: str, codes: Sequence[str]) -> int:
    if not codes:
        return 0
    placeholders = ", ".join(f":c{i}" for i in range(len(codes)))
    params: dict[str, Any] = {"rd": report_date, "rk": report_kind, "hk": holder_key}
    params.update({f"c{i}": c for i, c in enumerate(codes)})
    res = conn.execute(
        text(
            f"DELETE FROM {TABLE} WHERE report_date = :rd AND report_kind = :rk "
            f"AND holder_key = :hk AND stock_code NOT IN ({placeholders})"
        ),
        params,
    )
    return int(res.rowcount or 0)


def _upsert_registry(conn, rows: Sequence[Mapping[str, Any]], period: str) -> int:
    names: dict[str, dict[str, Any]] = {}
    for r in rows:
        n = r["holder_name"]
        ent = names.setdefault(
            n,
            {"holder_name": n, "group_key": r["group_key"], "holder_group": r["holder_group"],
             "first_seen": period, "last_seen": period, "rows_seen": 0},
        )
        ent["rows_seen"] += 1
        if period < ent["first_seen"]:
            ent["first_seen"] = period
        if period > ent["last_seen"]:
            ent["last_seen"] = period
    if not names:
        return 0
    sql = upsert_sql(
        REGISTRY_TABLE,
        ("holder_name", "group_key", "holder_group", "first_seen", "last_seen", "rows_seen"),
        ("holder_name",),
        ("group_key", "holder_group", "first_seen", "last_seen", "rows_seen"),
    )
    # 重复扫描同一期时 rows_seen 不能累加：用当前期行数覆盖式写入
    conn.execute(text(sql), list(names.values()))
    return len(names)


def discover_report_periods(
    *,
    http: Callable[..., Any] | None = None,
    limit: int = 4,
) -> list[str]:
    """按汇金投资的历史行发现最近 N 个报告期（倒序）。"""
    probe = HOLDER_BY_KEY["huijin_invest"]["name"]
    rows = _query_rows(REPORT_TABLES["float"], probe, None, http=http, page_size=500, max_pages=3)
    periods = sorted({d for d in (_norm_date(r.get("END_DATE")) for r in rows) if d}, reverse=True)
    if not periods:
        raise NationalTeamFetchError("未能从上游发现任何报告期（END_DATE 全空或全部解析失败）")
    return periods[:limit]


# ────────────────────────── 采集 ──────────────────────────


def scan_period(
    engine: Engine,
    report_date: str,
    *,
    report_kinds: Sequence[str] = ("float", "total"),
    http: Callable[..., Any] | None = None,
    page_size: int = 2000,
    max_pages: int = 80,
) -> dict[str, Any]:
    """全市场扫描某一报告期，按名称规则认出国家队/机构持仓并整期替换落库。

    这是拿到「社保各组合」「险资各产品账户」的唯一路径（名字事先不可枚举）。
    一期一表约 5.6 万行 / 29 页（pageSize=2000），两块表约 3-6 分钟。
    """
    snapshot_at = datetime.now(_CST).isoformat(timespec="seconds")
    receipt: dict[str, Any] = {
        "ok": True,
        "mode": "scan",
        "periods": [report_date],
        "snapshot_at": snapshot_at,
        "written": 0,
        "scanned_rows": {},
        "groups": {},
        "errors": [],
        "source": SOURCE,
        "caliber_note": CALIBER_NOTE,
    }
    for kind in report_kinds:
        report_table = REPORT_TABLES.get(kind)
        if report_table is None:
            raise ValueError(f"未知 report_kind: {kind}")
        try:
            logger.info("[national-team] scan 开始 %s/%s（全市场分页拉取）", report_date, kind)
            rows = _fetch_all_pages(
                report_table, f"(END_DATE='{report_date}')",
                http=http, page_size=page_size, max_pages=max_pages,
            )
            if not rows:
                receipt["scanned_rows"][f"{report_date}/{kind}"] = 0
                continue
            normalized, stats = normalize_rows(
                rows, holder=None, report_kind=kind, report_date=report_date, snapshot_at=snapshot_at
            )
            receipt["scanned_rows"][f"{report_date}/{kind}"] = stats["raw"]
            if not normalized:
                receipt["errors"].append({"target": f"{report_date}/{kind}", "error": f"全量 {stats['raw']} 行无可归类行"})
                receipt["ok"] = False
                continue
            with engine.begin() as conn:
                written = _replace_period(conn, report_date, kind, normalized)
                _upsert_registry(conn, normalized, report_date)
            receipt["written"] += written
            by_group: dict[str, int] = {}
            for r in normalized:
                by_group[r["group_key"]] = by_group.get(r["group_key"], 0) + 1
            receipt["groups"][kind] = by_group
            logger.info("[national-team] scan %s/%s 全量 %d 行 → 归类 %d 行 %s",
                        report_date, kind, stats["raw"], written, by_group)
        except Exception as exc:  # noqa: BLE001
            receipt["errors"].append({"target": f"{report_date}/{kind}", "error": str(exc)[:300]})
            receipt["ok"] = False
            logger.warning("[national-team] scan 失败 %s/%s: %s", report_date, kind, exc)
    return receipt


def registry_names(engine: Engine, group_key: str | None = None) -> list[str]:
    sql = f"SELECT holder_name FROM {REGISTRY_TABLE}"
    params: dict[str, Any] = {}
    if group_key:
        sql += " WHERE group_key = :g"
        params["g"] = group_key
    rows = _rows(engine, sql, params)
    return [str(r["holder_name"]) for r in rows]


def sync_targeted(
    engine: Engine,
    *,
    periods: Sequence[str],
    report_kinds: Sequence[str] = ("float", "total"),
    http: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """用注册表名单定向刷新（`in (...)`），秒级完成；注册表为空时退回种子名单。"""
    snapshot_at = datetime.now(_CST).isoformat(timespec="seconds")
    names = registry_names(engine)
    if not names:
        names = [h["name"] for h in HOLDERS]
    receipt: dict[str, Any] = {
        "ok": True,
        "mode": "targeted",
        "periods": list(periods),
        "snapshot_at": snapshot_at,
        "written": 0,
        "holder_names": len(names),
        "errors": [],
        "empty": [],
        "source": SOURCE,
        "caliber_note": CALIBER_NOTE,
    }
    for period in periods:
        for kind in report_kinds:
            report_table = REPORT_TABLES.get(kind)
            if report_table is None:
                raise ValueError(f"未知 report_kind: {kind}")
            try:
                raw = _query_names_in(report_table, names, period, http=http)
            except Exception as exc:  # noqa: BLE001
                receipt["errors"].append({"target": f"{period}/{kind}", "error": str(exc)[:300]})
                receipt["ok"] = False
                continue
            normalized, stats = normalize_rows(
                raw, holder=None, report_kind=kind, report_date=period, snapshot_at=snapshot_at
            )
            if not normalized:
                receipt["empty"].append(f"{period}/{kind}")
                continue
            # 定向刷新不是全量真值：按 (holder_key, stock) upsert + 清掉本批主体下已消失的行
            with engine.begin() as conn:
                written = _upsert(conn, normalized)
                by_key: dict[str, list[str]] = {}
                for r in normalized:
                    by_key.setdefault(r["holder_key"], []).append(r["stock_code"])
                for hk, codes in by_key.items():
                    _delete_stale(conn, period, kind, hk, codes)
                _upsert_registry(conn, normalized, period)
            receipt["written"] += written
            logger.info("[national-team] targeted %s/%s 写入 %d 行（源 %d 行）",
                        period, kind, written, stats["raw"])
    return receipt


def sync(
    engine: Engine,
    *,
    periods: Sequence[str] | None = None,
    report_kinds: Sequence[str] = ("float", "total"),
    mode: str = "scan",
    http: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """采集入口。`mode="scan"` 全市场扫描（季度/新报告期）；`mode="targeted"` 按注册表定向刷新。"""
    if periods is None:
        periods = discover_report_periods(http=http, limit=2)[:2]
    periods = [p for p in periods if p]
    if not periods:
        raise NationalTeamFetchError("periods 为空")
    if mode == "targeted":
        return sync_targeted(engine, periods=periods, report_kinds=report_kinds, http=http)

    snapshot_at = datetime.now(_CST).isoformat(timespec="seconds")
    agg: dict[str, Any] = {
        "ok": True, "mode": "scan", "periods": list(periods), "snapshot_at": snapshot_at,
        "written": 0, "scanned_rows": {}, "groups": {}, "errors": [],
        "source": SOURCE, "caliber_note": CALIBER_NOTE,
    }
    for period in periods:
        rc = scan_period(engine, period, report_kinds=report_kinds, http=http,
                         page_size=2000)
        agg["written"] += rc.get("written", 0)
        agg["scanned_rows"].update(rc.get("scanned_rows") or {})
        agg["groups"].update(rc.get("groups") or {})
        agg["errors"] += rc.get("errors") or []
        agg["ok"] = bool(agg["ok"] and rc.get("ok", True))
        agg["snapshot_at"] = rc.get("snapshot_at") or snapshot_at
    return agg


# ────────────────────────── 读取 ──────────────────────────


def _rows(engine: Engine, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        res = conn.execute(text(sql), dict(params or {}))
        return [dict(r._mapping) for r in res]


def persisted_periods(engine: Engine, kind: str = "float") -> list[str]:
    rows = _rows(
        engine,
        f"SELECT DISTINCT report_date FROM {TABLE} WHERE report_kind = :k ORDER BY report_date DESC",
        {"k": kind},
    )
    return [str(r["report_date"]) for r in rows]


def latest_period(engine: Engine, kind: str = "float") -> str | None:
    periods = persisted_periods(engine, kind)
    return periods[0] if periods else None


def overview(engine: Engine, report_date: str | None = None, kind: str = "float") -> dict[str, Any]:
    """按机构组汇总（组内多账户合并；同一股票在同组多账户下按只数去重、持股数相加）。"""
    periods = persisted_periods(engine, kind)
    target = report_date or (periods[0] if periods else None)
    if not target:
        return {
            "available": False,
            "report_date": None,
            "report_kind": kind,
            "periods": [],
            "groups": [],
            "absent_holders": [],
            "source": SOURCE,
            "caliber_note": CALIBER_NOTE,
            "reason": "尚未采集国家队持仓（表为空）",
        }

    rows = _rows(
        engine,
        f"""
SELECT group_key,
       COUNT(DISTINCT stock_code) AS stocks,
       COUNT(DISTINCT holder_name) AS holders,
       SUM(hold_num) AS hold_num,
       SUM(COALESCE(market_cap, 0)) AS market_cap,
       SUM(CASE WHEN market_cap IS NULL THEN 1 ELSE 0 END) AS market_cap_missing,
       MAX(snapshot_at) AS snapshot_at
FROM {TABLE}
WHERE report_date = :rd AND report_kind = :rk
GROUP BY group_key
""",
        {"rd": target, "rk": kind},
    )

    prev = periods[1] if len(periods) > 1 else None
    changed: dict[str, int] = {}
    if prev:
        crow = _rows(
            engine,
            f"""
SELECT c.group_key, COUNT(*) AS changed
FROM (
  SELECT group_key, stock_code, SUM(hold_num) AS num
  FROM {TABLE} WHERE report_date = :cur AND report_kind = :rk
  GROUP BY group_key, stock_code
) c
LEFT JOIN (
  SELECT group_key, stock_code, SUM(hold_num) AS num
  FROM {TABLE} WHERE report_date = :prev AND report_kind = :rk
  GROUP BY group_key, stock_code
) p ON p.group_key = c.group_key AND p.stock_code = c.stock_code
WHERE p.stock_code IS NULL OR p.num <> c.num
GROUP BY c.group_key
""",
            {"cur": target, "prev": prev, "rk": kind},
        )
        changed = {str(r["group_key"]): int(r["changed"] or 0) for r in crow}

    by_group = {str(r["group_key"]): r for r in rows}
    groups: list[dict[str, Any]] = []
    for gk in GROUP_ORDER:
        r = by_group.get(gk)
        if not r or not int(r["stocks"] or 0):
            continue
        groups.append(
            {
                "group_key": gk,
                "group_label": GROUPS[gk]["label"],
                "group_desc": GROUPS[gk]["desc"],
                "stocks": int(r["stocks"] or 0),
                "holders": int(r["holders"] or 0),
                "hold_num": float(r["hold_num"] or 0),
                "market_cap": float(r["market_cap"] or 0),
                "market_cap_missing": int(r["market_cap_missing"] or 0),
                "changed_stocks": changed.get(gk, 0),
            }
        )
    # 兜底：注册表之外出现的新组（未来扩展）也要显示，不能静默丢
    for gk, r in by_group.items():
        if gk in GROUP_ORDER or not int(r["stocks"] or 0):
            continue
        groups.append(
            {
                "group_key": gk,
                "group_label": GROUPS.get(gk, {}).get("label", gk),
                "group_desc": GROUPS.get(gk, {}).get("desc", ""),
                "stocks": int(r["stocks"] or 0),
                "holders": int(r["holders"] or 0),
                "hold_num": float(r["hold_num"] or 0),
                "market_cap": float(r["market_cap"] or 0),
                "market_cap_missing": int(r["market_cap_missing"] or 0),
                "changed_stocks": changed.get(gk, 0),
            }
        )

    # 合计口径：**只数按股票去重**（同一只票可能同时被汇金与社保持有，组内相加会重复计），
    # 市值/持股按持仓相加（不同账户持有的是不同股份，相加才是真实敞口）。
    overall = _rows(
        engine,
        f"SELECT COUNT(DISTINCT stock_code) AS n FROM {TABLE} WHERE report_date = :rd AND report_kind = :rk",
        {"rd": target, "rk": kind},
    )
    totals = {
        "stocks": int((overall[0]["n"] if overall else 0) or 0),
        "market_cap": sum(g["market_cap"] for g in groups),
        "hold_num": sum(g["hold_num"] for g in groups),
        "market_cap_missing": sum(g["market_cap_missing"] for g in groups),
        "group_stocks_summed": sum(g["stocks"] for g in groups),
    }
    present = {g["group_key"] for g in groups}
    absent = [
        {
            "group_key": gk,
            "group_label": GROUPS[gk]["label"],
            "group_desc": GROUPS[gk]["desc"],
            "note": "本期未出现在十大股东/十大流通股东（已退出或未进前十）",
        }
        for gk in GROUP_ORDER
        if gk not in present
    ]
    snapshot_at = max((str(r.get("snapshot_at") or "") for r in rows), default="") or None
    return {
        "available": True,
        "report_date": target,
        "report_kind": kind,
        "report_kind_label": REPORT_KIND_LABEL.get(kind, kind),
        "prev_report_date": prev,
        "periods": periods,
        "groups": groups,
        "absent_holders": absent,
        "totals": totals,
        "snapshot_at": snapshot_at,
        "source": SOURCE,
        "caliber_note": CALIBER_NOTE,
        "holder_scope_note": HOLDER_SCOPE_NOTE,
    }


_SORT_COLS = {
    "market_cap": "market_cap",
    "hold_num": "hold_num",
    "hold_ratio": "hold_ratio",
    "stock_code": "stock_code",
}


def holdings(
    engine: Engine,
    *,
    report_date: str | None = None,
    kind: str = "float",
    holder: str | None = None,
    q: str | None = None,
    sort: str = "market_cap",
    desc: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """持仓明细（`holder` 传组 key，如 insurance；或传具体 holder_key）。"""
    target = report_date or latest_period(engine, kind)
    if not target:
        return {
            "available": False,
            "report_date": None,
            "count": 0,
            "items": [],
            "reason": "尚未采集国家队持仓（表为空）",
            "caliber_note": CALIBER_NOTE,
        }

    where = ["report_date = :rd", "report_kind = :rk"]
    params: dict[str, Any] = {"rd": target, "rk": kind}
    if holder and holder != "all":
        if holder in GROUPS:
            where.append("group_key = :gk")
            params["gk"] = holder
        else:
            where.append("holder_key = :hk")
            params["hk"] = holder
    if q:
        where.append("(stock_code LIKE :q OR stock_name LIKE :q OR holder_name LIKE :q)")
        params["q"] = f"%{q.strip()}%"

    order_col = _SORT_COLS.get(sort, "market_cap")
    direction = "DESC" if desc else "ASC"
    lim = max(1, min(int(limit), 1000))
    off = max(0, int(offset))
    clause = " AND ".join(where)

    total = _rows(engine, f"SELECT COUNT(*) AS n FROM {TABLE} WHERE {clause}", params)
    items = _rows(
        engine,
        f"""
SELECT stock_code, stock_name, holder_key, holder_name, holder_group, group_key,
       hold_num, hold_ratio, market_cap, holder_rank, change_type, shares_type, snapshot_at
FROM {TABLE}
WHERE {clause}
ORDER BY {order_col} {direction}, stock_code ASC
LIMIT :lim OFFSET :off
""",
        {**params, "lim": lim, "off": off},
    )
    return {
        "available": bool(items),
        "report_date": target,
        "report_kind": kind,
        "count": int((total[0]["n"] if total else 0) or 0),
        "limit": lim,
        "offset": off,
        "items": items,
        "caliber_note": CALIBER_NOTE,
    }


def changes(
    engine: Engine,
    *,
    report_date: str | None = None,
    kind: str = "float",
    limit: int = 100,
) -> dict[str, Any]:
    """环比变动（按 机构组 + 股票 聚合；组内多账户先合并再比较）。"""
    periods = persisted_periods(engine, kind)
    target = report_date or (periods[0] if periods else None)
    if not target or len(periods) < 2:
        return {
            "available": False,
            "report_date": target,
            "prev_report_date": periods[1] if len(periods) > 1 else None,
            "counts": {},
            "items": [],
            "reason": "只有一期数据，无法计算环比（需至少两个报告期）",
        }
    prev = periods[1] if periods[0] == target else periods[0]

    rows = _rows(
        engine,
        f"""
SELECT c.group_key, c.stock_code, MAX(c.stock_name) AS stock_name,
       SUM(c.hold_num) AS cur_num, SUM(p.hold_num) AS prev_num,
       COUNT(DISTINCT c.holder_name) AS cur_holders,
       SUM(COALESCE(c.market_cap, 0)) AS cur_cap,
       SUM(COALESCE(p.market_cap, 0)) AS prev_cap
FROM {TABLE} c
LEFT JOIN {TABLE} p
  ON p.report_date = :prev AND p.report_kind = c.report_kind
 AND p.holder_key = c.holder_key AND p.stock_code = c.stock_code
WHERE c.report_date = :cur AND c.report_kind = :rk
GROUP BY c.group_key, c.stock_code
""",
        {"cur": target, "prev": prev, "rk": kind},
    )

    items: list[dict[str, Any]] = []
    counts = {"new": 0, "increase": 0, "decrease": 0, "exit": 0, "unchanged": 0}
    cur_pairs = {(str(r["group_key"]), str(r["stock_code"])) for r in rows}
    for r in rows:
        cur = float(r["cur_num"] or 0)
        prev_num = r.get("prev_num")
        if prev_num is None:
            kind_ = "new"
            delta = cur
        else:
            delta = cur - float(prev_num or 0)
            if abs(delta) < 1:
                kind_ = "unchanged"
            elif delta > 0:
                kind_ = "increase"
            else:
                kind_ = "decrease"
        counts[kind_] += 1
        if kind_ == "unchanged":
            continue
        gk = str(r["group_key"])
        items.append(
            {
                "kind": kind_,
                "group_key": gk,
                "holder_group": GROUPS.get(gk, {}).get("label", gk),
                "holder_name": GROUPS.get(gk, {}).get("label", gk),
                "stock_code": r["stock_code"],
                "stock_name": r["stock_name"],
                "cur_num": cur,
                "prev_num": float(prev_num) if prev_num is not None else None,
                "delta_num": delta,
                "cur_ratio": None,
                "prev_ratio": None,
                "cur_cap": r.get("cur_cap"),
                "prev_cap": r.get("prev_cap"),
                "change_type": None,
                "cur_holders": int(r.get("cur_holders") or 0),
            }
        )

    exits = _rows(
        engine,
        f"""
SELECT p.group_key, p.stock_code, MAX(p.stock_name) AS stock_name,
       SUM(p.hold_num) AS prev_num, SUM(COALESCE(p.market_cap, 0)) AS prev_cap
FROM {TABLE} p
WHERE p.report_date = :prev AND p.report_kind = :rk
GROUP BY p.group_key, p.stock_code
""",
        {"prev": prev, "rk": kind},
    )
    for r in exits:
        pair = (str(r["group_key"]), str(r["stock_code"]))
        if pair in cur_pairs:
            continue
        counts["exit"] += 1
        gk = pair[0]
        items.append(
            {
                "kind": "exit",
                "group_key": gk,
                "holder_group": GROUPS.get(gk, {}).get("label", gk),
                "holder_name": GROUPS.get(gk, {}).get("label", gk),
                "stock_code": r["stock_code"],
                "stock_name": r["stock_name"],
                "cur_num": 0.0,
                "prev_num": float(r["prev_num"] or 0),
                "delta_num": -float(r["prev_num"] or 0),
                "cur_ratio": None,
                "prev_ratio": None,
                "cur_cap": None,
                "prev_cap": r.get("prev_cap"),
                "change_type": "退出",
                "cur_holders": 0,
            }
        )

    order = {"new": 0, "increase": 1, "decrease": 2, "exit": 3}
    items.sort(key=lambda x: (order.get(x["kind"], 9), -abs(x["delta_num"])))
    lim = max(1, min(int(limit), 500))
    return {
        "available": True,
        "computed": True,
        "report_date": target,
        "prev_report_date": prev,
        "report_kind": kind,
        "counts": counts,
        "count": len(items),
        "items": items[:lim],
        "truncated": len(items) > lim,
        "caliber_note": CALIBER_NOTE,
    }
