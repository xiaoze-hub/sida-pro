#!/usr/bin/env python3
"""离线复盘 CLI（受控 RSI 证据层 S0，2026-10-23）。

读 `copilot_runs` / `copilot_steps`，产出「哪份配方的哪条规则该改」的建议清单。
**只读**：不写任何配方 / 提示词 / 配置，不联网，不调 LLM。

用法：
  python scripts/rsi_offline_review.py                        # 近 14 天 → docs/research/rsi-offline-review-YYYYMMDD.md
  python scripts/rsi_offline_review.py --days 30 --limit 500
  python scripts/rsi_offline_review.py --json --out /tmp/rsi.json
  python scripts/rsi_offline_review.py --days 0 --out /tmp/smoke.md   # 空窗口 → 报告写「样本不足」

退出码：0 = 报告已写出（**哪怕样本不足也是 0**，样本不足是正常状态，不是错误）；
        2 = 报告写不出来（磁盘/权限/路径）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

READONLY_BANNER = "只读：未修改任何配方/提示词/配置"


def _default_out() -> Path:
    return REPO_ROOT / "docs" / "research" / f"rsi-offline-review-{datetime.now().strftime('%Y%m%d')}.md"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="离线复盘：只提议，不改配置")
    ap.add_argument("--days", type=int, default=14, help="回看窗口天数（默认 14）")
    ap.add_argument("--limit", type=int, default=200, help="最多扫多少轮（默认 200）")
    ap.add_argument("--out", default=None, help="输出路径（默认 docs/research/rsi-offline-review-<YYYYMMDD>.md）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    args = ap.parse_args(argv)

    from src.core.harness_replay import collect_signals, propose, render
    from src.db.session import SessionLocal

    db = None
    try:
        db = SessionLocal()
        # 只发 SELECT：本脚本没有任何 add/commit/execute(DDL) 调用。
        signals = collect_signals(db, days=args.days, limit=args.limit)
        prop = propose(db, days=args.days, signals=signals)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响已算出的报告
                pass

    if args.json:
        payload = {"signals": signals, "proposal": prop, "readonly": True}
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        body = render(prop)

    out_path = Path(args.out) if args.out else _default_out()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"报告写入失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(READONLY_BANNER)
        return 2

    n_runs = int(prop.get("n_runs") or 0)
    n_props = len(prop.get("proposals") or [])
    print(f"窗口: 近 {args.days} 天 | 样本 n={n_runs} 轮 | 建议 {n_props} 条")
    if not prop.get("available"):
        print(f"轨迹不可读: {prop.get('reason')} —— 报告照实写「样本不足」，不编数")
    print(f"报告: {out_path}")
    print(READONLY_BANNER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
