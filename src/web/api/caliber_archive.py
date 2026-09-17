"""口径快照档案(B5, 2026-09-18)。

**为什么要有这个模块**：明盘 L2(同花顺/TQ)、暗盘逐笔(腾讯 v6)、东财四档对同一个"主力"给出
**天然不同**的数字。差异本身是资产 —— 留痕之后才能回答"哪家源在什么行情下偏离多少"，
页面上每个数字也才能点得开(可溯源: 哪个源 / 什么口径 / 哪个基准日 / 取值时间 / 有没有质量标记)。

**三条纪律**(与全仓诚实口径一致)：
1. 取不到就 `available=0` + `reason`，`value` 写 **NULL** —— 0 是真实数字，不能拿来冒充"没有"；
2. **不合成单一"权威数字"**，也不做跨源平均/校准；存的是"每源每字段各说各的"；
3. 跨源比较必须在返回里显式声明**比的是哪两个字段**，并说明这是"口径差异"而非"误差"。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

#: 每个源用于"跨源对比"的那个字段 —— **显式声明**, 不做自动猜测。
#: 三家对"主力"的定义不同(见 caliber_compare.SOURCE_META), 所以这里的差异是**口径差异**。
DRIFT_FIELD_BY_SOURCE: dict[str, str] = {
    "thsdk_l2": "主力净流入",
    "tencent_dark": "主力净额（≥20万）",
    "eastmoney_flow": "主力净流入",
}

#: 一次采集的标的数上限(防止把源打爆; 需要更多就调这个常量或分批)
MAX_SYMBOLS_PER_RUN = 40

#: 默认留痕标的(小样本起步, 可在 app_settings.caliber_archive_symbols 里覆盖)
DEFAULT_ARCHIVE_SYMBOLS: tuple[str, ...] = (
    "002361",  # 神剑股份(用户主力标的)
    "000001",  # 平安银行
    "600519",  # 贵州茅台
    "300750",  # 宁德时代
    "601318",  # 中国平安
)


def _cst_today() -> str:
    """交易日口径按 CST 取(与 dark_flow._cache_day 同源纪律; 不用跑测机器本地时区)。"""
    try:
        from src.core.dark_flow import _cache_day

        return _cache_day()
    except Exception:  # noqa: BLE001 —— 兜底: CST 当天
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def get_archive_symbols(engine: Engine | None = None) -> list[str]:
    """留痕标的: app_settings.caliber_archive_symbols(JSON 数组) > 默认小样本。"""
    if engine is None:
        return list(DEFAULT_ARCHIVE_SYMBOLS)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM app_settings WHERE key = 'caliber_archive_symbols'")
            ).fetchone()
        if row and row[0]:
            import json

            arr = json.loads(row[0])
            if isinstance(arr, list):
                codes = [str(x).strip() for x in arr if str(x).strip().isdigit()]
                if codes:
                    return codes[:MAX_SYMBOLS_PER_RUN]
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 caliber_archive_symbols 失败, 用默认样本: %r", exc)
    return list(DEFAULT_ARCHIVE_SYMBOLS)


def record_symbol(engine: Engine, symbol: str, trade_date: str | None = None) -> dict[str, Any]:
    """采集一个标的的三源口径并落库(幂等: 同标的+同交易日+同源+同字段 → UPDATE)。

    返回 {symbol, trade_date, rows, available_by_source} —— 只报**实际写入**的, 不报"应该有"的。
    """
    from src.web.api.caliber_compare import build_caliber_compare

    day = trade_date or _cst_today()
    snap = build_caliber_compare(symbol)
    rows: list[dict[str, Any]] = []
    avail: dict[str, int] = {}

    for src in snap.get("sources", []):
        key = str(src.get("key") or "")
        if not key:
            continue
        fields = src.get("fields") or []
        if not fields:
            # 源不可用: 也留痕(记下"这天这个源没数据"本身是信息), value=NULL, available=0
            rows.append(
                {
                    "source": key,
                    "field_key": DRIFT_FIELD_BY_SOURCE.get(key, ""),
                    "caliber": src.get("caliber") or "",
                    "unit": src.get("unit") or "",
                    "value": None,
                    "available": 0,
                    "reason": (src.get("note") or "无数据")[:400],
                    "quality": "",
                }
            )
            avail[key] = 0
            continue
        got = 0
        for f in fields:
            label = str(f.get("label") or "")
            val = f.get("value")
            is_primary = label == DRIFT_FIELD_BY_SOURCE.get(key)
            if val is None and not is_primary:
                continue  # 非主字段缺失就不留空行, 免得表里全是 NULL
            rows.append(
                {
                    "source": key,
                    "field_key": label,
                    "caliber": src.get("caliber") or "",
                    "unit": f.get("unit") or src.get("unit") or "",
                    "value": float(val) if isinstance(val, (int, float)) else None,
                    "available": 1 if isinstance(val, (int, float)) else 0,
                    "reason": "" if isinstance(val, (int, float)) else (src.get("note") or "")[:400],
                    "quality": "suspect" if "可疑" in (src.get("note") or "") else "",
                }
            )
            if isinstance(val, (int, float)):
                got += 1
        avail[key] = got

    with engine.begin() as conn:
        for r in rows:
            conn.execute(
                text(
                    """
