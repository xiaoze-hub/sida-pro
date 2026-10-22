"""离线复盘（S0）—— 只提建议，绝不改配置（2026-10-23，受控 RSI 证据层）。

**这一层的定位**：把 `copilot_runs` / `copilot_steps` 里已经躺着的数据，翻成
「哪份配方的哪条规则该改」的**候选清单**，交给人审。它不写任何文件，不碰
`src/harness/recipes/*.yaml`、`src/harness/skills/*.md`、`SYSTEM_PROMPT`。

为什么必须「只提议」：
- 冻结环（symbol_guard / number_audit / caliber 口径 / 交易 403 / TQ 优先链 /
  提示词约束段）一旦能被自动改写，一次错误的进化就把护栏拆了；
- 可变环（配方 when/tools、技能手册文本）也只能**半自动**：市场 regime 会切换，
  退潮期学到的「炸板核按钮」在主升期会把弱转强全部误杀；
- 样本极小（4 个账号、日均几十轮），任何率都必须带 n，n 不够就只说「样本不足」。

**证据优先于结论**：每条 proposal 的 `evidence` 必须能落到具体 run_id + 具体数字，
不允许「整体偏慢」这种话。没有证据就不出建议。

只读实现约束：本模块 **不 import yaml / recipes / skills**，不 open 任何配置文件；
`target` 只是把「该改哪个文件」写成字符串，供人去看。
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 冻结环：任何 proposal 都不许指向它们（render 里会原样打印，让审核者一眼看到边界）
FROZEN_RING = (
    "symbol_guard（零工具拦截 / 标的核对）",
    "number_audit（数字对账）",
    "caliber.py（逐笔 vs 东财四档口径红线）",
    "actions 交易 403（下单类一律拒绝）",
    "TQ 优先链（行情源优先级）",
    "SYSTEM_PROMPT 约束段（提示词红线）",
)

MIN_SIGNAL_N = 2          # 小于这个数的信号只记录，不据此提建议
MIN_RECIPE_N = 5          # 单一配方的率，样本少于 5 轮不解读
SLOW_MS = 8000            # 工具 p90 >= 8s 视为慢（单轮预算 10~11 次调用，2 个慢工具就超时）
DEGRADED_RATE = 0.20      # 降级率 >= 20% 值得看
ERROR_RATE = 0.10         # 失败率 >= 10% 值得看
REPEAT_FAIL_N = 3         # 同一工具窗口内失败 >= 3 次

# 「个股提问」的机械判据：问题里有 6 位代码。没代码的问法（"今天市场怎么样"）
# 本来就该走全市场配方，0 工具是另一类问题，不混进来。
_SYMBOL_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")

# 回答正文不落库（只在 result_digest 里留 500 字），所以「缺维度」只能从错误文本侧看
MISS_MARKERS = ("未取到", "取不到", "empty", "暂无", "数据缺失")


def _now_naive() -> datetime:
    """与 `CopilotRun.created_at`（naive utcnow）同尺度比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pct(sorted_vals: list[float], q: float) -> float | None:
    """最近秩百分位（不做插值，样本小的时候插值会造出不存在的数）。"""
    if not sorted_vals:
        return None
    k = int(math.ceil(q / 100.0 * len(sorted_vals))) - 1
    return sorted_vals[max(0, min(len(sorted_vals) - 1, k))]


def _has_symbol(question: str | None) -> bool:
    return bool(_SYMBOL_RE.search(str(question or "")))


