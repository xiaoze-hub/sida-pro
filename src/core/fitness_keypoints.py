"""关键位兑现（F3）—— A 股版的「延迟到账真值」（2026-10-23，受控 RSI 证据层 S1）。

**为什么需要这一层**：通用 RSI 靠单测/编译拿 0-1 真值，A 股没有。今天涨不代表
判断对，退潮期的经验在主升期会把弱转强全部误杀。唯一与「模型说得像不像」无关的
客观量，是它自己写下的关键位（阻力/支撑）在 T+1..T+N 有没有被市场触及、误差多少。

**这一层只做三件事**（多一件都不做）：
1. 从 `copilot_steps.result_digest` 里把 `阻力 X` / `支撑 Y` 抠出来（文本来自
   `core/scenario_engine.py::render`，注意 digest 只存前 500 字，故必须容忍截断）；
2. 用 `load_qfq_bars_cached` 取该轮之后的日 K，判「有没有触及 / 先触哪边 / 误差 bp」；
3. 按配方聚合命中率 —— **样本不够就拒绝出率**（4 个账号、日均几十轮，n 才是结论）。

**边界（不许越）**：
- 不评「这条建议好不好」。那需要用户意图真值（当时想干什么、有没有照做），
  我们只有问题文本和关键位，硬评就是编。
- 缺关键位、缺 forward 行情、缺 symbol 一律 `available=False` + 原因，不猜、不填零。
- 只读：本模块不写库、不改配置、不发网络请求。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from src.db.klines_repo import load_qfq_bars_cached

logger = logging.getLogger(__name__)

# 情景矩阵工具名（唯一真值出口，`scenario_engine.render` 的文本就是它写进 digest 的）
SCENARIO_TOOL = "get_scenario_matrix"

# 「阻力 35.80」「支撑：32.8」「支撑32.8」都要认。
# 只认「标签 + 数字」，不认裸数字 —— 裸数字在矩阵文本里遍地都是（均线、量比、分位），
# 猜一个当关键位比不评更糟。
_LEVEL_RE = re.compile(r"(阻力|支撑)\s*[:：]?\s*(-?\d+(?:\.\d+)?)")

# 「触及后方向」的中性带：|Δ| < 20bp 视为 flat（一天 0.2% 以内的收盘差无方向含义）
FLAT_BAND_BP = 20.0

# 一次评分最多回看多少日历日（T+3 只需 ~7 个自然日，留余量给长停牌）
_MIN_LOOKBACK_DAYS = 30


def _norm(text: str) -> str:
    """全角→半角（：／（）／数字），避免「阻力：３５．８」被漏掉。"""
    return unicodedata.normalize("NFKC", str(text or ""))


def extract_levels(text: str) -> dict[str, float | None] | None:
    """从情景矩阵文本里抠 {resist, support}；一个都没有 → None。

    容忍：全角/半角标点、冒号带不带、数字后跟括号注释（`35.80（MA20）`）、
    文本被 digest 截断（只剩一半也把读到的那一半交出去）。
    只有一个时另一个给 None —— 部分信息照实返回，由调用方决定能不能评分。
    """
    if not text:
        return None
    found = _LEVEL_RE.findall(_norm(text))
    if not found:
        return None
    out: dict[str, float | None] = {"resist": None, "support": None}
    for label, num in found:
        key = "resist" if label == "阻力" else "support"
        if out[key] is None:          # 同一份文本里重复出现时取第一次（矩阵只写一次）
            try:
                out[key] = float(num)
            except (TypeError, ValueError):  # pragma: no cover - 正则已保证可转
                continue
    if out["resist"] is None and out["support"] is None:
        return None
    return out


def _parse_symbol(args_text: str | None) -> str | None:
    """从 step.args 里取 symbol。args 被截断成非法 JSON 时退回正则。"""
    if not args_text:
        return None
    try:
        raw = json.loads(args_text)
        sym = str((raw or {}).get("symbol") or "").strip().upper()
        return sym or None
    except Exception:  # noqa: BLE001 - 截断的 JSON 是常态（args 限 2KB）
        m = re.search(r'"symbol"\s*:\s*"([^"]+)"', str(args_text))
        return (m.group(1).strip().upper() or None) if m else None


def _unavailable(reason: str, **extra: Any) -> dict:
    out = {"available": False, "reason": reason}
    out.update(extra)
    return out


def _first_touch(win: list[dict], resist: float, support: float) -> tuple[str, int | None]:
    """先触哪边。同一根 K 线双触时无法知道先后 → 以更接近开盘价的一端为准（并注明）。"""
    for i, b in enumerate(win):
        hit_r = float(b["high"]) >= resist
        hit_s = float(b["low"]) <= support
        if hit_r and hit_s:
            return ("resist" if abs(resist - float(b["open"])) <= abs(float(b["open"]) - support)
                    else "support"), i
        if hit_r:
            return "resist", i
        if hit_s:
            return "support", i
    return "neither", None


def score_run(
    db,
    run_id: str,
    *,
    horizon_td: int = 3,
    market: str = "CN",
) -> dict:
    """把某轮参谋写下的关键位，和该轮之后的日 K 对账。

    返回（字段固定，不多不少）：
      available / run_id / symbol / as_of / levels / horizon_td /
      hit_resist / hit_support / first_touch / error_bp / direction_after_touch / soul

    `available=False` 的三种常态（都不是错误）：
      - 该轮没有情景矩阵读数（配方没调 `get_scenario_matrix`）；
      - 矩阵文本里没有可解析的关键位，或没有 symbol；
      - **行情尚未走完**（T+N 还没到）—— 这是最常见的一种，必须原样告知，不能算未命中。
    """
    from src.db.models import CopilotRun, CopilotStep

    steps = (
        db.query(CopilotStep)
        .filter(CopilotStep.run_id == run_id)
        .order_by(CopilotStep.seq)
        .all()
    )
    scen = [s for s in steps if str(getattr(s, "tool", "")) == SCENARIO_TOOL]
    if not scen:
        return _unavailable("该轮没有情景矩阵读数", run_id=run_id)
    # 优先取成功的；全失败时也保留（文本里通常是「[情景矩阵] 关键位不足」，解析会拿不到）
    step = next((s for s in scen if bool(getattr(s, "ok", True))), scen[-1])

    levels = extract_levels(getattr(step, "result_digest", None) or "")
    if not levels:
        return _unavailable("情景矩阵文本里没有可解析的关键位", run_id=run_id)
    lvl_r, lvl_s = levels.get("resist"), levels.get("support")
    if lvl_r is None or lvl_s is None:
        return _unavailable("关键位不全（只有一端），无法判定先后", run_id=run_id, levels=levels)

    symbol = _parse_symbol(getattr(step, "args", None))
    if not symbol:
        return _unavailable("工具参数里没有标的代码", run_id=run_id, levels=levels)

    row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).one_or_none()
    if row is None or getattr(row, "created_at", None) is None:
        return _unavailable("找不到该轮的运行记录（缺基准日期）", run_id=run_id, levels=levels)

    created = row.created_at
    run_day = created.date() if isinstance(created, datetime) else created
    lookback = max(_MIN_LOOKBACK_DAYS, (datetime.now().date() - run_day).days + 10)
    bars = load_qfq_bars_cached(symbol, market, days=lookback) or []

    # 严格晚于该轮当天：盘中写的关键位不能拿当天的 K 线判（那是同一根，等于自证）
    fwd = [b for b in bars if str(b.get("date") or "")[:10] > run_day.isoformat()]
    fwd = [b for b in fwd if all(k in b for k in ("open", "high", "low", "close"))]
    if len(fwd) < max(1, int(horizon_td)):
        return _unavailable(
            f"行情尚未走完（需要 T+{int(horizon_td)}）",
            run_id=run_id, symbol=symbol, levels=levels,
            horizon_td=int(horizon_td), have_bars=len(fwd),
        )

    win = fwd[: int(horizon_td)]
    resist = float(lvl_r)
    support = float(lvl_s)

    hit_resist = any(float(b["high"]) >= resist for b in win)
    hit_support = any(float(b["low"]) <= support for b in win)
    side, idx = _first_touch(win, resist, support)

    error_bp: float | None = None
    direction: str | None = None
    if side == "resist":
        extreme = max(float(b["high"]) for b in win)
        error_bp = round(abs(extreme - resist) / resist * 10000.0, 1)
    elif side == "support":
        extreme = min(float(b["low"]) for b in win)
        error_bp = round(abs(support - extreme) / support * 10000.0, 1)

    if idx is not None:
        touch_close = float(win[idx]["close"])
        last_close = float(win[-1]["close"])
        if touch_close > 0:
            diff_bp = (last_close - touch_close) / touch_close * 10000.0
            direction = "flat" if abs(diff_bp) < FLAT_BAND_BP else ("up" if diff_bp > 0 else "down")

    return {
        "available": True,
        "run_id": run_id,
        "symbol": symbol,
        "as_of": str(win[-1].get("date") or "")[:10],
        "levels": {"resist": resist, "support": support},
        "horizon_td": int(horizon_td),
        "hit_resist": bool(hit_resist),
        "hit_support": bool(hit_support),
        "first_touch": side,
        "error_bp": error_bp,
        "direction_after_touch": direction,
        "soul": {
            "scored_on": f"T+1..T+{int(horizon_td)} 日K",
            "bars": [str(b.get("date") or "")[:10] for b in win],
            "flat_band_bp": FLAT_BAND_BP,
            "touch_order_note": "同一根 K 线双触时按「离开盘价更近的一端」定序（无法知道真实先后）",
            "why_not_advice_quality": "不评建议好坏：缺用户意图真值（当时想干什么/有没有照做）",
        },
    }


def _pct(part: int, total: int) -> float | None:
    return round(part / total, 3) if total else None


def aggregate(db, *, days: int = 30, min_samples: int = 20) -> dict:
    """窗口内按配方聚合关键位命中率与平均误差 bp。

    **n < min_samples 一律拒绝出率**：返回 `{available: False, n, reason: "样本不足"}`，
    响应里不出现任何 `*_rate` 键 —— 让「0% 命中」这种伪结论从源头不可能被写进报告。
    """
    from src.db.models import CopilotRun

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(0, int(days)))
    rows = (
        db.query(CopilotRun)
        .filter(CopilotRun.created_at >= cutoff)
        .order_by(CopilotRun.created_at.desc())
        .all()
    )

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid = str(getattr(row, "recipe", None) or "unrouted")
        scored = score_run(db, row.run_id)
        if not scored.get("available"):
            continue
        b = buckets.setdefault(rid, {"n": 0, "hit_resist": 0, "hit_support": 0, "any_hit": 0,
                                     "errors": []})
        b["n"] += 1
        b["hit_resist"] += int(bool(scored.get("hit_resist")))
        b["hit_support"] += int(bool(scored.get("hit_support")))
        b["any_hit"] += int(bool(scored.get("hit_resist")) or bool(scored.get("hit_support")))
        if scored.get("error_bp") is not None:
            b["errors"].append(float(scored["error_bp"]))

    n_total = sum(b["n"] for b in buckets.values())
    if n_total < int(min_samples):
        # 不出率、不出均值 —— 只有 n 与原因
        return {
            "available": False,
            "n": n_total,
            "reason": "样本不足",
            "min_samples": int(min_samples),
            "window_days": int(days),
            "scored_recipes": len(buckets),
        }

    by_recipe: dict[str, dict] = {}
    all_err: list[float] = []
    for rid, b in sorted(buckets.items(), key=lambda kv: kv[1]["n"], reverse=True):
        all_err.extend(b["errors"])
        if b["n"] < int(min_samples):
            by_recipe[rid] = {"n": b["n"], "available": False,
                              "reason": "样本不足", "min_samples": int(min_samples)}
            continue
        by_recipe[rid] = {
            "n": b["n"],
            "available": True,
            "hit_resist_rate": _pct(b["hit_resist"], b["n"]),
            "hit_support_rate": _pct(b["hit_support"], b["n"]),
            "any_hit_rate": _pct(b["any_hit"], b["n"]),
            "mean_error_bp": round(sum(b["errors"]) / len(b["errors"]), 1) if b["errors"] else None,
        }

    hr = sum(b["hit_resist"] for b in buckets.values())
    hs = sum(b["hit_support"] for b in buckets.values())
    anyh = sum(b["any_hit"] for b in buckets.values())
    return {
        "available": True,
        "n": n_total,
        "window_days": int(days),
        "min_samples": int(min_samples),
        "hit_resist_rate": _pct(hr, n_total),
        "hit_support_rate": _pct(hs, n_total),
        "any_hit_rate": _pct(anyh, n_total),
        "mean_error_bp": round(sum(all_err) / len(all_err), 1) if all_err else None,
        "by_recipe": by_recipe,
        "soul": {
            "n_is_the_point": "A 股没有即时真值；命中率只在 n >= min_samples 时给，n 必须随率一起出现",
            "not_scored": "无情景矩阵 / 行情未走完 / 关键位不全的轮次不计入 n（既不分子也不分母）",
        },
    }
