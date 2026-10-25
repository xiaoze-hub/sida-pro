#!/usr/bin/env python3
"""D3：6 个内置智能体 × 3 个真实问题，跑生产参谋接口，采集「工具调用数 / 耗时 / 结构合规」。

判据（不是"答案好不好"，而是"配方有没有真的把动作收敛住"）：
  - tools：stage 事件数 = 实际工具调用次数，应显著低于默认配方
  - structure：是否按配方的输出段落作答（四段标记齐全）
  - symbol：涉及个股时，标的必须与提问一致

用法:
  SIDA_ADMIN_PW=xxx python3 scripts/harness_d3_probe.py --base https://SIDA.hengsheng-elec.com
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from pathlib import Path

CASES = {
    "premarket_strategist": [
        ("今天开盘前有什么值得盯的？", None),
        ("隔夜有什么事件可能影响 A 股？", None),
        ("我的自选股今天优先级怎么排？", None),
    ],
    "auction_hunter": [
        ("今天集合竞价有什么异常？", None),
        ("9:25 谁在爆量抢筹？", None),
        ("竞价撤单率高的票有哪些？", None),
    ],
    "intraday_watcher": [
        ("我的持仓现在什么状态？", None),
        ("有没有触发防守位的？", None),
        ("主力资金今天在流入还是流出？", None),
    ],
    "postmarket_reviewer": [
        ("今天涨停梯队怎么样？", None),
        ("今天情绪温度多少？", None),
        ("今天的主线题材是什么？", None),
    ],
    "risk_judge": [
        ("我的持仓风险有多大？", None),
        ("有没有已经破位的票？", None),
        ("现在的仓位结构有什么风险？", None),
    ],
    "chip_analyst": [
        ("600111 的筹码结构怎么样？", "600111"),
        ("600111 主力今天在拿货还是派发？", "600111"),
        ("600111 底仓成本和获利盘多少？", "600111"),
    ],
}

BASELINE = [
    ("postmarket_reviewer", "今天涨停梯队怎么样？"),
    ("risk_judge", "我的持仓风险有多大？"),
    ("chip_analyst", "600111 的筹码结构怎么样？"),
]


def _post(path: str, payload: dict, token: str, timeout: int = 180):
    req = urllib.request.Request(
        path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _expected_sections(agent: str | None) -> list[str]:
    if not agent:
        return ["结论", "依据"]
    try:
        from src.harness.recipes import get_recipe

        r = get_recipe(agent)
        return list(r.output_sections) if r else []
    except Exception:
        return []


def _structure_ok(text: str, agent: str | None) -> bool:
    want = _expected_sections(agent)
    if not want:
        return "【结论】" in text
    hit = sum(1 for w in want if w in text)
    return hit >= max(2, len(want) // 2)


def _latest_run_id(base: str, token: str, cid: int) -> str | None:
    """从会话里取最近一轮的 run_id（取不到返回 None，不抛）。"""
    try:
        req = urllib.request.Request(
            f"{base}/api/copilot/runs?conversation_id={cid}&limit=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        return (rows[0] or {}).get("run_id") if rows else None
    except Exception:  # noqa: BLE001
        return None


def run_case(base: str, token: str, agent: str | None, question: str, symbol: str | None) -> dict:
    conv = {"initial_context": "harness d3 自动评测"}
    if symbol:
        conv["stock_symbol"] = symbol
        conv["stock_market"] = "SH" if symbol[0] == "6" else "SZ"
    with _post(f"{base}/api/chat/conversations", conv, token) as r:
        cid = json.loads(r.read())["data"]["id"]

    body: dict = {"content": question}
    if agent:
        body["mentions"] = [{"type": "agent", "value": agent}]
    if symbol:
        body["mentions"] = (body.get("mentions") or []) + [{"type": "stock", "value": symbol, "label": symbol}]

    stages: list[str] = []
    text = ""
    kind = ""
    t0 = time.time()
    req = urllib.request.Request(
        f"{base}/api/chat/conversations/{cid}/messages/stream",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if kind == "stage":
                    msg = obj.get("message", "")
                    # 「正在准备上下文…」是预置提示，不是工具调用；算进去会让每次多 1
                    if msg and "准备上下文" not in msg:
                        stages.append(msg)
                elif kind == "delta":
                    text += obj.get("content", "")
                elif kind == "done":
                    text = text or obj.get("content", "")
    return {
        "agent": agent or "default",
        "question": question,
        "symbol": symbol,
        "seconds": round(time.time() - t0, 1),
        "tool_calls": len(stages),
        "tools": stages[:12],
        "chars": len(text),
        # 结构合规按"配方自己的输出段落"判，而不是写死【依据】——premarket 用「隔夜事件」、
        # postmarket 用「涨停结构」，用固定标记会把正确输出判成不合格（首轮评测就踩了）。
        "structured": _structure_ok(text, agent),
        # acceptance 判据要看全文（缺哪一段），只留 head 判不了
        "text": text,
        "head": text[:160].replace("\n", " "),
        # best-effort：拿到 conversation 的 run_id 才能做 fitness 对账；
        # 取不到就留 None（评估时如实报"没拿到 run_id"，不假装算过）
        "run_id": _latest_run_id(base, token, cid),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://SIDA.hengsheng-elec.com")
    ap.add_argument("--out", default="docs/research/harness_d3")
    ap.add_argument("--only", default="")
    ap.add_argument("--compare", default="", help="候选智能体 id：同批问题跑 默认配方 vs 它，出对照结论")
    ap.add_argument("--compare-base", default="", help="对照组智能体 id（默认空 = 默认配方）")
    ap.add_argument("--max-cost-increase", type=float, default=0.20, help="成本涨幅上限")
    ap.add_argument("--with-fitness", action="store_true",
                    help="同时跑关键位真值评分（需本地库；不传则 fitness 判据如实报不可用）")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    pw = os.environ.get("SIDA_ADMIN_PW", "")

    with _post(f"{base}/api/auth/login", {"username": "admin", "password": pw}, "") as r:
        token = json.loads(r.read())["data"]["token"]

    # P0-0.3 对照模式：跑完直接给 keep/rollback，不写常规报告
    if args.compare:
        cand_agent = args.compare
        cases = CASES.get(cand_agent) or BASELINE
        db = None
        if getattr(args, "with_fitness", False):
            try:
                from src.db.session import SessionLocal
                db = SessionLocal()
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 未接本地库，fitness 判据置为不可用：{type(e).__name__}: {e}")
        try:
            result = compare(base, token, cases, cand_agent, args.compare_base or None,
                             db=db, max_cost_increase=args.max_cost_increase)
        finally:
            if db is not None:
                db.close()
        result["decision"] = decide(result["baseline"], result["candidate"],
                                    max_cost_increase=args.max_cost_increase)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        fp = out / f"compare-{cand_agent.replace('/', '_')}.json"
        fp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("saved ->", fp)
        return 0 if result["decision"]["verdict"] == "keep" else 2

    rows: list[dict] = []
    only = {x for x in args.only.split(",") if x}
    for agent, cases in CASES.items():
        if only and agent not in only:
            continue
        for q, sym in cases:
            try:
                row = run_case(base, token, agent, q, sym)
            except Exception as e:
                row = {"agent": agent, "question": q, "error": f"{type(e).__name__}: {e}"[:120]}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False)[:220], flush=True)

    for agent, q in BASELINE:
        if only and agent not in only:
            continue
        try:
            row = run_case(base, token, None, q, "600111" if "600111" in q else None)
            row["baseline_for"] = agent
        except Exception as e:
            row = {"agent": "default", "question": q, "error": str(e)[:120], "baseline_for": agent}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False)[:220], flush=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "runs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# Harness D3 评测（内置智能体 vs 默认配方）", "",
             "| 智能体 | 问题 | 工具调用 | 耗时 | 结构合规 | 字数 |", "|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['agent']} | {r['question'][:18]} | ERR | - | - | {r['error'][:40]} |")
            continue
        lines.append(f"| {r['agent']} | {r['question'][:18]} | {r['tool_calls']} | {r['seconds']}s | "
                     f"{'✅' if r['structured'] else '—'} | {r['chars']} |")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("saved ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ── P0-0.3 对照执行器（2026-10-25）────────────────────────────────────────
# 目的：把「改一句提示词有没有用」变成可比的数据，而不是靠感觉。
# 纪律：**同 3 问、同模型、同环境**；原配方必须一起跑（不跑就没有对照，数据不可用）；
#      任一维度退化即判 rollback；不看 LLM 评审（主观分无法复核）。


def summarize(rows: list[dict]) -> dict:
    """把一批 case 结果压成可对比的四项指标。

    cost 用 `tool_calls + chars/1000` 而不是 token：探针拿不到 usage 时，
    拿工具调用次数与输出长度做代理是**可复核**的；缺 usage 就编一个 token 数才是假数据。
    """
    ok = [r for r in rows if not r.get("error")]
    err = len(rows) - len(ok)
    n = len(ok)
    structured = sum(1 for r in ok if r.get("structured"))
    tool_calls = sum(int(r.get("tool_calls") or 0) for r in ok)
    chars = sum(int(r.get("chars") or 0) for r in ok)
    seconds = round(sum(float(r.get("seconds") or 0) for r in ok), 1)
    return {
        "n": n,
        "errors": err,
        "pass_rate": round(structured / n, 4) if n else 0.0,
        "structure_n": structured,
        "tool_calls": tool_calls,
        "avg_tool_calls": round(tool_calls / n, 2) if n else 0.0,
        "chars": chars,
        "cost": round(tool_calls + chars / 1000.0, 2),
        "seconds": seconds,
    }


def decide(base: dict, cand: dict, *, max_cost_increase: float = 0.20) -> dict:
    """保留还是回滚。**任何一维退化都不接受**，成本涨幅另有上限。"""
    reasons: list[str] = []

    if cand["errors"] > base["errors"]:
        reasons.append(f"失败数增加 {base['errors']}→{cand['errors']}")

    if cand["pass_rate"] < base["pass_rate"]:
        reasons.append(
            f"结构合规率下降 {base['pass_rate']:.0%}→{cand['pass_rate']:.0%}"
        )

    if base["cost"] > 0:
        rise = (cand["cost"] - base["cost"]) / base["cost"]
        if rise > max_cost_increase:
            reasons.append(f"成本上升 {rise:.0%}（上限 {max_cost_increase:.0%}）")
    elif cand["cost"] > 0:
        reasons.append(f"基线成本为 0 而候选为 {cand['cost']}")

    return {
        "verdict": "rollback" if reasons else "keep",
        "reasons": reasons,
        "delta_pass_rate": round(cand["pass_rate"] - base["pass_rate"], 4),
        "delta_cost": round(cand["cost"] - base["cost"], 2),
    }


# ── P0-0.4 客观判据三条（2026-10-25）────────────────────────────────────
# 为什么要有三条而不是一条：
#   `pass_rate`（结构合规）只说明"该有的段落写了"，说明不了"写的东西对不对"；
#   `cost` 只管花销；唯一的**真值**来源是关键位能不能被后续行情兑现（fitness）。
#   少了任一维，"改一句提示词"都可能在一维变好、另一维悄悄变差而看不出来。
#
# 三条判据的求值口径（都必须是机检，不看 LLM 评审 —— 主观分无法复核）：
#   1. acceptance：配方 `output_sections` 全齐 + 不得整段省略（结构化机检）；
#   2. fitness：`fitness_keypoints.score_run` 的命中情况（**真值**，需要本地库 + 行情走完）；
#   3. cost：`tool_calls + chars/1000` 代理（拿不到 usage 就不编 token）。
#
# fitness 的 `available=False` 是常态而不是错误（行情没走完 / 该轮没写关键位），
# 这种情况**不计入分母**，但要如实报出覆盖率 —— 覆盖率太低时"命中率"没有意义。


def _expected_section_list(agent: str | None) -> list[str]:
    """配方声明的输出段落（与 `_structure_ok` 同源，避免两套判定漂移）。"""
    want = _expected_sections(agent)
    return list(want or [])


def acceptance_metrics(rows: list[dict], agent: str | None) -> dict:
    """判据一：结构合规（配方段落全齐 + 不整段省略）。

    与 `summarize()["pass_rate"]` 的区别：这里给的是**可解释的明细**
    （缺了哪几段、几成合规），而不是一个光秃秃的比例。
    """
    ok = [r for r in rows if not r.get("error")]
    n = len(ok)
    want = _expected_section_list(agent)
    full = 0
    missing: dict[str, int] = {}
    for r in ok:
        text = str(r.get("text") or "")
        if not want:
            hit = 1 if "【结论】" in text else 0
            full += 1 if hit else 0
            continue
        miss = [w for w in want if w not in text]
        if not miss:
            full += 1
        for w in miss:
            missing[w] = missing.get(w, 0) + 1
    return {
        "available": n > 0,
        "n": n,
        "sections_expected": want,
        "full_sections": full,
        "acceptance_rate": round(full / n, 4) if n else None,
        "missing_top": sorted(missing.items(), key=lambda kv: -kv[1])[:3],
        "reason": "" if n else "没有成功样本，无法评估结构合规",
    }


def fitness_metrics(run_ids: list[str], *, db=None, horizon_td: int = 3) -> dict:
    """判据二：关键位真值评分（复用 `fitness_keypoints.score_run`）。

    **没有本地库就明说 `available=False`**，不要用结构合规去冒充"关键位是对的"。
    `score_run` 的 `available=False`（行情没走完 / 没写关键位）不计入分母，
    但覆盖率必须一起报 —— 3 个样本里 1 个能对账，命中率 100% 是没有意义的。
    """
    if db is None:
        return {"available": False, "reason": "未接本地库，无法对账关键位（fitness 需要读 copilot_runs/steps 与日K）",
                "n": 0, "scored": 0, "coverage": None, "hit_rate": None, "details": []}
    ids = [str(x) for x in run_ids if x]
    if not ids:
        return {"available": False, "reason": "本轮没有拿到 run_id，无法对账",
                "n": 0, "scored": 0, "coverage": None, "hit_rate": None, "details": []}

    from src.core.fitness_keypoints import score_run

    details = []
    scored = 0
    hits = 0
    for rid in ids:
        try:
            r = score_run(db, rid, horizon_td=horizon_td)
        except Exception as e:  # noqa: BLE001
            details.append({"run_id": rid, "available": False, "reason": f"{type(e).__name__}: {e}"[:120]})
            continue
        avail = bool(r.get("available"))
        if avail:
            scored += 1
            hit = int(bool(r.get("hit_resist"))) + int(bool(r.get("hit_support")))
            hits += hit
        details.append({
            "run_id": rid, "available": avail,
            "hit_resist": r.get("hit_resist"), "hit_support": r.get("hit_support"),
            "reason": r.get("reason") or "",
        })
    n = len(ids)
    return {
        "available": scored > 0,
        "reason": "" if scored else "样本的行情都还没走完（T+N 未到）或没写关键位，无法对账",
        "n": n, "scored": scored,
        "coverage": round(scored / n, 4) if n else None,
        # hit_rate 的分母是**双向**次数（支撑+阻力各算一次），不是样本数
        "hit_rate": round(hits / (scored * 2), 4) if scored else None,
        "details": details,
    }


def decide3(base: dict, cand: dict, *, max_cost_increase: float = 0.20) -> dict:
    """三条判据合议：**任一维度退化即 rollback**。

    与 `decide()` 的区别：`decide()` 只看 pass_rate + cost；这里把 fitness 也作为
    硬判据，并且只在**两侧都有值**时比较（`available=False` 不参与比较，
    否则"这轮行情没走完"会被误判成退化）。
    """
    reasons: list[str] = []

    if cand.get("errors", 0) > base.get("errors", 0):
        reasons.append(f"失败数增加 {base.get('errors')}→{cand.get('errors')}")

    ba, ca = base.get("acceptance") or {}, cand.get("acceptance") or {}
    if ba.get("available") and ca.get("available"):
        br, cr = ba.get("acceptance_rate"), ca.get("acceptance_rate")
        if br is not None and cr is not None and cr < br:
            miss = "、".join(w for w, _ in (ca.get("missing_top") or [])[:2])
            reasons.append(f"结构合规率下降 {br:.0%}→{cr:.0%}" + (f"（候选缺：{miss}）" if miss else ""))

    bf, cf = base.get("fitness") or {}, cand.get("fitness") or {}
    if bf.get("available") and cf.get("available"):
        br, cr = bf.get("hit_rate"), cf.get("hit_rate")
        if br is not None and cr is not None and cr < br:
            reasons.append(f"关键位命中率下降 {br:.0%}→{cr:.0%}")

    bc, cc = base.get("cost"), cand.get("cost")
    if isinstance(bc, (int, float)) and isinstance(cc, (int, float)):
        if bc > 0:
            rise = (cc - bc) / bc
            if rise > max_cost_increase:
                reasons.append(f"成本上升 {rise:.0%}（上限 {max_cost_increase:.0%}）")
        elif cc > 0:
            reasons.append(f"基线成本为 0 而候选为 {cc}")

    return {
        "verdict": "rollback" if reasons else "keep",
        "reasons": reasons,
        "criteria": {
            "acceptance": {"base": ba.get("acceptance_rate"), "cand": ca.get("acceptance_rate")},
            "fitness": {"base": bf.get("hit_rate"), "cand": cf.get("hit_rate"),
                        "coverage": {"base": bf.get("coverage"), "cand": cf.get("coverage")}},
            "cost": {"base": bc, "cand": cc},
        },
    }


def compare(base_url: str, token: str, cases: list[tuple[str, str | None]],
            candidate_agent: str | None, base_agent: str | None = None,
            *, db=None, max_cost_increase: float = 0.20) -> dict:
    """同批问题跑「原配方 vs 候选」，返回可直接入库的对照结果。"""
    def _run(agent: str | None) -> list[dict]:
        out = []
        for q, sym in cases:
            try:
                row = run_case(base_url, token, agent, q, sym)
            except Exception as e:  # noqa: BLE001
                row = {"agent": agent or "default", "question": q,
                       "error": f"{type(e).__name__}: {e}"[:120]}
            out.append(row)
        return out

    base_rows = _run(base_agent)
    cand_rows = _run(candidate_agent)
    base = summarize(base_rows)
    cand = summarize(cand_rows)
    # P0-0.4：三条判据（结构合规 / 关键位真值 / 成本），任一退化即 rollback
    base["acceptance"] = acceptance_metrics(base_rows, base_agent)
    cand["acceptance"] = acceptance_metrics(cand_rows, candidate_agent)
    base["fitness"] = fitness_metrics([r.get("run_id") for r in base_rows], db=db)
    cand["fitness"] = fitness_metrics([r.get("run_id") for r in cand_rows], db=db)
    return {
        "baseline_agent": base_agent or "default",
        "candidate_agent": candidate_agent or "default",
        "questions": [q for q, _ in cases],
        "baseline": base,
        "candidate": cand,
        # decide3 是合议版；decide 保留给不关心 fitness 的旧调用
        "decision": decide3(base, cand, max_cost_increase=max_cost_increase),
        "decision_legacy": decide(base, cand, max_cost_increase=max_cost_increase),
    }
