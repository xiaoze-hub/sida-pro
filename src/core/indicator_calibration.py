# -*- coding: utf-8 -*-
"""每日跨源指标标定作业(Volume 单位事故教训, 2026-10-10)。

## 为什么需要它

历史事故: **快照成交量 = 手 / 日线成交量 = 股**, 两个契约混用把量能类指标算错 100 倍。
既有的单源对账(`unit_recon`)只查**单源内部**恒等式(vol×price≈amt), 查得出"某个源自己
的字段被 ×10000", 却查不出**跨源不一致**(腾讯说 1.78e8 股、TQ 说 1.78e6 股 —— 各自内部
都自洽, 只有摆在一起才现形)。本作业补这一格: 盘后抽样 N 只 A 股, 对腾讯/东财/TQ
**三个源各取当日快照**, 做两级校验:

  ① 单源恒等式: 成交量(归一为股)×现价 ≈ 成交额(元)(口径/容差复用 `unit_check`);
  ② 跨源一致性: 同一交易日同一标的, 三源的 成交量(股)/成交额(元)/现价 应一致。

异常**显式**落 `datasource_failures` + 告警(绝不静默); 单源缺失**显式降级**(不猜、不补齐、
不把单源当多源结论); 走 jobs 框架(返回体 `ok=False` → 作业判 failed, 诚实性约定 v0.13.49)。

## 单位契约(冻结 docs/_frozen/data.md + tests/test_tq_volume_unit.py)

- `Quote.volume` = **手**(腾讯 qt.gtimg.cn / 东财 push2 f47 / TQ get_market_snapshot 同口径)
  → 归一为**股**需 ×100;
- `Quote.turnover` = **元**(腾讯 parts[35] 第三段 / 东财 f48 / TQ 快照 Amount 万元 ×1e4)。

本作业只读快照(全部一手/元), 归一后同量纲比对, 不碰日线(股)避免复权维度干扰。

报告落 `DATA_DIR/reports/indicator_calibration/YYYY-MM-DD.json`。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

# 跨源一致性容差(%): 同标的同日, 各源成交量/额/价的最大两两相对偏差上限。
# 源的快照时点/舍入会有毫厘差异(实测 EOD 同源族 <0.1%), 2% 足以放过舍入、抓得住单位错
# (×100 级单位错 → 偏差 ≈ 9900%)。可用 SIDA_CALIB_CROSS_TOL_PCT 覆盖(合理域 0.01-100)。
DEFAULT_CROSS_TOL_PCT = 2.0
SOURCES = ("tencent", "eastmoney", "tq")


@dataclass
class Observation:
    """单源观测(已归一: 成交量为股, 金额为元, 价格为元)。"""

    source: str
    price: float | None = None
    volume_shares: float | None = None
    amount: float | None = None
    error: str | None = None  # 采集失败/无数据的显式原因(单源缺失时非空)

    def usable(self) -> bool:
        return (
            isinstance(self.price, (int, float)) and self.price > 0
            and isinstance(self.volume_shares, (int, float)) and self.volume_shares > 0
            and isinstance(self.amount, (int, float)) and self.amount > 0
        )


@dataclass
class SymbolVerdict:
    """单票标定结论(纯计算, 无 I/O)。"""

    symbol: str
    sources: dict[str, dict] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)  # 有观测但字段不全(不可用)
    n_present: int = 0
    identity_violations: list[dict] = field(default_factory=list)
    cross_mismatches: list[dict] = field(default_factory=list)
    cross_checked: bool = False
    status: str = "no_data"  # ok / degraded_single_source / no_data

    def anomalies(self) -> list[dict]:
        return [*self.identity_violations, *self.cross_mismatches]


def _cross_dev_pct(vals: Sequence[float]) -> float | None:
    """多个正值的最大两两相对偏差(%): (max-min) / 均值 ×100。少于 2 个有效值 → None。"""
    xs = [float(v) for v in vals if isinstance(v, (int, float)) and v > 0]
    if len(xs) < 2:
        return None
    mid = sum(xs) / len(xs)
    if mid <= 0:
        return None
    return (max(xs) - min(xs)) / mid * 100.0


def _cross_tol_pct() -> float:
    raw = os.environ.get("SIDA_CALIB_CROSS_TOL_PCT")
    if raw:
        try:
            v = float(raw)
            if 0.01 <= v <= 100:
                return v
        except ValueError:
            pass
    return DEFAULT_CROSS_TOL_PCT


def calibrate_observations(
    symbol: str,
    observations: Sequence[Observation],
    *,
    identity_tol_pct: float | None = None,
    cross_tol_pct: float | None = None,
) -> SymbolVerdict:
    """跨源标定单票(纯函数)。

    identity_tol_pct: 单源恒等式容差(None → unit_check 的 tol, 实测校准 5%)。
    cross_tol_pct: 跨源一致性容差(None → SIDA_CALIB_CROSS_TOL_PCT 或默认 2%)。
    """
    from src.core.unit_check import _tol_from_env, unit_deviation

    itol = identity_tol_pct if identity_tol_pct is not None else _tol_from_env()
    ctol = cross_tol_pct if cross_tol_pct is not None else _cross_tol_pct()
    v = SymbolVerdict(symbol=symbol)

    usable: list[Observation] = []
    for o in observations or []:
        if o.usable():
            dev = unit_deviation(volume=o.volume_shares, close=o.price, amount=o.amount)
            v.sources[o.source] = {
                "price": o.price, "volume_shares": o.volume_shares, "amount": o.amount,
                "identity_dev_pct": None if dev is None else round(dev, 4),
                "status": "identity_violation" if (dev is not None and dev > itol) else "ok",
            }
            usable.append(o)
            if dev is not None and dev > itol:
                v.identity_violations.append(
                    {"kind": "identity", "source": o.source, "dev_pct": round(dev, 4),
                     "tol_pct": itol}
                )
        else:
            v.sources[o.source] = {"price": o.price, "volume_shares": o.volume_shares,
                                   "amount": o.amount, "identity_dev_pct": None,
                                   "status": "missing", "error": o.error}
            if o.error:
                v.missing.append(o.source)
            else:
                v.degraded.append(o.source)

    v.n_present = len(usable)
    if v.n_present == 0:
        v.status = "no_data"
        return v
    if v.n_present == 1:
        v.status = "degraded_single_source"
        return v

    # 跨源一致性: 成交量(股)/成交额/现价 三个关键量各自求最大两两相对偏差。
    for metric, key in (("volume_shares", "volume_shares"), ("amount", "amount"), ("price", "price")):
        vals = [getattr(o, key) for o in usable]
        dev = _cross_dev_pct(vals)  # type: ignore[arg-type]
        if dev is not None and dev > ctol:
            v.cross_mismatches.append(
                {"kind": "cross", "metric": metric, "dev_pct": round(dev, 4),
                 "tol_pct": ctol, "sources": [o.source for o in usable]}
            )
    v.cross_checked = True
    v.status = "ok"
    return v


# ── 源适配: 各源快照 → 归一 Observation(成交量股 / 金额元 / 价格元) ──────────────

def _obs_tencent(symbol: str) -> Observation | None:
    from marketdata.symbol import Symbol
    from marketdata.vendors.tencent import TencentQuoteVendor

    q = TencentQuoteVendor().fetch([Symbol.parse(symbol, "CN")], {})
    if not q:
        return None
    q = q[0]
    if q.current_price is None:
        return None
    vol_shares = q.volume * 100 if isinstance(q.volume, (int, float)) else None
    return Observation("tencent", price=q.current_price, volume_shares=vol_shares, amount=q.turnover)


def _obs_eastmoney(symbol: str) -> Observation | None:
    from marketdata.symbol import Symbol
    from marketdata.vendors.eastmoney import EastmoneyQuoteVendor

    q = EastmoneyQuoteVendor().fetch([Symbol.parse(symbol, "CN")], {})
    if not q:
        return None
    q = q[0]
    if q.current_price is None:
        return None
    vol_shares = q.volume * 100 if isinstance(q.volume, (int, float)) else None
    return Observation("eastmoney", price=q.current_price, volume_shares=vol_shares, amount=q.turnover)


def _obs_tq(symbol: str) -> Observation | None:
    from marketdata.symbol import Symbol
    from marketdata.vendors.tq import TqQuoteVendor

    q = TqQuoteVendor().fetch([Symbol.parse(symbol, "CN")], {})
    if not q:
        return None
    q = q[0]
    if q.current_price is None:
        return None
    vol_shares = q.volume * 100 if isinstance(q.volume, (int, float)) else None
    return Observation("tq", price=q.current_price, volume_shares=vol_shares, amount=q.turnover)


_SOURCE_FETCHERS: dict[str, Callable[[str], Observation | None]] = {
    "tencent": _obs_tencent,
    "eastmoney": _obs_eastmoney,
    "tq": _obs_tq,
}


def collect_observations(symbol: str) -> list[Observation]:
    """三源各取一次当日快照 → 归一 Observation 列表(单源失败**显式**留痕, 不静默)。"""
    out: list[Observation] = []
    for name in SOURCES:
        try:
            o = _SOURCE_FETCHERS[name](symbol)
            out.append(o if o is not None else Observation(name, error="no data"))
        except Exception as e:  # noqa: BLE001
            out.append(Observation(name, error=f"{type(e).__name__}: {e}"))
    return out


def _record_anomaly(source: str, *, kind: str, symbol: str, detail: str) -> None:
    """异常显式落明细 + 告警(两条链路都 fail-soft, 任一不可用不影响标定主流程)。"""
    try:
        from src.core.datasource_failures import record

        record(source or "unknown", kind=kind, symbol=symbol, detail=detail)
    except Exception as e:  # noqa: BLE001
        logger.debug("[跨源标定] 失败明细落库异常(忽略): %r", e)
    try:
        from src.core.alerting import record_data_source_failure

        record_data_source_failure(source or "unknown", detail)
    except Exception as e:  # noqa: BLE001
        logger.debug("[跨源标定] 告警计数异常(忽略): %r", e)


def _sample_pool(n: int) -> list[str]:
    """抽样池(复用单位对账口径: watchlist/候选池 + 库挂回退固定流动池, 无网络)。"""
    from src.core.unit_recon import _sample_pool as _pool

    return [s for s, m in _pool(n) if m == "CN"]


def run_indicator_calibration(
    sample_n: int | None = None,
    *,
    collector: Callable[[str], list[Observation]] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """跑一次跨源标定并落报告。返回汇总 dict(ok 语义见下)。fail-soft: 单票异常只记警告。

    返回体恒带 `ok`:
      ok=True  —— 至少做了跨源校验, 且无单源恒等式违规、无跨源不一致;
      ok=False —— 出现恒等式违规 / 跨源不一致 / 无一票可跨源校验(数据路径不完整);
    `degraded_symbols` 单源缺失的显式降级清单(ok 仍可为 True, 但绝不静默)。
    """
    sample_n = int(sample_n or 20)
    collect = collector or collect_observations
    t0 = time.time()

    def report(frac: float, stage: str) -> None:
        if progress is None:
            return
        try:
            progress(max(0.0, min(1.0, float(frac))), stage)
        except Exception as e:  # noqa: BLE001
            logger.debug("[跨源标定] 进度上报失败: %r", e)

    pool = _sample_pool(sample_n)
    report(0.05, f"抽样池 {len(pool)} 只")

    identity_violations: list[dict] = []
    cross_mismatches: list[dict] = []
    degraded_symbols: list[dict] = []
    no_data_symbols: list[str] = []
    cross_checked = 0
    missing_by_source: dict[str, int] = {s: 0 for s in SOURCES}

    total = max(1, len(pool))
    for i, symbol in enumerate(pool, 1):
        try:
            obs = collect(symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("[跨源标定] %s 采集异常: %r", symbol, e)
            no_data_symbols.append(symbol)
            continue
        try:
            v = calibrate_observations(symbol, obs)
        except Exception as e:  # noqa: BLE001
            logger.warning("[跨源标定] %s 标定异常: %r", symbol, e)
            no_data_symbols.append(symbol)
            continue

        for src in v.missing:
            missing_by_source[src] = missing_by_source.get(src, 0) + 1
        for a in v.identity_violations:
            identity_violations.append({"symbol": symbol, **a})
            _record_anomaly(a["source"], kind="parse", symbol=symbol,
                            detail=f"单源恒等式偏差 {a['dev_pct']}% > {a['tol_pct']}%")
        for m in v.cross_mismatches:
            cross_mismatches.append({"symbol": symbol, **m})
        if v.cross_checked:
            cross_checked += 1
        elif v.status == "degraded_single_source":
            degraded_symbols.append({"symbol": symbol,
                                     "present": [s for s, d in v.sources.items()
                                                 if d.get("status") == "ok"],
                                     "reason": "仅 1 源可用, 无法跨源校验"})
        elif v.status == "no_data":
            no_data_symbols.append(symbol)
        if i % 5 == 0 or i == total:
            report(0.05 + 0.9 * i / total, f"标定 {i}/{total}")

    # 系统性单源缺失(样本内**全部**标的都取不到该源)→ 显式落痕告警(局部缺失只在报告里降级)。
    # 阈值取"全部"而非"半数": 抽样里偶尔取不到某源是常态(限流/短断), 只有当整批都拿不到
    # 才构成"这个源当前不可用"的结论。
    systemic_missing = {
        s: cnt for s, cnt in missing_by_source.items() if pool and cnt >= len(pool)
    }
    for src, cnt in systemic_missing.items():
        _record_anomaly(src, kind="fetch", symbol="",
                        detail=f"跨源标定: 抽样 {cnt}/{len(pool)} 只取不到该源(系统性缺失)")

    problems = bool(identity_violations or cross_mismatches)
    unverifiable = cross_checked == 0  # 无一票做成跨源校验 = 数据路径不完整
    ok = (not problems) and (not unverifiable) and (not systemic_missing)

    if identity_violations:
        logger.error("[跨源标定] 单源恒等式违规 %d 处(疑似单位换算错误)", len(identity_violations))
    if cross_mismatches:
        logger.error("[跨源标定] 跨源不一致 %d 处", len(cross_mismatches))
    if unverifiable:
        logger.warning("[跨源标定] 无一票可跨源校验(池 %d 只): 数据路径不完整", len(pool))

    reason = None
    if identity_violations or cross_mismatches:
        reason = (f"恒等式违规 {len(identity_violations)} / 跨源不一致 {len(cross_mismatches)} 处")
    elif unverifiable:
        reason = f"无一票可跨源校验(池 {len(pool)} 只), 数据路径不完整"
    elif systemic_missing:
        reason = "系统性单源缺失: " + ", ".join(f"{s}({c})" for s, c in systemic_missing.items())

    report(0.98, "落报告")
    verdict = {
        "ok": ok,
        "reason": reason,
        "date": datetime.now(_CST).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_pool": pool,
        "sample_n": len(pool),
        "cross_checked_symbols": cross_checked,
        "degraded_single_source": degraded_symbols,
        "no_data_symbols": no_data_symbols,
        "missing_by_source": missing_by_source,
        "systemic_missing": systemic_missing,
        "identity_violations": identity_violations,
        "cross_mismatches": cross_mismatches,
        "identity_tol_pct": _identity_tol(),
        "cross_tol_pct": _cross_tol_pct(),
        "elapsed_s": round(time.time() - t0, 1),
    }
    _write_report(verdict)
    logger.info(
        "[跨源标定] 完成: %d 股, 跨源校验 %d, 恒等式违规 %d, 跨源不一致 %d, ok=%s (%.1fs)",
        len(pool), cross_checked, len(identity_violations), len(cross_mismatches),
        ok, verdict["elapsed_s"],
    )
    return verdict


def _identity_tol() -> float:
    from src.core.unit_check import _tol_from_env

    return _tol_from_env()


def _write_report(report: dict[str, Any]) -> None:
    """报告落 DATA_DIR/reports/indicator_calibration/YYYY-MM-DD.json(W2.2/E4 单一口径)。"""
    data_dir = os.environ.get("DATA_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"
    )
    out_dir = os.path.join(data_dir, "reports", "indicator_calibration")
    path = os.path.join(out_dir, f"{report['date']}.json")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["report_path"] = path
    except Exception as e:  # noqa: BLE001
        logger.warning("[跨源标定] 报告写入失败 %s: %r", path, e)


def daily_job(sample_n: int | None = None, *, store=None) -> dict[str, Any]:
    """cron 入口(交易日盘后): 跨源标定走 jobs 框架, ok=False → 作业判 failed。永不抛异常。

    store: 作业存储(默认 `src.core.jobs.jobs`; 测试可注入)。作业表不可用时退化为
    "只跑并返回报告"(报告本身带 ok, 不因作业框架挂掉而假成功)。
    """
    report_fn = run_indicator_calibration
    if store is None:
        try:
            from src.core.jobs import jobs as store  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            store = None

    jid = None
    progress_cb = None
    if store is not None:
        try:
            jid, is_new = store.create("indicator_calibration", "每日跨源指标标定")
            if not is_new:
                return {"ok": True, "reused": True, "job_id": jid,
                        "note": "同类作业进行中, 复用既有 job_id(不并发起第二个)"}
            store.start(jid, "采样")
            progress_cb = store.progress_reporter(jid, min_interval_sec=2.0)
        except Exception as e:  # noqa: BLE001 - 作业框架不可用不该阻断标定
            logger.warning("[跨源标定] 作业框架不可用, 退化为直接运行: %r", e)
            jid = None
            progress_cb = None

    try:
        report = report_fn(sample_n, progress=progress_cb)
    except Exception as e:  # noqa: BLE001
        logger.exception("[跨源标定] 运行异常: %s", e)
        if store is not None and jid is not None:
            try:
                store.fail(jid, f"运行异常: {e}")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "error": str(e)}

    if store is not None and jid is not None:
        try:
            store.finish(jid, report, context="跨源指标标定: ")
        except Exception as e:  # noqa: BLE001
            logger.warning("[跨源标定] 作业收尾失败: %r", e)
    return report
