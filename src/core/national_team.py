"""国家队（汇金 / 证金 / 外汇局三平台）季度持仓采集与读取（2026-11-18）。

## 数据来源（实测可用，非推测）

东方财富 `datacenter-web` 两张股东报表，支持按**股东名称精确等值**过滤：

| 报表 | 含义 | 备注 |
|---|---|---|
| `RPT_F10_EH_FREEHOLDERS` | 十大**流通**股东 | 覆盖更全（进不了十大股东但进了十大流通股东的也会出现） |
| `RPT_F10_EH_HOLDERS` | 十大股东 | 多一列 `HOLD_NUM_RATIO`（持股比例），银行类大股东口径 |

源端**不支持 `like` 查询**（`filter=(HOLDER_NAME like "%汇金%")` → `不支持like查询`），
所以每个主体必须用**全称**单独查。一个主体 + 一个报告期 = 一次分页请求。

## 口径与硬限制（页面必须显式标注，不得省略）

1. **只有进入前十大股东 / 十大流通股东的持仓可见**，第 11 名以后不可见（这是披露规则，不是采集缺陷）。
2. **没有基金持有人报表**：汇金真正的重仓是沪深300 等宽基 ETF，只在基金半年报/年报的
   「前十大持有人」里披露，本模块（以及东财任何 datacenter 报表）都拿不到 —— 本表是
   **个股直投**口径，不是汇金总资产。
3. **A+H 混行**：建行等 `SHARES_TYPE` 为 `A股,H股`，A/H 未拆分；`market_cap` 直接取源端
   `HOLDER_MARKET_CAP`，不自行乘价重算（避免口径漂移）。
4. **报告期口径，不是实时持仓**：`report_date` 来自源端 `END_DATE`，季报/年报披露节奏。

## 不编造

- 上游失败（网络/配置/解析）→ 抛 `NationalTeamFetchError`，**不写 0 行**；
- 上游明确「返回数据为空」→ 该主体该期记为 0 只（合法状态，会计入 receipt 的 `empty` 列表）；
- 单行缺 `hold_num` / 代码非法 → 丢弃并计数，全部丢弃则整批失败（不是「没持仓」）。
"""
from __future__ import annotations

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

#: 两张报表 → 落库的 report_kind
REPORT_TABLES: dict[str, str] = {
    "float": "RPT_F10_EH_FREEHOLDERS",  # 十大流通股东
    "total": "RPT_F10_EH_HOLDERS",  # 十大股东（含持股比例）
}
REPORT_KIND_LABEL: dict[str, str] = {"float": "十大流通股东", "total": "十大股东"}

#: 国家队主体注册表。`name` 必须是源端 `HOLDER_NAME` 的**完整等值**（含「有限责任公司」后缀），
#: 源端不支持模糊匹配，少一个字就查不到。
HOLDERS: tuple[dict[str, str], ...] = (
    {
        "key": "huijin_invest",
        "name": "中央汇金投资有限责任公司",
        "group": "汇金·投资",
        "group_key": "huijin",
    },
    {
        "key": "huijin_asset",
        "name": "中央汇金资产管理有限责任公司",
        "group": "汇金·资管",
        "group_key": "huijin",
    },
    {
        "key": "csf",
        "name": "中国证券金融股份有限公司",
        "group": "证金公司",
        "group_key": "csf",
    },
    {
        "key": "wutongshu",
        "name": "梧桐树投资平台有限责任公司",
        "group": "外汇局·梧桐树",
        "group_key": "safe",
    },
    {
        "key": "fengshan",
        "name": "北京凤山投资有限责任公司",
        "group": "外汇局·凤山",
        "group_key": "safe",
    },
    {
        "key": "kunteng",
        "name": "北京坤藤投资有限责任公司",
        "group": "外汇局·坤藤",
        "group_key": "safe",
    },
)

HOLDER_BY_KEY: dict[str, dict[str, str]] = {h["key"]: h for h in HOLDERS}
HOLDER_BY_NAME: dict[str, dict[str, str]] = {h["name"]: h for h in HOLDERS}

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

