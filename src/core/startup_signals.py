"""启动早期信号 —— 读 `tq_formula_signal_daily`（客户端条件选股全市场扫描结果）。

口径说明（重要）：
    这里给的是**市场宽度视角**的客观计数 —— "今天全市场有多少只票满足某个技术条件"，
    以及相对近 N 日基线的倍数。它**不是**选股推荐，也不含方向判断；候选池的意义在于
    "哪些票刚出现放量/突破/见底形态"这一事实，是否参与由人决定。

数据源: 通达信客户端 108 个条件选股公式（`formula_all(1)`），盘后由
`tq_formula_signal_scheduler` 扫全市场落库（每公式约 8s）。命中清单存 `hits_json`
（上限 500 只，超出置 truncated=1，此时**不得**说成"全市场只有这些"）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: 换行常量（渲染用）
NL = "\n"

#: 与 tq_formula_signals.STARTUP_FORMULAS 的 acCode 保持一致
STARTUP_CODES: tuple[str, ...] = (
    "C116", "C117", "C112", "C113", "C118", "C131", "C120", "C123",
    "RED4", "C107", "C134", "C135", "MSTAR", "SUNBY",
)

DEFAULT_BASELINE_DAYS = 20
HITS_SHOW_LIMIT = 12


def _q(db, sql: str, params: dict) -> list:
    try:
        return list(db.execute(text(sql), params).fetchall())
    except Exception as exc:  # noqa: BLE001 — 表未建/迁移未跑
        logger.warning("startup_signals: 查询失败 %s", exc)
        return []


def read_startup_signals(db, *, days: int = DEFAULT_BASELINE_DAYS, names: dict | None = None) -> dict[str, Any]:
    """最新交易日的启动信号 + 近 N 日基线。数据缺失如实返回 ok=False。"""
    codes = list(STARTUP_CODES)
    ph = ", ".join(f":c{i}" for i in range(len(codes)))
    head = _q(db, f"SELECT MAX(trade_date) AS d FROM tq_formula_signal_daily WHERE formula_code IN ({ph})",
              {f"c{i}": c for i, c in enumerate(codes)})
    latest_date = (head[0][0] if head else None) or ""
    if not latest_date:
        return {"ok": False, "reason": "no_data", "hint": "尚未扫描落库(见 tq_formula_signal_scheduler)"}

    params = {f"c{i}": c for i, c in enumerate(codes)}
    params["d"] = latest_date
    rows = _q(db, f"""
SELECT formula_code, formula_name, hit_count, scanned_count, complete, truncated, hits_json
  FROM tq_formula_signal_daily
 WHERE trade_date = :d AND formula_code IN ({ph})
""", params)

    params2 = dict(params)
    params2["lim"] = int(days)
    base_rows = _q(db, f"""
SELECT formula_code, AVG(hit_count) AS avg_hits, COUNT(*) AS n
  FROM (SELECT formula_code, hit_count, trade_date,
               ROW_NUMBER() OVER (PARTITION BY formula_code ORDER BY trade_date DESC) AS rn
          FROM tq_formula_signal_daily
         WHERE formula_code IN ({ph}) AND trade_date < :d) z
 WHERE rn <= :lim
 GROUP BY formula_code
""", params2)
    baseline = {r[0]: (float(r[1]) if r[1] is not None else None, int(r[2] or 0)) for r in base_rows}

    items: list[dict[str, Any]] = []
    for r in rows:
        code, name, hits, scanned, complete, truncated, hits_json = (
            r[0], r[1], int(r[2] or 0), int(r[3] or 0), int(r[4] or 0), int(r[5] or 0), r[6] or "[]",
        )
        try:
            syms = json.loads(hits_json) if isinstance(hits_json, str) else []
        except Exception:  # noqa: BLE001
            syms = []
        avg = baseline.get(code, (None, 0))[0]
        items.append({
            "code": code,
            "name": names.get(code, name) if names else name,
            "hits": hits,
            "scanned": scanned,
            "complete": bool(complete),
            "truncated": bool(truncated),
            "baseline_avg": avg,
            "ratio": round(hits / avg, 2) if (avg and avg > 0) else None,
            "symbols": syms[:HITS_SHOW_LIMIT],
            "symbols_total": len(syms),
        })
    items.sort(key=lambda x: (-(x["ratio"] or 0), -x["hits"]))
    return {
        "ok": True,
        "trade_date": latest_date,
        "baseline_days": int(days),
        "items": items,
        "caliber": "客户端条件选股全市场扫描(通达信 TQ); 计数=只; 基线=近 N 交易日均值",
    }


def render_text(payload: dict[str, Any], *, top: int = 8) -> str:
    """chat 出口文本。取不到显示『—』；truncated 必须显式提示。"""
    if not payload or not payload.get("ok"):
        return ""
    items = payload.get("items") or []
    if not items:
        return ""
    lines = [f"🚀 启动早期信号（{payload.get('trade_date')}，全市场条件扫描，基线近 {payload.get('baseline_days')} 日）"]

    def one(it):
        b = it.get("baseline_avg")
        base = f"{b:.1f}" if isinstance(b, (int, float)) else "—"
        r = it.get("ratio")
        mult = f"×{r:g}" if isinstance(r, (int, float)) and r else ""
        warn = " ⚠️扫描不完整" if not it.get("complete") else ""
        return f"· {it['name']}：{it['hits']} 只（基线 {base} {mult}）{warn}"

    lines.append("【倍数最高】" + NL + NL.join(one(i) for i in items[:top]))
    hot = [i for i in items if i.get("symbols")][:2]
    for it in hot:
        syms = "、".join(it["symbols"])
        more = f" 等{it['symbols_total']}只" if it["symbols_total"] > len(it["symbols"]) else ""
        trunc = "（命中过多，仅存前 500 只）" if it.get("truncated") else ""
        lines.append(f"【{it['name']} 命中】{syms}{more}{trunc}")
    flags = [i for i in items if i.get("truncated") or not i.get("complete")]
    if flags:
        parts = []
        for i in flags:
            tags = []
            if not i.get("complete"):
                tags.append("扫描不完整")
            if i.get("truncated"):
                tags.append("命中超 500 只，仅存前 500 只")
            parts.append(f"{i['name']}（{'，'.join(tags)}）")
        lines.append("⚠️ 需注意：" + "；".join(parts))
    lines.append("说明：这是**客观条件满足的家数**及其相对基线的倍数，不是推荐；"
                 "『—』= 该公式暂无基线（样本不足），**不是 0**。")
    lines.append("口径：" + str(payload.get("caliber", "")))
    return NL.join(lines)
