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
        "head": text[:160].replace("\n", " "),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://SIDA.hengsheng-elec.com")
    ap.add_argument("--out", default="docs/research/harness_d3")
    ap.add_argument("--only", default="")
    ap.add_argument("--compare", default="", help="候选智能体 id：同批问题跑 默认配方 vs 它，出对照结论")
    ap.add_argument("--compare-base", default="", help="对照组智能体 id（默认空 = 默认配方）")
    ap.add_argument("--max-cost-increase", type=float, default=0.20, help="成本涨幅上限")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    pw = os.environ.get("SIDA_ADMIN_PW", "")

    with _post(f"{base}/api/auth/login", {"username": "admin", "password": pw}, "") as r:
        token = json.loads(r.read())["data"]["token"]

    # P0-0.3 对照模式：跑完直接给 keep/rollback，不写常规报告
    if args.compare:
        cand_agent = args.compare
        cases = CASES.get(cand_agent) or BASELINE
        result = compare(base, token, cases, cand_agent, args.compare_base or None)
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


def compare(base_url: str, token: str, cases: list[tuple[str, str | None]],
            candidate_agent: str | None, base_agent: str | None = None) -> dict:
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
    return {
        "baseline_agent": base_agent or "default",
        "candidate_agent": candidate_agent or "default",
        "questions": [q for q, _ in cases],
        "baseline": base,
        "candidate": cand,
        "decision": decide(base, cand),
    }
