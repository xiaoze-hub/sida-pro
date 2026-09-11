"""GS 信号校准复现脚本(2026-09-11, 对齐 docs/GS校准报告_原版视频口径.md)。

对同一份样本(默认上证指数近 500 根日线)量化三档口径:
  ① raw        — 裸交叉信号
  ② merged     — +3 天内反向成对丢弃(merge_whipsaw, 已上线)
  ③ gated      — +ACT(机构活跃度)≥3 门控(本次新增验证)
指标: 信号数 / 抖动对数(相邻反向且间隔≤3天) / G 位置分位(21 日窗, 越接近 0 越靠谷底)。

用法(容器或本机):
  python scripts/gs_calibration.py [--symbol 999999.SH] [--bars 500]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date as _date

sys.path.insert(0, ".")


def _fetch_bars(symbol: str, count: int) -> list[dict]:
    """优先 TDX(容器内可达), 失败回落腾讯。"""
    try:
        from marketdata.vendors.tq import tq_rpc

        code = symbol if "." in symbol else f"{symbol}.SH"
        v = tq_rpc(
            "get_market_data",
            {"stock_list": [code], "period": "1d", "count": count, "dividend_type": "none"},
            timeout=60,
        )
        rows = (v or {}).get(code) or {}
        ds, op, cl = rows.get("Date") or [], rows.get("Open") or [], rows.get("Close") or []
        hi, lo, vol = rows.get("High") or [], rows.get("Low") or [], rows.get("Volume") or []
        n = min(len(ds), len(cl))
        if n >= 60:
            return [
                {
                    "date": str(ds[i]),
                    "open": float(op[i]),
                    "close": float(cl[i]),
                    "high": float(hi[i]),
                    "low": float(lo[i]),
                    "volume": float(vol[i]),
                }
                for i in range(n)
            ]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] TDX 取数失败, 回落腾讯: {e}")
    import httpx

    code = symbol.lower().replace(".sh", "").replace(".sz", "")
    if not code.startswith(("sh", "sz")):
        code = "sh" + code
    r = httpx.get(
        f"https://ifzq.gtimg.cn/appstock/app/newfqkline/get?param={code},day,,,{count},qfq",
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    ).json()
    data = r.get("data", {}).get(code, {})
    rows = data.get("qfqday") or data.get("day") or []
    return [
        {"date": str(x[0]), "open": float(x[1]), "close": float(x[2]), "high": float(x[3]), "low": float(x[4]), "volume": float(x[5])}
        for x in rows
    ]


def _norm(d) -> str:
    """日期归一到 YYYYMMDD(信号 ISO 与 TDX 取样格式不一致, 曾致门控/分位空转)。"""
    d = str(d or "").strip().replace("-", "")
    return d[:8] if len(d) >= 8 else d


def _whipsaw_pairs(signals: list[dict], window: int = 3) -> int:
    """相邻反向且间隔 ≤window 天的对数(抖动计数)。"""
    pairs = 0
    for a, b in zip(signals, signals[1:]):
        if a.get("side") == b.get("side"):
            continue
        try:
            d1 = _date.fromisoformat(_norm(a.get("date")))
            d2 = _date.fromisoformat(_norm(b.get("date")))
        except (ValueError, TypeError):
            continue
        if 0 <= (d2 - d1).days <= window:
            pairs += 1
    return pairs


def _pos_quantile(signals: list[dict], bars: list[dict], side: str, window: int = 21) -> float | None:
    """信号在 ±window 窗口内的价格分位(0=谷底, 1=峰顶)。"""
    closes = [b["close"] for b in bars]
    dates = [_norm(b.get("date")) for b in bars]
    qs = []
    for s in signals:
        if s.get("side") != side:
            continue
        try:
            i = dates.index(_norm(s.get("date")))
        except ValueError:
            continue
        lo, hi = max(0, i - window), min(len(closes) - 1, i + window)
        seg = closes[lo : hi + 1]
        if len(seg) < 5 or max(seg) == min(seg):
            continue
        qs.append((closes[i] - min(seg)) / (max(seg) - min(seg)))
    return round(statistics.median(qs), 3) if qs else None


def _gated(signals: list[dict], bars: list[dict], min_activity: float = 3.0) -> list[dict]:
    """ACT 门控: 丢弃同日机构活跃度 < min_activity 的信号(缺数据不门控)。"""
    from src.core.ai_activity import eval_activity

    out = []
    for s in signals:
        try:
            i = next(idx for idx, b in enumerate(bars) if _norm(b.get("date")) == _norm(s.get("date")))
        except StopIteration:
            out.append(s)
            continue
        act = (eval_activity(bars[: i + 1]) or {}).get("activity")
        if act is None or act >= min_activity:
            out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="999999.SH", help="标的(TDX 代码, 默认上证指数)")
    ap.add_argument("--symbols", default="", help="多标的逗号分隔(覆盖 --symbol)")
    ap.add_argument("--bars", type=int, default=500)
    args = ap.parse_args()

    from src.core.gs_strategy import compute_gs_signals

    syms = [x.strip() for x in args.symbols.split(",") if x.strip()] or [args.symbol]
    for sym in syms:
        bars = _fetch_bars(sym, args.bars)
        if len(bars) < 60:
            print(f"[fail] {sym} 样本不足: {len(bars)} 根")
            continue
        raw = compute_gs_signals(bars, denoise=False)
        merged = compute_gs_signals(bars, denoise=True)
        gated = _gated(merged, bars, min_activity=3.0)
        print(f"样本: {sym} {len(bars)} 根({bars[0]['date']} → {bars[-1]['date']})")
        for name, sig in (("raw", raw), ("merged", merged), ("merged+gated", gated)):
            n_g = sum(1 for s in sig if s["side"] == "G")
            n_s = sum(1 for s in sig if s["side"] == "S")
            print(
                f"  {name:<12} 信号 {len(sig):>3} (G {n_g} / S {n_s}) | "
                f"抖动对 {_whipsaw_pairs(sig):>2} | G位置分位 {_pos_quantile(sig, bars, 'G')} | S位置分位 {_pos_quantile(sig, bars, 'S')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
