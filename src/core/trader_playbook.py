# -*- coding: utf-8 -*-
"""交易模式画像(2026-10-22) —— 用**证据**说清主人的交易思维: 长项/短板各带样本量与置信度。

## 为什么
数智参谋要"更懂我的交易模式", 但**画像不能靠夸**。本模块只做两件事:
1. 把已有证据**算成可核对的数字**(每条结论都带 n 与置信度);
2. 证据不够就**明说不够**, 绝不用猜测填坑。

## 证据源(两个, 口径不同, 绝不混用)
### A. 本人交易证据 —— `users.shadow_profile_json`(交割单聚合画像)
唯一能代表"主人自己怎么交易"的证据: 已平仓回合数、盈利回合数、持仓周期、
市场范围、提炼出的规则。生产值: 457 回合 / 249 盈利(54.5%) / 持仓中位 5 天(P75 21 天)/
区间 2024-01-24 ~ 2026-08-11。**长项/短板只从这里出**。

### B. 系统信号台账 —— `decision_log`
**这不是本人的成交记录**, 是系统生成的信号(resonance3 / demon_pool / bdqk_confirm …)
连同其后真实收益的台账。生产 8013 行。
它衡量的是**信号池质量**, **绝不能写成"主人胜率"** —— 那就是编造用户业绩。
故本模块把它单独放在 `signal_ledger` 段, 不进 strengths/weaknesses, 也不进 render 正文。

## 三条纪律
1. **宁缺不编**: 本人只有聚合画像、没有逐笔 → 盈亏比/加仓行为/止损纪律**算不出来**,
   `weaknesses` 允许为空数组, 并在 notes 显式写明"本人逐笔成交未落库"。
2. **样本不足显式标注**: n < 8 根本不产出结论(只进 notes); n<30 标 low,
   30~100 标 mid, >100 标 high。绝不拿 3 个样本算 67%。
3. **只讲行为特征, 不下买卖指令**: 输出是"他通常怎么拿/多久了结/在哪些范围内交易",
   不是"该买该卖"。`render()` 里不出现任何买卖指令措辞。

## 方言
SQL 只用标准聚合(CASE/SUM/AVG/COUNT/MIN/MAX/substr/length) + 绑定参数,
**不用方言特有函数**(不用 `json_extract` / `::jsonb` / `FILTER (WHERE ...)` /
`ROUND(x, n)` / `GLOB`) —— SQLite 与 PG 都能跑(测试用 sqlite)。
`users.shadow_profile_json` 在 PG 是 json 列、在 SQLite 是 TEXT 列 → 一律当**文本**处理。
日期归一在 Python 侧做(紧凑 20251225 与 ISO 2026-10-13 混排, SQL 侧排序会错)。

## 接线(主控一行, 本模块不碰 `src/web/api/chat.py`)
应插入位置: `src/core/ai_client.py::build_system_prompt` 里"用户交易风格画像"段之后。
一行示例:

    from src.core.trader_playbook import block as _tp_block
    _tp = _tp_block(db, getattr(user, "id", None)) if user is not None else ""
    if _tp:
        base_prompt = base_prompt + "\n\n--- 交易模式画像 ---\n" + _tp

`block()` 内部异常一律返回空串(熔断), 绝不拖垮对话。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

#: 结论门槛: n < 8 根本不产出结论(只进 notes, 避免拿 3 个样本算 67%)
MIN_CONCLUSION = 8
#: 置信度分档: n < 30 → low; 30 ~ 100 → mid; > 100 → high
LOW_N = 30
MID_N = 100

#: 信号台账口径声明(**必须逐字出现在 notes 里**, 防止下游把它当本人业绩)
LEDGER_NOTE = "信号台账来自系统生成信号，非本人成交，仅用于衡量信号池质量"

#: 本人逐笔缺失时的口径声明(必须逐字出现在 notes 里)
NO_TICK_NOTE = "本人逐笔成交未落库，盈亏比/加仓行为/止损纪律无法计算"

#: 市场代码 → 中文名(画像 preferred_markets 用)
_MARKET_LABELS = {"china_a": "A 股", "hk": "港股", "us": "美股"}

#: 情绪周期 key → 中文名(与 `src.core.market_regime.REGIME_LABELS` 同源, 缺库时回退本表)
_REGIME_LABELS_FALLBACK = {
    "ice": "冰点",
    "repair": "修复",
    "ferment": "发酵",
    "climax": "高潮",
    "ebb": "退潮",
    "unknown": "数据不足",
}

#: render() 输出上限(字)—— 给系统提示用的紧凑块, 超过即截断
RENDER_MAX_CHARS = 600


# ────────────────────────────── 基础工具 ──────────────────────────────


def _today_cst() -> str:
    return datetime.now(_CST).date().isoformat()


def _confidence(n: int) -> str:
    """样本量 → 置信度分档(n<8 不会走到这里)。"""
    if n < LOW_N:
        return "low"
    if n <= MID_N:
        return "mid"
    return "high"


def _pct(x: float | None) -> str:
    """小数 → 百分比字符串(1 位)。None → '—'(不补 0)。"""
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"


def _ret_pct(x: float | None) -> str:
    """收益均值 → 百分比(带符号)。None → '—'。"""
    if x is None:
        return "—"
    return f"{x * 100:+.2f}%"


def _iso_day(raw: Any) -> str:
    """日期 → ISO(紧凑 20251225 / ISO 2026-10-13 都认; 认不出原样返回)。"""
    s = str(raw or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) >= 10:
        return s[:10]
    return s


def _exec(db: Any, sql: str, params: dict[str, Any]) -> list:
    """执行只读 SQL。

    `db` 可以是 Session(ORM 会话) / Engine(引擎) / Connection(连接) ——
    三个都支持, 因为调用方既有传会话的(chat 工具)也有传引擎的(批处理/生产验证)。
    """
    if isinstance(db, Engine):
        with db.connect() as conn:
            return list(conn.execute(text(sql), params).fetchall())
    if isinstance(db, Connection):
        return list(db.execute(text(sql), params).fetchall())
    if isinstance(db, Session):
        return list(db.execute(text(sql), params).fetchall())
    # 兜底: 任何带 execute 的对象
    return list(db.execute(text(sql), params).fetchall())


def _engine_of(db: Any) -> Any:
    """拿到底层 engine(用于查列是否存在); 拿不到返回 None(永不抛)。"""
    if db is None:
        return None
    get_bind = getattr(db, "get_bind", None)
    if callable(get_bind):
        try:
            return get_bind()
        except Exception:  # noqa: BLE001
            return None
    return db


def _has_column(db: Any, table: str, column: str) -> bool:
    """表是否有某列(兼容未跑 v197/v198 的老库); 查不到 → False(视作没有)。"""
    bind = _engine_of(db)
    if bind is None:
        return False
    try:
        from sqlalchemy import inspect as _inspect

        cols = {c["name"] for c in _inspect(bind).get_columns(table)}
        return column in cols
    except Exception:  # noqa: BLE001
        return False


def _regime_label(regime: str) -> str:
    """regime key → 中文名(优先用仓库常量, 缺库回退本表)。"""
    try:
        from src.core.market_regime import REGIME_LABELS

        return REGIME_LABELS.get(regime, regime)
    except Exception:  # noqa: BLE001
        return _REGIME_LABELS_FALLBACK.get(regime, regime)


# ────────────────────────────── 证据读取 ──────────────────────────────


def _read_profile(db: Any, user_id: Any) -> dict[str, Any]:
    """读本人交割单聚合画像(users.shadow_profile_json)。

    跨方言取值形态:
      - PG(json 列) → psycopg2 直接给 dict;
      - SQLite(JSON 列存 TEXT) → 给 JSON 文本;
      - 老库(迁移加的 TEXT 列) → 同样是 JSON 文本, 但可能被**双重编码**
        (存的是 '"{...}"' 这种字符串里套 JSON) → 逐层解包直到拿到 dict。
    读不到/坏数据 → {} (绝不编造)。
    """
    uid = str(user_id or "").strip()
    if not uid:
        return {}
    rows = _exec(db, "SELECT shadow_profile_json FROM users WHERE id = :uid", {"uid": uid})
    if not rows:
        return {}
    raw = rows[0][0]
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    # 最多解 3 层(正常 1 层; 双重编码 2 层; 再多视为脏数据)
    for _ in range(3):
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return {}
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return raw if isinstance(raw, dict) else {}


#: 停牌跨期行(outcome_state='suspended_gap')不进胜率/均值 —— 与 `decision_log.stats`
#: 同口径(排除加在**分子/分母**上, n_total 仍报全量行数, 排除条数单列标注)。
def _ledger_excl(db: Any) -> str:
    if _has_column(db, "decision_log", "outcome_state"):
        return "COALESCE(outcome_state, '') <> 'suspended_gap'"
    return "1 = 1"


def _agg_expr(excl: str) -> str:
    """账本聚合表达式(标准 SQL; 未回填的行不进分母 —— 未回填不是"没命中")。"""
    e = f"{excl} AND " if excl else ""
    return (
        "COUNT(*) AS n_total, "
        f"SUM(CASE WHEN {e}hit_t1 IS NOT NULL THEN 1 ELSE 0 END) AS n_t1, "
        f"SUM(CASE WHEN {e}hit_t1 = 1 THEN 1 ELSE 0 END) AS w_t1, "
        f"SUM(CASE WHEN {e}ret_t1 IS NOT NULL THEN 1 ELSE 0 END) AS n_ret1, "
        f"AVG(CASE WHEN {e}ret_t1 IS NOT NULL THEN ret_t1 END) AS avg_ret1, "
        f"SUM(CASE WHEN {e}ret_t3 IS NOT NULL THEN 1 ELSE 0 END) AS n_ret3, "
        f"AVG(CASE WHEN {e}ret_t3 IS NOT NULL THEN ret_t3 END) AS avg_ret3, "
        f"SUM(CASE WHEN {e}ret_t5 IS NOT NULL THEN 1 ELSE 0 END) AS n_ret5, "
        f"AVG(CASE WHEN {e}ret_t5 IS NOT NULL THEN ret_t5 END) AS avg_ret5"
    )


def _ledger_group(db: Any, dim: str) -> list[dict[str, Any]]:
    """按维度分组聚合账本(标准 SQL; 命中分母=已回填行)。"""
    sql = (
        f"SELECT {dim} AS bucket, {_agg_expr(_ledger_excl(db))} "
        "FROM decision_log "
        f"GROUP BY {dim} "
        "ORDER BY COUNT(*) DESC"
    )
    out: list[dict[str, Any]] = []
    for row in _exec(db, sql, {}):
        out.append(
            {
                "bucket": str(row[0]) if row[0] is not None else "unknown",
                "n_total": int(row[1] or 0),
                "n_t1": int(row[2] or 0),
                "w_t1": int(row[3] or 0),
                "n_ret1": int(row[4] or 0),
                "avg_ret1": float(row[5]) if row[5] is not None else None,
                "n_ret3": int(row[6] or 0),
                "avg_ret3": float(row[7]) if row[7] is not None else None,
                "n_ret5": int(row[8] or 0),
                "avg_ret5": float(row[9]) if row[9] is not None else None,
            }
        )
    return out


def _ledger_overall(db: Any) -> dict[str, Any]:
    """账本整体口径(总行数 / 已回填分母 / 全量口径胜率 / 均值 / 排除条数)。"""
    excl = _ledger_excl(db)
    has_state = _has_column(db, "decision_log", "outcome_state")
    gap_expr = (
        "SUM(CASE WHEN outcome_state = 'suspended_gap' THEN 1 ELSE 0 END) AS n_gap"
        if has_state
        else "0 AS n_gap"
    )
    sql = f"SELECT {_agg_expr(excl)}, {gap_expr} FROM decision_log"
    rows = _exec(db, sql, {})
    if not rows:
        return {}
    row = rows[0]
    return {
        "n_total": int(row[0] or 0),
        "n_t1": int(row[1] or 0),
        "w_t1": int(row[2] or 0),
        "n_ret1": int(row[3] or 0),
        "avg_ret1": float(row[4]) if row[4] is not None else None,
        "n_ret3": int(row[5] or 0),
        "avg_ret3": float(row[6]) if row[6] is not None else None,
        "n_ret5": int(row[7] or 0),
        "avg_ret5": float(row[8]) if row[8] is not None else None,
        "n_gap": int(row[9] or 0),
    }


def _ledger_window(db: Any) -> list[str | None]:
    """账本信号日区间(Python 侧归一: 紧凑/ISO 混排时 SQL 的 MIN/MAX 会排错)。"""
    try:
        rows = _exec(db, "SELECT DISTINCT trade_date FROM decision_log", {})
    except Exception:  # noqa: BLE001
        return [None, None]
    days = sorted(d for d in (_iso_day(r[0]) for r in rows) if d)
    if not days:
        return [None, None]
    return [days[0], days[-1]]


# ────────────────────────────── 本人画像 → 长项/短板 ──────────────────────────────


def _own_facts(profile: dict[str, Any]) -> dict[str, Any]:
    """从聚合画像里抽出**可核对的事实**(不做任何推断)。"""
    total = int(profile.get("total_roundtrips") or 0)
    profit = int(profile.get("profitable_roundtrips") or 0)
    holding = profile.get("typical_holding_days")
    median = p75 = None
    if isinstance(holding, (list, tuple)) and len(holding) >= 2:
        median = float(holding[0]) if holding[0] is not None else None
        p75 = float(holding[1]) if holding[1] is not None else None
    elif isinstance(holding, (int, float)):
        median = float(holding)
    rng = profile.get("date_range") or []
    window: list[str | None] = [None, None]
    if isinstance(rng, (list, tuple)) and len(rng) >= 2:
        window = [_iso_day(rng[0]) or None, _iso_day(rng[1]) or None]
    window = [w if w else None for w in window]
    markets = [str(m) for m in (profile.get("preferred_markets") or []) if m]
    rules = [r for r in (profile.get("rules") or []) if isinstance(r, dict)]
    trades = [t for t in (profile.get("trades") or []) if isinstance(t, dict)]
    win_rate = (profit / total) if total > 0 else None
    return {
        "total": total,
        "profit": profit,
        "loss": max(total - profit, 0) if total else 0,
        "win_rate": win_rate,
        "median": median,
        "p75": p75,
        "window": window,
        "markets": markets,
        "rules": rules,
        "trades": trades,
        "profile_text": str(profile.get("profile_text") or "").strip(),
    }


def _own_strengths(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """长项 = 有证据支撑的**行为特征**(不是夸, 不含买卖指令措辞)。"""
    out: list[dict[str, Any]] = []
    n = facts["total"]
    if n < MIN_CONCLUSION:
        return out

    # ① 回合胜率(只讲"回合口径", 明确未计盈亏比)
    if facts["win_rate"] is not None:
        out.append(
            {
                "key": "roundtrip_win_rate",
                "text": (
                    f"回合胜率 {_pct(facts['win_rate'])}（{facts['profit']}/{n} 笔平仓回合盈利）"
                    "—— 高于五成, 但未计盈亏比, 不可据此判定正期望"
                ),
                "metric": {
                    "win_rate": round(facts["win_rate"], 4),
                    "profitable_roundtrips": facts["profit"],
                    "total_roundtrips": n,
                },
                "n": n,
                "confidence": _confidence(n),
            }
        )

    # ② 持仓节奏
    if facts["median"] is not None:
        med, p75 = facts["median"], facts["p75"]
        if p75 is not None:
            text_ = f"持仓节奏偏短: 中位 {med:g} 天, P75 {p75:g} 天 —— 多数仓位在 P75 内了结"
            metric = {"holding_days_median": med, "holding_days_p75": p75}
        else:
            text_ = f"持仓节奏偏短: 中位 {med:g} 天"
            metric = {"holding_days_median": med}
        out.append(
            {
                "key": "short_horizon",
                "text": text_,
                "metric": metric,
                "n": n,
                "confidence": _confidence(n),
            }
        )

    # ③ 交易范围收敛
    if facts["markets"]:
        labels = [_MARKET_LABELS.get(m, m) for m in facts["markets"]]
        out.append(
            {
                "key": "market_focus",
                "text": f"交易范围收敛: {n} 回合全部落在 {'、'.join(labels)}",
                "metric": {"preferred_markets": facts["markets"]},
                "n": n,
                "confidence": _confidence(n),
            }
        )

    return out


def _own_weaknesses(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """短板 = 有证据支撑的**行为特征缺口**; 证据不足就少给, 绝不用猜测补。

    本人只有聚合画像 → 盈亏比/加仓行为/止损纪律**算不出来**(见 NO_TICK_NOTE),
    这里只写能从聚合画像直接读出的缺口。
    """
    out: list[dict[str, Any]] = []
    n = facts["total"]
    if n < MIN_CONCLUSION:
        return out

    # ① 画像规则的"区分度": coverage_rate 是相对**盈利回合**算的 —— 覆盖越高, 越说明
    #    它只描述了"盈利回合长什么样", 缺负样本对照 → 不足以当筛选条件。
    best_rule = None
    for rule in facts["rules"]:
        sup = int(rule.get("support_count") or 0)
        if best_rule is None or sup > int(best_rule.get("support_count") or 0):
            best_rule = rule
    if best_rule is not None:
        sup = int(best_rule.get("support_count") or 0)
        cov = best_rule.get("coverage_rate")
        rng = best_rule.get("holding_days_range") or []
        span = ""
        if isinstance(rng, (list, tuple)) and len(rng) >= 2:
            span = f", 持有区间 {rng[0]}-{rng[1]} 天"
        if sup >= MIN_CONCLUSION:
            out.append(
                {
                    "key": "rule_low_discrimination",
                    "text": (
                        f"画像规则缺区分力: 覆盖 {_pct(cov) if cov is not None else '—'}"
                        f" 的盈利回合({sup} 笔{span}) —— 它只描述『盈利回合长什么样』, "
                        "缺负样本对照, 还不能当筛选条件"
                    ),
                    "metric": {
                        "rule_id": best_rule.get("rule_id"),
                        "support_count": sup,
                        "coverage_rate": cov,
                    },
                    "n": sup,
                    "confidence": _confidence(sup),
                }
            )

    # ② 持仓节奏分化: 中位与 P75 差距大 → 快节奏与拖长两种节奏并存
    if facts["median"] is not None and facts["p75"] is not None:
        med, p75 = facts["median"], facts["p75"]
        if p75 > 0 and p75 >= med * 2:
            out.append(
                {
                    "key": "holding_dispersion",
                    "text": (
                        f"持仓节奏分化: 中位 {med:g} 天但 P75 达 {p75:g} 天 —— "
                        "快节奏与拖长两种持仓并存, 了结口径不统一"
                    ),
                    "metric": {"holding_days_median": med, "holding_days_p75": p75},
                    "n": n,
                    "confidence": _confidence(n),
                }
            )

    return out


# ────────────────────────────── 主入口 ──────────────────────────────


def build(db: Any, user_id: Any) -> dict[str, Any]:
    """算交易模式画像(只读)。

    Returns:
        {
          as_of, sample{decisions, roundtrips, window, ledger_window},
          baseline{win_rate, holding_days_median, holding_days_p75},
          strengths[{key,text,metric,n,confidence}], weaknesses[同构],
          by_kind[{signal_kind,n,win_t1,avg_ret_t1,avg_ret_t3,avg_ret_t5,...}],
          by_regime[{regime,label,...}],
          signal_ledger{n,win_t1,avg_ret_t1,avg_ret_t5,...},
          notes[str],
        }
    """
    out: dict[str, Any] = {
        "as_of": _today_cst(),
        "sample": {
            "decisions": 0,
            "roundtrips": 0,
            "window": [None, None],
            "ledger_window": [None, None],
        },
        "baseline": {
            "win_rate": None,
            "holding_days_median": None,
            "holding_days_p75": None,
        },
        "strengths": [],
        "weaknesses": [],
        "by_kind": [],
        "by_regime": [],
        "signal_ledger": {
            "n": 0,
            "win_t1": None,
            "avg_ret_t1": None,
            "avg_ret_t5": None,
            "note": LEDGER_NOTE,
        },
        "notes": [],
        #: 证据缺口条目(真正的"样本不足/算不出")—— render 的「样本不足未结论」段用它,
        #: 不用在 notes 里靠字符串匹配挑(口径说明里也会出现"未落库"字样)。
        "gaps": [],
        #: 读库本身失败(不是"用户没数据")。block() 遇此熔断返回空串 ——
        #: 免得把系统故障当成"样本不足"塞进系统提示。
        "fatal": False,
    }
    notes: list[str] = out["notes"]
    #: 证据缺口条目(真正的"样本不足/算不出")—— 供 render 的「样本不足未结论」段用。
    #: 与 notes 分开记, 免得靠字符串匹配挑(口径说明里也会出现"未落库"字样)。
    gaps: list[str] = out["gaps"]

    # ── A. 本人交易证据(交割单聚合画像) ──
    profile: dict[str, Any] = {}
    profile_err = ""
    try:
        profile = _read_profile(db, user_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到就如实说, 不编
        profile_err = f"{type(exc).__name__}: {exc}"
        out["fatal"] = True
    facts = _own_facts(profile)

    if profile_err:
        notes.append(f"本人画像读取失败({profile_err}) —— 长项/短板不产出")
        gaps.append(f"本人画像读取失败({profile_err}) —— 长项/短板不产出")
    if not profile:
        _msg = "未上传交割单画像(users.shadow_profile_json 为空) —— 无本人交易证据, 不产出长项/短板"
        notes.append(_msg)
        gaps.append(_msg)
    else:
        out["sample"]["roundtrips"] = facts["total"]
        out["sample"]["window"] = facts["window"]
        out["baseline"] = {
            "win_rate": round(facts["win_rate"], 4) if facts["win_rate"] is not None else None,
            "holding_days_median": facts["median"],
            "holding_days_p75": facts["p75"],
        }
        out["strengths"] = _own_strengths(facts)
        out["weaknesses"] = _own_weaknesses(facts)
        notes.append(
            f"胜率口径: 已平仓回合中盈利回合占比({facts['profit']}/{facts['total']}), "
            "不含盈亏比(逐笔未落库) —— 不可据此判定正期望"
        )
        notes.append("持仓周期取自交割单画像 typical_holding_days(中位/P75), 非逐笔重算")
        if len(facts["trades"]) < MIN_CONCLUSION:
            notes.append(NO_TICK_NOTE)
            gaps.append(NO_TICK_NOTE)
        else:
            notes.append(
                f"本人逐笔成交已落库 {len(facts['trades'])} 笔, 本模块仍只用聚合画像口径(未做逐笔指标)"
            )
        if not out["weaknesses"]:
            _gap = "短板暂无可下结论项: 现有证据不足以证明哪一项是缺口(宁缺不编)"
            notes.append(_gap)
            gaps.append(_gap)

    # ── B. 系统信号台账(decision_log): 衡量信号池质量, **不是本人战绩** ──
    try:
        overall = _ledger_overall(db)
        kinds = _ledger_group(db, "signal_kind")
        regimes = _ledger_group(db, "COALESCE(NULLIF(regime, ''), 'unknown')")
        ledger_window = _ledger_window(db)
    except Exception as exc:  # noqa: BLE001 —— 账本不可用 → 该段缺失, 不影响本人画像
        overall, kinds, regimes, ledger_window = {}, [], [], [None, None]
        notes.append(f"信号台账读取失败({type(exc).__name__}: {exc}) —— 该段不产出")
        out["fatal"] = True

    if overall:
        n_total = int(overall.get("n_total") or 0)
        n_t1 = int(overall.get("n_t1") or 0)
        w_t1 = int(overall.get("w_t1") or 0)
        win_t1 = (w_t1 / n_t1) if n_t1 else None
        win_all = (w_t1 / n_total) if n_total else None
        out["sample"]["decisions"] = n_total
        out["sample"]["ledger_window"] = ledger_window
        out["signal_ledger"] = {
            "n": n_total,
            "n_t1": n_t1,
            "win_t1": round(win_t1, 4) if win_t1 is not None else None,
            "win_t1_all_rows": round(win_all, 4) if win_all is not None else None,
            "avg_ret_t1": round(overall["avg_ret1"], 6) if overall.get("avg_ret1") is not None else None,
            "avg_ret_t3": round(overall["avg_ret3"], 6) if overall.get("avg_ret3") is not None else None,
            "avg_ret_t5": round(overall["avg_ret5"], 6) if overall.get("avg_ret5") is not None else None,
            "n_ret_t1": int(overall.get("n_ret1") or 0),
            "n_ret_t3": int(overall.get("n_ret3") or 0),
            "n_ret_t5": int(overall.get("n_ret5") or 0),
            "suspended_gap_excluded": int(overall.get("n_gap") or 0),
            "note": LEDGER_NOTE,
        }
        notes.append(LEDGER_NOTE)
        notes.append(
            f"信号台账胜率分母 = 已回填 {n_t1} 行(未回填 {max(n_total - n_t1, 0)} 行不进分母); "
            f"若按全量 {n_total} 行计, T+1 命中为 {_pct(win_all)} —— 两种口径别混用"
        )
        notes.append(
            "decision_log 无 user_id 列 → 信号台账是全站系统信号, 与本人无关; "
            f"已排除停牌跨期 {int(overall.get('n_gap') or 0)} 行(outcome_state='suspended_gap')"
        )

        for label, rows, key in (
            ("signal_kind", kinds, "by_kind"),
            ("regime", regimes, "by_regime"),
        ):
            kept: list[dict[str, Any]] = []
            for row in rows:
                n_row = int(row["n_total"])
                if n_row < MIN_CONCLUSION:
                    _gap = f"信号台账 {label}={row['bucket']} 样本 n={n_row} < {MIN_CONCLUSION}, 不出结论"
                    notes.append(_gap)
                    gaps.append(_gap)
                    continue
                nt1 = int(row["n_t1"])
                entry: dict[str, Any] = {
                    "n": n_row,
                    "n_t1": nt1,
                    "win_t1": round(row["w_t1"] / nt1, 4) if nt1 else None,
                    "avg_ret_t1": round(row["avg_ret1"], 6) if row["avg_ret1"] is not None else None,
                    "avg_ret_t3": round(row["avg_ret3"], 6) if row["avg_ret3"] is not None else None,
                    "avg_ret_t5": round(row["avg_ret5"], 6) if row["avg_ret5"] is not None else None,
                    "confidence": _confidence(n_row),
                }
                if key == "by_kind":
                    entry = {"signal_kind": row["bucket"], **entry}
                else:
                    entry = {"regime": row["bucket"], "label": _regime_label(row["bucket"]), **entry}
                kept.append(entry)
            out[key] = kept

    return out


# ────────────────────────────── 渲染 ──────────────────────────────


def _insufficient_items(playbook: dict[str, Any]) -> list[str]:
    """样本不足/没出结论的项(给人看: 哪些还欠数据)。

    优先用 `gaps`(build() 显式标出来的证据缺口); 老结构没有 gaps 字段时
    回退到 notes 的字符串匹配(兼容手搓的 playbook dict)。
    """
    gaps = playbook.get("gaps")
    if isinstance(gaps, list) and gaps:
        return [str(g) for g in gaps]
    items: list[str] = []
    for note in playbook.get("notes") or []:
        s = str(note)
        if any(k in s for k in ("样本不足", "无法计算", "未落库", "n <", "宁缺不编")):
            items.append(s)
    for note in playbook.get("notes") or []:
        s = str(note)
        if s not in items and any(k in s for k in ("不产出", "不出结论")):
            items.append(s)
    return items


def render(playbook: dict[str, Any]) -> str:
    """给系统提示用的紧凑文本块(≤600 字)。

    纪律: 无数据写"样本不足"; 本人胜率只能引用交割单画像的回合数;
    **不出现任何买卖指令措辞**(只讲行为特征); 信号台账数字不进正文。
    """
    pb = playbook or {}
    sample = pb.get("sample") or {}
    base = pb.get("baseline") or {}
    as_of = str(pb.get("as_of") or _today_cst())

    roundtrips = int(sample.get("roundtrips") or 0)
    win_rate = base.get("win_rate")
    median = base.get("holding_days_median")
    p75 = base.get("holding_days_p75")

    if not roundtrips or win_rate is None:
        head = f"主人交易模式（证据 {as_of}）：样本不足 —— 无本人交割单画像（0 回合），不产出长项/短板。"
    else:
        hold_txt = f"持仓中位 {median:g} 天" if median is not None else "持仓周期未知"
        if p75 is not None:
            hold_txt += f"（P75 {p75:g} 天）"
        head = (
            f"主人交易模式（证据 {as_of}）：胜率 {_pct(win_rate)}（{roundtrips} 回合，"
            f"盈亏比未知）｜{hold_txt}。"
        )

    def _seg(items: list[dict[str, Any]], limit: int = 2) -> str:
        if not items:
            return "无（证据不足，未下结论）"
        return "；".join(
            f"{str(it.get('text') or '')}（n={it.get('n')}）" for it in items[:limit]
        )

    body = f"长项：{_seg(pb.get('strengths') or [])}。短板：{_seg(pb.get('weaknesses') or [])}。"

    insufficient = _insufficient_items(pb)
    if insufficient:
        body += f"样本不足未结论：{insufficient[0]}"
        for extra in insufficient[1:3]:
            body += f"；{extra}"
    else:
        body += "样本不足未结论：无。"

    text = head + body
    if len(text) > RENDER_MAX_CHARS:
        text = text[: RENDER_MAX_CHARS - 1] + "…"
    return text


def block(db: Any, user_id: Any) -> str:
    """系统提示接线用的文本块 —— **异常一律返回空串**(熔断), 绝不拖垮对话。

    熔断口径: 读库本身失败(fatal)→ 空串(系统故障不该被当成"样本不足"注入提示);
    用户**确实没数据**(没上传交割单)→ 正常返回"样本不足"文本(这是有效信息)。
    """
    try:
        playbook = build(db, user_id)
    except Exception as exc:  # noqa: BLE001 —— 旁路, 只 warning
        logger.warning("交易模式画像生成失败(已熔断): %r", exc)
        return ""
    if playbook.get("fatal"):
        logger.warning("交易模式画像读库失败(已熔断, 返回空串)")
        return ""
    try:
        return render(playbook)
    except Exception as exc:  # noqa: BLE001
        logger.warning("交易模式画像渲染失败(已熔断): %r", exc)
        return ""


def to_json(playbook: dict[str, Any]) -> str:
    """结构化输出(JSON 文本, 供 chat 工具直接返回)。"""
    return json.dumps(playbook or {}, ensure_ascii=False, indent=2, default=str)


__all__ = [
    "LEDGER_NOTE",
    "MIN_CONCLUSION",
    "NO_TICK_NOTE",
    "RENDER_MAX_CHARS",
    "block",
    "build",
    "render",
    "to_json",
]