#: 页面必须显示的口径声明（前端直接展示，不另写一份）
CALIBER_NOTE = (
    "口径：东方财富十大股东/十大流通股东（季报披露），只含进入前十大的持仓；"
    "不含宽基 ETF 持仓（基金半年报/年报「前十大持有人」另计）；A+H 行未拆分。"
)

#: 主体级说明（社保等按组合分散披露，未纳入本表）
HOLDER_SCOPE_NOTE = (
    "覆盖主体：中央汇金投资、中央汇金资管、证金公司、外汇局三平台（梧桐树/凤山/坤藤）。"
    "社保基金按「XX组合」分散披露，未纳入本期。"
)


class NationalTeamFetchError(RuntimeError):
    """上游取数失败（网络/配置/解析）。绝不降级成「无持仓」。"""


# ────────────────────────── HTTP ──────────────────────────


def _http_json(
    params: Mapping[str, str],
    *,
    http: Callable[..., Any] | None = None,
    tries: int = 4,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """请求 datacenter-web 并解析 JSON，带重试。

    实测坑：`pageSize=500` 偶发 `http.client.IncompleteRead`（chunked 断流），
    必须重试；源端故障时返回 `success:false` + `message`，由调用方区分
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


def _query_rows(
    report_table: str,
    holder_name: str,
    report_date: str | None = None,
    *,
    http: Callable[..., Any] | None = None,
    page_size: int = 500,
    max_pages: int = 40,
) -> list[dict[str, Any]]:
    """分页取某主体某报告期的全部股东行；`report_date=None` 时按 END_DATE 倒序（用于发现报告期）。"""
    filt = f'(HOLDER_NAME="{holder_name}")'
    if report_date:
        filt += f"(END_DATE='{report_date}')"
    rows: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        params = {
            "reportName": report_table,
            "columns": "ALL",
            "pageSize": str(page_size),
            "pageNumber": str(page),
            "filter": filt,
            "sortColumns": "END_DATE,HOLDER_MARKET_CAP",
            "sortTypes": "-1,-1",
            "source": "WEB",
            "client": "WEB",
        }
        data = _http_json(params, http=http)
        if not data.get("success"):
            message = str(data.get("message") or "")
            if "返回数据为空" in message:
                return rows
            raise NationalTeamFetchError(
                f"{report_table} / {holder_name} / {report_date or 'latest'} → {message or 'success=false'}"
            )
        result = data.get("result") or {}
        batch = result.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        pages = int(result.get("pages") or 0)
        if page >= pages:
            break
        page += 1
        time.sleep(0.15)
    return rows


# ────────────────────────── 解析 ──────────────────────────


def _norm_date(raw: Any) -> str | None:
    """源端 END_DATE 形如 `2026-09-30 00:00:00` → `2026-09-30`。"""
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
    holder: Mapping[str, str],
    report_kind: str,
    report_date: str,
    snapshot_at: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """把源行归一化成落库行；非法行丢弃并计数（全部丢弃由调用方判失败）。"""
    out: list[dict[str, Any]] = []
    stats = {"raw": 0, "kept": 0, "dropped_code": 0, "dropped_value": 0, "dropped_date": 0}
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
        rank = _num(row.get("HOLDER_RANK"))
        out.append(
            {
                "report_date": report_date,
                "report_kind": report_kind,
                "holder_key": holder["key"],
                "stock_code": code,
                "holder_name": holder["name"],
                "holder_group": holder["group"],
                "group_key": holder["group_key"],
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


def _delete_stale(conn, report_date: str, report_kind: str, holder_key: str, codes: Sequence[str]) -> int:
    """同一(报告期, 口径, 主体)重采后，源端已不存在的股票要删掉，避免旧行残留。"""
    if not codes:
        return 0
    placeholders = ", ".join(f":c{i}" for i in range(len(codes)))
    params: dict[str, Any] = {
        "rd": report_date,
        "rk": report_kind,
        "hk": holder_key,
    }
    params.update({f"c{i}": c for i, c in enumerate(codes)})
    res = conn.execute(
        text(
            f"DELETE FROM {TABLE} WHERE report_date = :rd AND report_kind = :rk "
            f"AND holder_key = :hk AND stock_code NOT IN ({placeholders})"
        ),
        params,
    )
    return int(res.rowcount or 0)


def discover_report_periods(
    *,
    http: Callable[..., Any] | None = None,
    limit: int = 4,
) -> list[str]:
    """按汇金投资的「十大流通股东」历史行发现最近 N 个报告期（倒序）。

    用汇金投资当探针：它是每期必然在榜的主体（四大行），比查「全市场最新 END_DATE」便宜。
    """
    probe = HOLDER_BY_KEY["huijin_invest"]["name"]
    rows = _query_rows(REPORT_TABLES["float"], probe, None, http=http, page_size=500, max_pages=3)
    periods = sorted({d for d in (_norm_date(r.get("END_DATE")) for r in rows) if d}, reverse=True)
    if not periods:
        raise NationalTeamFetchError("未能从上游发现任何报告期（END_DATE 全空或全部解析失败）")
    return periods[:limit]


def sync(
    engine: Engine,
    *,
    periods: Sequence[str] | None = None,
    report_kinds: Sequence[str] = ("float", "total"),
    holders: Sequence[Mapping[str, str]] | None = None,
    http: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """采集最近 2 个报告期（默认）× 两套口径 × 全部主体，幂等 upsert 到 `national_team_holdings`。

    返回 receipt：`{ok, periods, written, deleted_stale, per_holder, empty, errors, snapshot_at}`。
    任一主体全部失败 → `ok=False` 并保留 errors；部分失败仍写入成功部分（页面按 receipt 标注）。
    """
    snapshot_at = datetime.now(_CST).isoformat(timespec="seconds")
    if periods is None:
        periods = discover_report_periods(http=http, limit=2)[:2]
    periods = [p for p in periods if p]
    if not periods:
        raise NationalTeamFetchError("periods 为空")

    holder_list = list(holders or HOLDERS)
    receipt: dict[str, Any] = {
        "ok": True,
        "periods": list(periods),
        "report_kinds": list(report_kinds),
        "snapshot_at": snapshot_at,
        "written": 0,
        "deleted_stale": 0,
        "per_holder": {},
        "empty": [],
        "errors": [],
        "dropped": {},
        "source": SOURCE,
        "caliber_note": CALIBER_NOTE,
    }

    for period in periods:
        for kind in report_kinds:
            report_table = REPORT_TABLES.get(kind)
            if report_table is None:
                raise ValueError(f"未知 report_kind: {kind}")
            for holder in holder_list:
                label = f"{period}/{kind}/{holder['key']}"
                try:
                    raw = _query_rows(report_table, holder["name"], period, http=http)
                except Exception as exc:  # noqa: BLE001 — 单主体失败不拖垮整批
                    receipt["errors"].append({"target": label, "error": str(exc)[:300]})
                    receipt["ok"] = False
                    logger.warning("[national-team] 取数失败 %s: %s", label, exc)
                    continue

                if not raw:
                    receipt["empty"].append(label)
                    receipt["per_holder"][label] = {"stocks": 0}
                    # 上游明确为空 ⇒ 该主体该期确实不在榜：清掉旧行，避免残留
                    with engine.begin() as conn:
                        receipt["deleted_stale"] += _delete_stale(
                            conn, period, kind, holder["key"], ["__none__"]
                        )
                    continue

                rows, stats = normalize_rows(
                    raw,
                    holder=holder,
                    report_kind=kind,
                    report_date=period,
                    snapshot_at=snapshot_at,
                )
                receipt["dropped"][label] = stats
                if not rows:
                    receipt["errors"].append(
                        {"target": label, "error": f"源返回 {stats['raw']} 行但全部非法（{stats}）"}
                    )
                    receipt["ok"] = False
                    continue

                with engine.begin() as conn:
                    written = _upsert(conn, rows)
                    stale = _delete_stale(conn, period, kind, holder["key"], [r["stock_code"] for r in rows])
                receipt["written"] += written
                receipt["deleted_stale"] += stale
                receipt["per_holder"][label] = {
                    "stocks": len(rows),
                    "rows": stats["raw"],
                    "dropped": {
                        "code": stats["dropped_code"],
                        "value": stats["dropped_value"],
                        "date": stats["dropped_date"],
                    },
                }
                logger.info("[national-team] %s 写入 %d 行（源 %d 行）", label, written, stats["raw"])

    return receipt


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
    """主体维度汇总 + 报告期列表 + 采集时点。无数据 → available=false（不编造）。"""
    periods = persisted_periods(engine, kind)
    target = report_date or (periods[0] if periods else None)
    if not target:
        return {
            "available": False,
            "report_date": None,
            "report_kind": kind,
            "periods": [],
            "groups": [],
            "source": SOURCE,
            "caliber_note": CALIBER_NOTE,
            "reason": "尚未采集国家队持仓（表为空）",
        }

    rows = _rows(
        engine,
        f"""
SELECT holder_key, holder_name, holder_group, group_key,
       COUNT(*) AS stocks,
       SUM(hold_num) AS hold_num,
       SUM(COALESCE(market_cap, 0)) AS market_cap,
       SUM(CASE WHEN market_cap IS NULL THEN 1 ELSE 0 END) AS market_cap_missing,
       MAX(snapshot_at) AS snapshot_at
FROM {TABLE}
WHERE report_date = :rd AND report_kind = :rk
GROUP BY holder_key, holder_name, holder_group, group_key
ORDER BY market_cap DESC
""",
        {"rd": target, "rk": kind},
    )

    # 环比：与库中上一期比较（按主体统计变动只数）
    prev = periods[1] if len(periods) > 1 else None
    changed: dict[str, int] = {}
    if prev:
        crow = _rows(
            engine,
            f"""
SELECT c.holder_key, COUNT(*) AS changed
FROM {TABLE} c
LEFT JOIN {TABLE} p
  ON p.report_date = :prev AND p.report_kind = c.report_kind
 AND p.holder_key = c.holder_key AND p.stock_code = c.stock_code
WHERE c.report_date = :cur AND c.report_kind = :rk
  AND (p.stock_code IS NULL OR p.hold_num <> c.hold_num)
GROUP BY c.holder_key
""",
            {"cur": target, "prev": prev, "rk": kind},
        )
        changed = {str(r["holder_key"]): int(r["changed"] or 0) for r in crow}

    groups = []
    for r in rows:
        key = str(r["holder_key"])
        groups.append(
            {
                "holder_key": key,
                "holder_name": r["holder_name"],
                "holder_group": r["holder_group"],
                "group_key": r["group_key"],
                "stocks": int(r["stocks"] or 0),
                "hold_num": float(r["hold_num"] or 0),
                "market_cap": float(r["market_cap"] or 0),
                "market_cap_missing": int(r["market_cap_missing"] or 0),
                "changed_stocks": changed.get(key, 0),
            }
        )

    totals = {
        "stocks": sum(g["stocks"] for g in groups),
        "market_cap": sum(g["market_cap"] for g in groups),
        "hold_num": sum(g["hold_num"] for g in groups),
        "market_cap_missing": sum(g["market_cap_missing"] for g in groups),
    }
    present = {g["holder_key"] for g in groups}
    # 注册在案但本期无任一行的主体: 这是「本期未在榜/已退出」的事实, 不是采集失败 ——
    # 必须在汇总里显式列出, 否则用户会以为漏采集(外汇局三平台 2020 年后确实已退出)。
    absent = [
        {
            "holder_key": h["key"],
            "holder_name": h["name"],
            "holder_group": h["group"],
            "group_key": h["group_key"],
            "note": "本期未出现在十大股东/十大流通股东（已退出或未进前十）",
        }
        for h in HOLDERS
        if h["key"] not in present
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
    "change": "hold_num",
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
    """持仓明细（可按主体/代码或名称关键字过滤）。"""
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
        where.append("holder_key = :hk")
        params["hk"] = holder
    if q:
        where.append("(stock_code LIKE :q OR stock_name LIKE :q)")
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
    """环比变动：新进 / 加仓 / 减仓 / 退出（以库中上一报告期为基准）。"""
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
SELECT c.holder_key, c.holder_name, c.holder_group, c.stock_code, c.stock_name,
       c.hold_num AS cur_num, p.hold_num AS prev_num,
       c.hold_ratio AS cur_ratio, p.hold_ratio AS prev_ratio,
       c.market_cap AS cur_cap, p.market_cap AS prev_cap,
       c.change_type
FROM {TABLE} c
LEFT JOIN {TABLE} p
  ON p.report_date = :prev AND p.report_kind = c.report_kind
 AND p.holder_key = c.holder_key AND p.stock_code = c.stock_code
WHERE c.report_date = :cur AND c.report_kind = :rk
""",
        {"cur": target, "prev": prev, "rk": kind},
    )
    exits = _rows(
        engine,
        f"""
SELECT p.holder_key, p.holder_name, p.holder_group, p.stock_code, p.stock_name,
       p.hold_num AS prev_num, p.hold_ratio AS prev_ratio, p.market_cap AS prev_cap
FROM {TABLE} p
LEFT JOIN {TABLE} c
  ON c.report_date = :cur AND c.report_kind = p.report_kind
 AND c.holder_key = p.holder_key AND c.stock_code = p.stock_code
WHERE p.report_date = :prev AND p.report_kind = :rk AND c.stock_code IS NULL
""",
        {"cur": target, "prev": prev, "rk": kind},
    )

    items: list[dict[str, Any]] = []
    counts = {"new": 0, "increase": 0, "decrease": 0, "exit": 0, "unchanged": 0}
    for r in rows:
        cur = float(r["cur_num"] or 0)
        prev_num = r.get("prev_num")
        if prev_num is None:
            kind_ = "new"
            delta = cur
        else:
            prev_f = float(prev_num or 0)
            delta = cur - prev_f
            if abs(delta) < 1:
                kind_ = "unchanged"
            elif delta > 0:
                kind_ = "increase"
            else:
                kind_ = "decrease"
        counts[kind_] += 1
        if kind_ == "unchanged":
            continue
        items.append(
            {
                "kind": kind_,
                "holder_key": r["holder_key"],
                "holder_name": r["holder_name"],
                "holder_group": r["holder_group"],
                "stock_code": r["stock_code"],
                "stock_name": r["stock_name"],
                "cur_num": cur,
                "prev_num": float(prev_num) if prev_num is not None else None,
                "delta_num": delta,
                "cur_ratio": r.get("cur_ratio"),
                "prev_ratio": r.get("prev_ratio"),
                "cur_cap": r.get("cur_cap"),
                "prev_cap": r.get("prev_cap"),
                "change_type": r.get("change_type"),
            }
        )
    for r in exits:
        counts["exit"] += 1
        items.append(
            {
                "kind": "exit",
                "holder_key": r["holder_key"],
                "holder_name": r["holder_name"],
                "holder_group": r["holder_group"],
                "stock_code": r["stock_code"],
                "stock_name": r["stock_name"],
                "cur_num": 0.0,
                "prev_num": float(r["prev_num"] or 0),
                "delta_num": -float(r["prev_num"] or 0),
                "cur_ratio": None,
                "prev_ratio": r.get("prev_ratio"),
                "cur_cap": None,
                "prev_cap": r.get("prev_cap"),
                "change_type": "退出",
            }
        )

    order = {"new": 0, "increase": 1, "decrease": 2, "exit": 3}
    items.sort(key=lambda x: (order.get(x["kind"], 9), -abs(x["delta_num"])))
    lim = max(1, min(int(limit), 500))
    # available=True 表示**已算出环比**（即使零变动）；零变动由 counts/items 空表达，
    # 不能被前端误读成「没有可比报告期」（这两件事必须能区分）。
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