INSERT INTO caliber_snapshots
  (symbol, trade_date, source, field_key, caliber, unit, value, available, reason, quality)
VALUES
  (:symbol, :trade_date, :source, :field_key, :caliber, :unit, :value, :available, :reason, :quality)
ON CONFLICT (symbol, trade_date, source, field_key) DO UPDATE SET
  value = excluded.value,
  available = excluded.available,
  reason = excluded.reason,
  quality = excluded.quality,
  unit = excluded.unit,
  caliber = excluded.caliber,
  captured_at = CURRENT_TIMESTAMP
"""
                ),
                {"symbol": symbol, **r, "trade_date": day},
            )

    return {"symbol": symbol, "trade_date": day, "rows": len(rows), "available_by_source": avail}


def drift_series(engine: Engine, symbol: str, days: int = 30) -> dict[str, Any]:
    """口径漂移: 每源每日的"主力"值 + 跨源差异(**口径差异, 不是误差**)。

    返回结构刻意不做"合成单一数字": 每源一条序列, 谁有谁没有如实标注;
    `comparisons` 里明确写出比的是**哪两个字段**。
    """
    days = max(2, min(int(days or 30), 120))
    since = (date.fromisoformat(_cst_today()) - timedelta(days=days * 2)).isoformat()

    wanted = list(DRIFT_FIELD_BY_SOURCE.items())
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
SELECT trade_date, source, field_key, value, available, reason, quality, unit
FROM caliber_snapshots
WHERE symbol = :symbol AND trade_date >= :since
  AND source IN :sources
  AND field_key IN :fields
ORDER BY trade_date ASC
"""
            ).bindparams(
                bindparam("sources", expanding=True),
                bindparam("fields", expanding=True),
            ),
            {
                "symbol": symbol,
                "since": since,
                "sources": tuple(k for k, _ in wanted),
                "fields": tuple(f for _, f in wanted),
            },
        ).fetchall()

    # 只保留"该源自己的那个字段", 免得把别的字段混进同一张图
    per_source: dict[str, dict[str, dict[str, Any]]] = {k: {} for k, _ in wanted}
    for r in rows:
        src = str(r[1])
        if DRIFT_FIELD_BY_SOURCE.get(src) != str(r[2]):
            continue
        per_source.setdefault(src, {})[str(r[0])] = {
            "value": None if r[3] is None else float(r[3]),
            "available": bool(r[4]),
            "reason": r[5] or "",
            "quality": r[6] or "",
            "unit": r[7] or "",
        }

    all_dates = sorted({d for s in per_source.values() for d in s})[-days:]
    series = []
    for d in all_dates:
        entry: dict[str, Any] = {"trade_date": d, "sources": {}}
        for src, _field in wanted:
            hit = per_source.get(src, {}).get(d)
            entry["sources"][src] = hit or {
                "value": None,
                "available": False,
                "reason": "该日未留痕",
                "quality": "",
                "unit": "",
            }
        series.append(entry)

    # 跨源差异: 只算"两端都 available"的日, 且**不产出单一权威值**
    comparisons: list[dict[str, Any]] = []
    keys = [k for k, _ in wanted]
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            pairs = [
                (e["sources"][a]["value"], e["sources"][b]["value"])
                for e in series
                if e["sources"][a]["available"] and e["sources"][b]["available"]
            ]
            diffs = [abs(x - y) for x, y in pairs]
            comparisons.append(
                {
                    "left": {"source": a, "field": DRIFT_FIELD_BY_SOURCE[a]},
                    "right": {"source": b, "field": DRIFT_FIELD_BY_SOURCE[b]},
                    "both_available_days": len(pairs),
                    "mean_abs_diff": (sum(diffs) / len(diffs)) if diffs else None,
                    "max_abs_diff": max(diffs) if diffs else None,
                    "note": "两端定义不同, 这是**口径差异**不是误差; 不取平均、不互相校准。",
                }
            )

    return {
        "symbol": symbol,
        "days": days,
        "field_by_source": dict(wanted),
        "series": series,
        "comparisons": comparisons,
        "archived_days": len(all_dates),
        "note": "逐日留痕来自定时采集(收盘后); 未留痕的日期如实标「该日未留痕」, 不插值、不补 0。",
    }
