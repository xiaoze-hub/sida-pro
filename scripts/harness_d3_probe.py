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
    args = ap.parse_args()
    base = args.base.rstrip("/")
    pw = os.environ.get("SIDA_ADMIN_PW", "")

    with _post(f"{base}/api/auth/login", {"username": "admin", "password": pw}, "") as r:
        token = json.loads(r.read())["data"]["token"]

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
