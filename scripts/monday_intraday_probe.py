"""周一盘中实测探针(批次A/B/C 字段口径验证, 2026-09-06 28号)。

验证清单(对应 docs/innov-dev-plan.md 风险项 + 发版邮件承诺):
1. BCancel/SCancel 口径: 间隔 60s 两次采样 fetch_tq_l2 累计字段,
   差分 ≥0 且随交易增长 → 差分口径成立; 若数值恒定 → 快照值, 需改口径
2. FCAmo 语义: more_info 原始字段 vs 涨停池封单额(wudao) 对照
3. 新浪期货连通性: fetch_snapshot + fetch_daily_close (无需盘中, 即时可测)
4. 封单成色实跑: seal_quality.compute_from_series 消费当日真实采样

用法: python scripts/monday_intraday_probe.py [--symbol 002361] [--wait 60]
输出: 人读摘要 + JSON(probe_results 追加到 data/probe_results.json)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

_CST = ZoneInfo("Asia/Shanghai")
_RESULTS_FILE = Path(__file__).parent.parent / "data" / "probe_results.json"


def probe_tq_cumulative(symbol: str, wait_s: int) -> dict:
    """1. BCancel/SCancel 差分口径验证(两次采样, 间隔 wait_s)。

    直连 TQ RPC(绕过 marketdata Engine): Windows 本地库未配 vendor 优先级时
    Engine 会返回空(2026-09-06 实测), 口径验证必须打真实链路。
    """
    sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "marketdata" / "src"))
    from marketdata.symbol import Symbol as _Sym
    from marketdata.vendors.tq import to_tq_code as _tqc
    from marketdata.vendors.tq import tq_rpc

    tqc = _tqc(_Sym.parse(symbol, "CN"))
    if not tqc:
        return {"verdict": "FAIL", "note": "代码无法转 TQ 格式", "deltas": {}}

    def _once() -> dict:
        raw = tq_rpc("get_more_info", {"stock_code": tqc})
        if not isinstance(raw, dict):
            return {}
        return {
            "cancel_buy": raw.get("BCancel"),
            "cancel_sell": raw.get("SCancel"),
            "l2_tick_num": raw.get("L2TicNum"),
            "l2_order_num": raw.get("L2OrderNum"),
            "total_buy_vol": raw.get("TotalBVol"),
            "total_sell_vol": raw.get("TotalSVol"),
        }

    a = _once()
    if not a:
        return {"verdict": "FAIL", "note": "首次采样为空(TQ 网关不可达?)", "deltas": {}}
    time.sleep(wait_s)
    b = _once()
    if not b:
        return {"verdict": "FAIL", "note": "第二次采样为空", "deltas": {}}

    deltas = {}
    for k in a:
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            deltas[k] = None
            continue
        deltas[k] = {"t0": va, "t1": vb, "delta": round(float(vb) - float(va), 3)}

    growing = [k for k, d in deltas.items() if d and d["delta"] and d["delta"] > 0]
    flat = [k for k, d in deltas.items() if d and d["delta"] == 0]
    negative = [k for k, d in deltas.items() if d and d["delta"] is not None and d["delta"] < 0]
    if negative:
        verdict, note = "FAIL", "出现负差分=数据重置或口径异常, 检查"
    elif growing:
        verdict, note = "PASS", f"累计口径成立({len(growing)} 字段随交易增长, 差分方案可用)"
    elif flat:
        verdict, note = "WARN", "全部字段 60s 内零增长(可能未开盘/停牌/极端冻结), 需开盘时段复测"
    else:
        verdict, note = "WARN", "字段缺失, 逐字段核对"
    return {"verdict": verdict, "note": note, "wait_s": wait_s, "deltas": deltas}


def probe_fcamo(symbol: str) -> dict:
    """2. FCAmo 封单额语义: 与涨停池封单额对照(仅涨停股有意义)。

    直连 TQ RPC(Engine 在 Windows 本地无 vendor 配置会返回空)。
    """
    sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "marketdata" / "src"))
    from marketdata.symbol import Symbol as _Sym
    from marketdata.vendors.tq import to_tq_code as _tqc
    from marketdata.vendors.tq import tq_rpc

    tqc = _tqc(_Sym.parse(symbol, "CN"))
    raw = tq_rpc("get_more_info", {"stock_code": tqc}) if tqc else {}
    raw = raw if isinstance(raw, dict) else {}
    fc_amo = raw.get("FCAmo")
    fc_b = raw.get("FCb")
    pool = None
    order_amount = None
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector

        for p in MarketSentimentCollector().get_limit_up_pool() or []:
            if str(p.get("code")) == symbol:
                pool = p
                order_amount = p.get("amount")
                break
    except Exception as e:  # noqa: BLE001
        print(f"  涨停池获取失败: {e}")

    ratio = None
    if fc_amo and order_amount:
        ratio = round(float(fc_amo) / float(order_amount), 3)

    if ratio is not None and 0.9 <= ratio <= 1.1:
        verdict = "PASS: FCAmo ≈ 涨停池封单额(同单位同口径)"
    elif ratio is not None:
        verdict = f"WARN: FCAmo/封单额={ratio}, 非近似(检查单位或字段含义)"
    else:
        verdict = "WARN: 无法对照(当日非涨停或涨停池无此股), FCAmo 原值留存供人工判断"
    return {"verdict": verdict, "FCAmo": fc_amo, "FCb": fc_b, "pool_order_amount": order_amount, "ratio": ratio}


def probe_futures() -> dict:
    """3. 新浪期货连通性 + **原始报文留存**(盘中经验校准字段位置用)。

    2026-09-06 周六实测: HTTP 200 通, 但字段位置与社区口径不符
    ([1]=时间非价格, [9]=0 非昨结算) → 解析器正确拒绝, 降级生效。
    周一盘中: 保存多帧原始报文, 用真实波动反推最新价/昨结算位置。
    """
    from src.core.commodity_quotes import fetch_daily_close, fetch_snapshot, momentum_score

    snap = fetch_snapshot()
    closes = fetch_daily_close("SC0", days=70)
    raw_frame = None
    try:
        import httpx

        resp = httpx.get(
            "https://hq.sinajs.cn/list=nf_SC0,nf_AU0",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=8,
        )
        raw_frame = resp.content.decode("gbk", errors="replace")
    except Exception as e:  # noqa: BLE001
        raw_frame = f"<fetch failed: {e}>"
    out = {
        "snapshot_available": snap.get("available"),
        "snapshot_reason": snap.get("reason"),
        "snapshot_items": snap.get("items"),
        "sc0_daily_bars": len(closes),
        "sc0_momentum_20d": momentum_score(closes, 20),
        "raw_frame": raw_frame,
    }
    if snap.get("available") and len(closes) >= 21:
        out["verdict"] = "PASS: 快照+日K双通, 价格证据可进轮动钟"
    elif snap.get("available"):
        out["verdict"] = "PASS(部分): 快照通, 日K需检查(动量降级事件版)"
    elif raw_frame and "hq_str" in str(raw_frame):
        out["verdict"] = "WARN: 接口连通但字段映射待盘中校准(解析器拒绝可疑数据=降级正常)"
    else:
        out["verdict"] = "FAIL: 新浪接口不通, 轮动钟回落事件文本版(已设计降级, 不阻塞)"
    return out


def probe_seal_quality_real(symbol: str) -> dict:
    """4. 封单成色实跑(消费 seal_sampler 当日已采样的真实数据)。"""
    from src.core.seal_quality import compute_from_series
    from src.core.seal_sampler import get_recent_samples

    samples = get_recent_samples(symbol)
    if not samples:
        return {"verdict": "WARN", "reason": "当日无采样数据(采样任务未跑/非涨停股/库无数据)"}
    metrics = compute_from_series(samples)
    return {"verdict": "PASS(available=true)" if metrics.get("available") else f"WARN: {metrics.get('reason')}",
            "n_samples": len(samples), "metrics": metrics}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="002361", help="盘中实测标的(默认 002361 神剑股份)")
    ap.add_argument("--wait", type=int, default=60, help="差分验证两次采样间隔秒数")
    ap.add_argument("--skip-wait", action="store_true", help="跳过 60s 差分等待(非盘中跑)")
    args = ap.parse_args()

    now = datetime.now(_CST)
    print(f"=== SIDA-Pro 盘中实测探针 {now.isoformat(timespec='seconds')} ===")
    result: dict = {"probe_time": now.isoformat(timespec="seconds"), "symbol": args.symbol}

    print("\n[1] BCancel/SCancel 累计字段口径(采样两次, 间隔 {}s)...".format(args.wait))
    r1 = probe_tq_cumulative(args.symbol, 0 if args.skip_wait else args.wait)
    result["tq_cumulative"] = r1
    print(f"  判定: {r1['verdict']}\n  说明: {r1['note']}")

    print("\n[2] FCAmo 封单额语义对照...")
    r2 = probe_fcamo(args.symbol)
    result["fcamo"] = r2
    print(f"  判定: {r2['verdict']}\n  FCAmo={r2['FCAmo']} FCb={r2['FCb']} 涨停池封单额={r2['pool_order_amount']}")

    print("\n[3] 新浪期货连通性(即时)...")
    r3 = probe_futures()
    result["futures"] = r3
    print(f"  判定: {r3['verdict']}")
    for it in r3.get("snapshot_items") or []:
        print(f"    {it.get('name')}({it.get('code')}): {it.get('last')} ({it.get('chg_pct'):+.2f}%)")
    print(f"    SC0 日K条数: {r3['sc0_daily_bars']}, 20日动量: {r3['sc0_momentum_20d']}")

    print("\n[4] 封单成色实跑(当日真实采样)...")
    r4 = probe_seal_quality_real(args.symbol)
    result["seal_quality_real"] = r4
    print(f"  判定: {r4['verdict']}")

    overall = "PASS" if all(
        str(r.get("verdict", "")).startswith(("PASS", "WARN")) for r in (r1, r2, r3, r4)
    ) else "FAIL"
    result["overall"] = overall
    print(f"\n=== 总判定: {overall} (FAIL 需处理, WARN 需复测) ===")

    try:
        _RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        history = []
        if _RESULTS_FILE.exists():
            try:
                history = json.loads(_RESULTS_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                history = []
        history.append(result)
        _RESULTS_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"结果已追加: {_RESULTS_FILE}")
    except Exception as e:  # noqa: BLE001
        print(f"结果写文件失败(不影响判定): {e}")


if __name__ == "__main__":
    main()