def _clip(text: Any, limit: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _matches_miss_marker(text: str | None) -> bool:
    low = str(text or "").lower()
    return any(m.lower() in low for m in MISS_MARKERS)


def collect_signals(db, *, days: int = 14, limit: int = 200) -> dict:
    """只读聚合：把窗口内的轨迹翻成 6 类候选信号，每条带证据。

    读库失败不是崩溃 —— 返回 `available=False` + 原因，让 CLI 照实写「取不到轨迹」，
    绝不补 0 冒充「没有问题」。
    """
    from src.db.models import CopilotRun, CopilotStep

    cutoff = _now_naive() - timedelta(days=max(0, int(days)))
    try:
        runs = (
            db.query(CopilotRun)
            .filter(CopilotRun.created_at >= cutoff)
            .order_by(CopilotRun.created_at.desc())
            .limit(max(1, int(limit)))
            .all()
        )
    except Exception as exc:  # noqa: BLE001 - 只读脚本不能因为没建表就崩
        # 只留首行: SQLAlchemy 的 str(exc) 带整条 SQL + 参数, 打到 cron 日志里全是噪音
        head = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
        logger.warning("[replay] 读 copilot_runs 失败: %s", head)
        return {
            "available": False,
            "reason": f"轨迹表不可读：{type(exc).__name__}",
            "window_days": int(days),
            "limit": int(limit),
            "n_runs": 0,
            "n_steps": 0,
            "signals": {},
        }

    run_ids = [r.run_id for r in runs]
    steps_all: list = []
    if run_ids:
        try:
            steps_all = db.query(CopilotStep).filter(CopilotStep.run_id.in_(run_ids)).all()
        except Exception as exc:  # noqa: BLE001
            head = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
            logger.warning("[replay] 读 copilot_steps 失败: %s", head)
            steps_all = []

    by_run: dict[str, list] = {}
    for s in steps_all:
        by_run.setdefault(str(s.run_id), []).append(s)

    # -- 1. 个股提问但零工具（路由没命中） ----------------------------------
    zero: list[dict] = []
    for r in runs:
        if int(getattr(r, "tool_calls", 0) or 0) == 0 and _has_symbol(getattr(r, "question", None)):
            zero.append({
                "run_id": r.run_id,
                "recipe": getattr(r, "recipe", None),
                "question": _clip(getattr(r, "question", None), 80),
                "tool_calls": 0,
            })

    # -- 2/3. 按配方的降级率 / 失败率 ---------------------------------------
    per_recipe: dict[str, dict[str, int]] = {}
    for r in runs:
        rid = str(getattr(r, "recipe", None) or "unrouted")
        b = per_recipe.setdefault(rid, {"n": 0, "degraded": 0, "error": 0, "tools": 0})
        b["n"] += 1
        st = str(getattr(r, "status", "") or "")
        if st == "degraded":
            b["degraded"] += 1
        elif st == "error":
            b["error"] += 1
        b["tools"] += int(getattr(r, "tool_calls", 0) or 0)

    degraded_rate = {
        rid: {"n": b["n"], "degraded": b["degraded"], "rate": round(b["degraded"] / b["n"], 3)}
        for rid, b in per_recipe.items() if b["n"]
    }
    error_rate = {
        rid: {"n": b["n"], "error": b["error"], "rate": round(b["error"] / b["n"], 3)}
        for rid, b in per_recipe.items() if b["n"]
    }

    # -- 4. 慢工具（p50/p90，仅 n>=5） --------------------------------------
    run_recipe = {str(r.run_id): str(getattr(r, "recipe", None) or "") for r in runs}
    tool_ms: dict[str, list[float]] = {}
    tool_run: dict[str, str] = {}
    tool_recipe: dict[str, dict[str, int]] = {}
    for s in steps_all:
        tool = str(getattr(s, "tool", "") or "")
        rid_now = run_recipe.get(str(getattr(s, "run_id", "")), "")
        if rid_now:
            tc = tool_recipe.setdefault(tool, {})
            tc[rid_now] = tc.get(rid_now, 0) + 1
        ms = getattr(s, "elapsed_ms", None)
        if ms is None:
            continue
        tool_ms.setdefault(tool, []).append(float(ms))
        tool_run.setdefault(tool, str(getattr(s, "run_id", "")))

    def _dominant_recipe(tool: str) -> str:
        tc = tool_recipe.get(tool) or {}
        return max(tc.items(), key=lambda kv: kv[1])[0] if tc else ""

    slow_tools = []
    for tool, vals in tool_ms.items():
        if len(vals) < MIN_RECIPE_N:
            continue
        vals_sorted = sorted(vals)
        slow_tools.append({
            "tool": tool,
            "n": len(vals),
            "p50_ms": int(_pct(vals_sorted, 50) or 0),
            "p90_ms": int(_pct(vals_sorted, 90) or 0),
            "max_ms": int(vals_sorted[-1]),
            "sample_run_id": tool_run.get(tool),
            "recipe": _dominant_recipe(tool),
        })
    slow_tools.sort(key=lambda x: x["p90_ms"], reverse=True)

    # -- 5. 反复失败的工具（窗口内 >=3 次），带错误原文 ----------------------
    fail_by_tool: dict[str, dict[str, Any]] = {}
    for s in steps_all:
        if bool(getattr(s, "ok", True)):
            continue
        tool = str(getattr(s, "tool", "") or "")
        f = fail_by_tool.setdefault(tool, {"n_failed": 0, "n_total": 0, "errors": {}, "runs": []})
        f["n_failed"] += 1
        err = _clip(getattr(s, "error", None) or "(无错误文本)", 160)
        f["errors"][err] = f["errors"].get(err, 0) + 1
        if len(f["runs"]) < 5:
            f["runs"].append(str(getattr(s, "run_id", "")))
    for tool, vals in tool_ms.items():
        if tool in fail_by_tool:
            fail_by_tool[tool]["n_total"] = len(vals)
    repeated_failed = []
    for tool, f in sorted(fail_by_tool.items(), key=lambda kv: kv[1]["n_failed"], reverse=True):
        if f["n_failed"] < REPEAT_FAIL_N:
            continue
        repeated_failed.append({
            "tool": tool,
            "n_failed": f["n_failed"],
            "n_total": f["n_total"],
            "recipe": _dominant_recipe(tool),
            "errors": [{"error": e, "count": c}
                       for e, c in sorted(f["errors"].items(), key=lambda kv: kv[1], reverse=True)[:3]],
            "sample_run_ids": f["runs"][:3],
        })

    # -- 6. 缺维度标记（回答不落库 → 只能从 error / step.error 侧看） --------
    miss: list[dict] = []
    for r in runs:
        hit: list[str] = []
        if _matches_miss_marker(getattr(r, "error", None)):
            hit.append("run.error")
        for s in by_run.get(str(r.run_id), []):
            if _matches_miss_marker(getattr(s, "error", None)):
                hit.append(f"step:{getattr(s, 'tool', '')}")
                break
        if hit:
            miss.append({
                "run_id": r.run_id,
                "recipe": getattr(r, "recipe", None),
                "where": ",".join(hit),
                "error": _clip(getattr(r, "error", None), 100),
            })

    return {
        "available": True,
        "window_days": int(days),
        "limit": int(limit),
        "n_runs": len(runs),
        "n_steps": len(steps_all),
        "signals": {
            "zero_tool_runs": {"n": len(zero), "samples": zero[:10]},
            "degraded_rate_by_recipe": degraded_rate,
            "error_rate_by_recipe": error_rate,
            "slow_tools": slow_tools[:10],
            "repeated_failed_tool": repeated_failed[:10],
            "missing_dimension_markers": {"n": len(miss), "samples": miss[:10]},
        },
        "notes": [
            "只读聚合：未开启任何写事务，未读取任何配置文件",
            "回答正文不落库（result_digest 仅前 500 字），故「缺维度」只能从 error 文本侧统计",
            f"率只在 n>={MIN_RECIPE_N} 时被解读；n<{MIN_SIGNAL_N} 的信号只记录不提建议",
        ],
    }


def _recipe_target(rid: str | None) -> str:
    """配方 id → 文件路径字符串。**只拼字符串，不读文件、不校验存在性。**"""
    return f"src/harness/recipes/{rid}.yaml" if rid else "src/harness/recipes/<未归因>"


def propose(db, *, days: int = 14, signals: dict | None = None) -> dict:
    """把信号翻成建议清单。**不 open / 不 write 任何配置文件**。

    每条：`{id, kind, target, evidence, suggested_change}`
      kind ∈ recipe_when | recipe_tools | skill_text | metric_only
      target 只是「该去看哪个文件」的字符串，本函数不验证文件是否存在、更不修改它。
    """
    sig = signals if signals is not None else collect_signals(db, days=days)
    out: list[dict] = []

    if not sig.get("available"):
        return {
            "available": False,
            "reason": sig.get("reason") or "轨迹不可读",
            "window_days": int(days),
            "n_runs": 0,
            "proposals": [],
            "frozen_ring": list(FROZEN_RING),
            "readonly": True,
            "generated_at": _now_naive().strftime("%Y-%m-%d %H:%M UTC"),
        }

    s = sig["signals"]
    n_runs = sig["n_runs"]

    # 1) 个股提问零工具 → when 没命中（最便宜、最确定的改进点）
    z = s["zero_tool_runs"]
    if z["n"] >= MIN_SIGNAL_N:
        sample = z["samples"][0]
        rid = str(sample.get("recipe") or "")
        target = _recipe_target(rid) if rid else "src/harness/recipes/<路由未命中>"
        out.append({
            "id": "P1",
            "kind": "recipe_when",
            "target": target,
            "evidence": (
                f"{z['n']} 轮个股提问工具调用为 0（n={z['n']}，窗口 {days} 天）；"
                f"例：run {sample['run_id']} recipe={sample.get('recipe') or 'null'} "
                f"tool_calls=0 问题「{sample.get('question')}」"
            ),
            "suggested_change": (
                f"在 {target} 的 when 里补上这类问法的触发词（人工审核后手改）："
                f"该问法在窗口内 {z['n']} 轮里一次工具都没调，说明路由没有认出它是个股诊断"
            ),
        })

    # 2) 降级率偏高 → 大概率是某个工具在拖（配 tools 或换源）
    for rid, m in sorted(s["degraded_rate_by_recipe"].items(), key=lambda kv: kv[1]["rate"], reverse=True):
        if m["n"] < MIN_RECIPE_N or m["rate"] < DEGRADED_RATE:
            continue
        out.append({
            "id": f"P{len(out) + 1}",
            "kind": "recipe_tools",
            "target": f"src/harness/recipes/{rid}.yaml",
            "evidence": (
                f"recipe {rid}: {m['degraded']}/{m['n']} 轮降级（rate={m['rate']:.0%}，n={m['n']}，窗口 {days} 天）"
            ),
            "suggested_change": (
                f"{rid}: 降级率 {m['rate']:.0%}（n={m['n']}）说明有工具部分失败。"
                f"先看该配方 tools 里失败最多的那个工具，能换源就换源、拿不到就删依赖，"
                f"不要靠提示词「尽力拿到」（模型补不出来）"
            ),
        })

    # 3) 失败率偏高 → 同上，优先级更高
    for rid, m in sorted(s["error_rate_by_recipe"].items(), key=lambda kv: kv[1]["rate"], reverse=True):
        if m["n"] < MIN_RECIPE_N or m["rate"] < ERROR_RATE:
            continue
        out.append({
            "id": f"P{len(out) + 1}",
            "kind": "recipe_tools",
            "target": f"src/harness/recipes/{rid}.yaml",
            "evidence": (
                f"recipe {rid}: {m['error']}/{m['n']} 轮整体失败（rate={m['rate']:.0%}，n={m['n']}，窗口 {days} 天）"
            ),
            "suggested_change": (
                f"{rid}: 整体失败率 {m['rate']:.0%}（n={m['n']}）—— 先确认是全工具失败还是上游超时，"
                f"再决定砍工具还是加降级路径；不要用「重试」掩盖确定性失败"
            ),
        })

    # 4) 反复失败的工具（>=3 次）→ 删依赖 / 换源（正是任务里那个例子）
    for f in s["repeated_failed_tool"]:
        err_top = f["errors"][0]["error"] if f["errors"] else "(无错误文本)"
        target = _recipe_target(f.get("recipe"))
        out.append({
            "id": f"P{len(out) + 1}",
            "kind": "recipe_tools",
            "target": target,
            "evidence": (
                f"工具 {f['tool']} 在窗口 {days} 天内失败 {f['n_failed']} 次"
                f"（共调用 {f['n_total']} 次，n={f['n_failed']}）；"
                f"例：run {f['sample_run_ids'][0]} 报「{err_top}」"
            ),
            "suggested_change": (
                f"删掉对 {f['tool']} 的依赖：它在窗口内失败 {f['n_failed']} 次，"
                f"同一个错误反复出现 —— 确定性失败不会靠重试好转，"
                f"要么换数据源，要么把该维度如实写成「未取到」"
            ),
        })

    # 5) 慢工具 → 从管线里挪走 / 降级为可选
    for t in s["slow_tools"]:
        if t["p90_ms"] < SLOW_MS:
            continue
        out.append({
            "id": f"P{len(out) + 1}",
            "kind": "recipe_tools",
            "target": _recipe_target(t.get("recipe")),
            "evidence": (
                f"工具 {t['tool']} p50={t['p50_ms']}ms p90={t['p90_ms']}ms max={t['max_ms']}ms"
                f"（n={t['n']} 次调用）；例：run {t['sample_run_id']}"
            ),
            "suggested_change": (
                f"{t['tool']}: p90 {t['p90_ms']}ms（n={t['n']}）已占掉单轮预算的一大块，"
                f"建议从确定性管线挪到「用户追问再取」，或换更快的源"
            ),
        })

    # 6) 缺维度标记 → 技能手册补条件分支（不是提示词约束段！那属于冻结环）
    m = s["missing_dimension_markers"]
    if m["n"] >= MIN_SIGNAL_N:
        sample = m["samples"][0]
        rid = str(sample.get("recipe") or "")
        target = f"src/harness/skills/{rid}.md" if rid else "src/harness/skills/（对应配方手册）"
        out.append({
            "id": f"P{len(out) + 1}",
            "kind": "skill_text",
            "target": target,
            "evidence": (
                f"{m['n']} 轮出现「未取到/empty」类标记（n={m['n']}）；"
                f"例：run {sample['run_id']} 在 {sample['where']} 命中「{sample['error'] or '—'}」"
            ),
            "suggested_change": (
                f"{target}: 给「该维度取不到时怎么写」补一条条件分支（必须绑 regime 标签），"
                f"让缺数据时如实降级；注意 SYSTEM_PROMPT 的约束段属于冻结环，不许动"
            ),
        })

    # 7) 有轨迹但一条建议都出不来 → 只记录指标，明确说样本不足
    if not out:
        out.append({
            "id": "P1",
            "kind": "metric_only",
            "target": "（无）",
            "evidence": (
                f"窗口 {days} 天共 n={n_runs} 轮，无任何信号达到建议门槛"
                f"（零工具需 n>={MIN_SIGNAL_N}、配方率需 n>={MIN_RECIPE_N}、同工具失败需 n>={REPEAT_FAIL_N}）"
            ),
            "suggested_change": "不改任何东西：样本不足，继续积累轨迹；本报告只留指标基线",
        })

    return {
        "available": True,
        "window_days": int(days),
        "n_runs": n_runs,
        "n_steps": sig.get("n_steps", 0),
        "proposals": out,
        "frozen_ring": list(FROZEN_RING),
        "readonly": True,
        "generated_at": _now_naive().strftime("%Y-%m-%d %H:%M UTC"),
    }


def render(proposal: dict) -> str:
    """把 propose() 的输出渲染成 Markdown。每一行都带证据与 n。"""
    lines: list[str] = ["# 离线复盘建议（只读）", ""]
    n_runs = int(proposal.get("n_runs") or 0)
    lines.append(f"- 窗口：近 {proposal.get('window_days', 0)} 天｜样本 n={n_runs} 轮")
    lines.append(f"- 生成时间：{proposal.get('generated_at') or '—'}")

    if not proposal.get("available"):
        lines.append(f"- 轨迹不可读：{proposal.get('reason')}（n=0）")
        lines.append("- 结论：样本不足，不生成任何建议，不猜数。")
        lines += ["", "---", "", "只读：未修改任何配方/提示词/配置"]
        return "\n".join(lines) + "\n"

    if n_runs < MIN_SIGNAL_N:
        lines.append(f"- 结论：**样本不足**（n={n_runs} < {MIN_SIGNAL_N}），本报告不作任何结论。")

    lines += ["", "## 冻结环（本轮所有建议都不指向这里）", ""]
    for item in proposal.get("frozen_ring") or FROZEN_RING:
        lines.append(f"- {item}")

    props = proposal.get("proposals") or []
    lines += ["", f"## 建议清单（{len(props)} 条）", ""]
    for p in props:
        lines.append(f"### {p.get('id')} · [{p.get('kind')}] {p.get('target')}")
        lines.append(f"- 证据：{p.get('evidence')}")
        lines.append(f"- 建议：{p.get('suggested_change')}")
        lines.append(f"- 状态：仅提案，需人工审核后手动改（n={n_runs} 轮窗口内证据）")
        lines.append("")

    if n_runs < MIN_SIGNAL_N:
        lines.append(f"- 注：n={n_runs} 轮不足以支撑任何改动，以上条目只作为下一窗口的观察项。")
        lines.append("")

    lines += ["---", "", "只读：未修改任何配方/提示词/配置"]
    return "\n".join(lines) + "\n"
