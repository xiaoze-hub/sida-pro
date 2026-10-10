"""每日跨源指标标定作业测试(Volume 单位事故教训)。

全部离线: 源采集一律 mock(`collector` 注入 / `_SOURCE_FETCHERS` 打桩), 禁真网络。
覆盖: 正常通过 / 恒等式违规告警 / 跨源不一致 / 单源缺失显式降级 / 全断不可校验 /
系统性缺失 / 作业诚实性(ok=False → failed)/ 报告落盘 / 复用活跃作业。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.indicator_calibration import (
    Observation,
    calibrate_observations,
    collect_observations,
    daily_job,
    run_indicator_calibration,
)


def _obs(src: str, price=10.0, vol_shares=1_000_000, amount=10_000_000.0) -> Observation:
    return Observation(src, price=price, volume_shares=vol_shares, amount=amount)


# ── 纯函数: calibrate_observations ──────────────────────────────────────────

def test_all_sources_agree_is_ok():
    v = calibrate_observations("600519", [_obs("tencent"), _obs("tq"), _obs("eastmoney")])
    assert v.status == "ok" and v.n_present == 3 and v.cross_checked
    assert v.identity_violations == [] and v.cross_mismatches == []


def test_identity_violation_flagged():
    # tq 把量当手(少 ×100)→ 恒等式 dev≈9900%
    v = calibrate_observations("600519", [_obs("tencent"), _obs("tq", vol_shares=10_000)])
    assert any(x["source"] == "tq" for x in v.identity_violations)
    assert v.sources["tq"]["status"] == "identity_violation"
    assert v.sources["tencent"]["status"] == "ok"


def test_cross_source_mismatch_flagged():
    # 两源各自内部自洽, 但量差 2 倍 → 只有跨源比对才现形
    v = calibrate_observations(
        "600519",
        [_obs("tencent", vol_shares=1_000_000, amount=10_000_000),
         _obs("tq", vol_shares=2_000_000, amount=20_000_000)],
    )
    metrics = {m["metric"]: m["dev_pct"] for m in v.cross_mismatches}
    assert "volume_shares" in metrics and "amount" in metrics
    assert metrics["volume_shares"] > 2.0
    assert v.identity_violations == []  # 各自内部恒等式都对


def test_single_source_degrades_explicitly():
    v = calibrate_observations("600519", [_obs("tencent"), Observation("tq", error="boom")])
    assert v.status == "degraded_single_source" and not v.cross_checked
    assert v.missing == ["tq"]           # 缺失显式列出, 不静默
    assert v.sources["tq"]["status"] == "missing"


def test_no_source_available():
    v = calibrate_observations("600519", [Observation("tencent", error="x"),
                                          Observation("tq", error="y")])
    assert v.status == "no_data" and v.n_present == 0


# ── collect_observations 单源失败留痕 ───────────────────────────────────────

def test_collect_observations_records_source_failure(monkeypatch):
    import src.core.indicator_calibration as ic

    def boom(_sym):
        raise RuntimeError("tq down")

    monkeypatch.setitem(ic._SOURCE_FETCHERS, "tq", boom)
    monkeypatch.setitem(ic._SOURCE_FETCHERS, "tencent", lambda s: _obs("tencent"))
    monkeypatch.setitem(ic._SOURCE_FETCHERS, "eastmoney", lambda s: _obs("eastmoney"))
    obs = collect_observations("600519")
    by = {o.source: o for o in obs}
    assert by["tencent"].usable() and by["eastmoney"].usable()
    assert by["tq"].error and "tq down" in by["tq"].error  # 失败原因显式留痕


# ── 作业级: run_indicator_calibration ───────────────────────────────────────

def _patch_pool(monkeypatch, pool):
    import src.core.indicator_calibration as ic

    monkeypatch.setattr(ic, "_sample_pool", lambda n: list(pool)[:n])


def _collect_ok(symbol):
    return [_obs("tencent"), _obs("tq"), _obs("eastmoney")]


def test_run_normal_passes_and_writes_report(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _patch_pool(monkeypatch, ["600519", "000001"])
    r = run_indicator_calibration(2, collector=_collect_ok)
    assert r["ok"] is True and r["cross_checked_symbols"] == 2
    assert r["identity_violations"] == [] and r["cross_mismatches"] == []
    path = Path(r["report_path"])
    assert path.exists() and path.parent == tmp_path / "reports" / "indicator_calibration"
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is True


def test_run_identity_violation_records_and_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _patch_pool(monkeypatch, ["600519"])
    calls: list = []
    import src.core.datasource_failures as failures_mod

    monkeypatch.setattr(failures_mod, "record",
                        lambda prov, kind="fetch", **kw: calls.append((prov, kind)))
    import src.core.alerting as alerting_mod

    monkeypatch.setattr(alerting_mod, "record_data_source_failure",
                        lambda prov, detail="": calls.append(("alert", prov)))

    def bad(_s):
        return [_obs("tencent"), _obs("tq", vol_shares=10_000), _obs("eastmoney")]

    r = run_indicator_calibration(1, collector=bad)
    assert r["ok"] is False and "恒等式" in (r["reason"] or "")
    assert ("tq", "parse") in calls and ("alert", "tq") in calls  # 显式落明细 + 告警


def test_run_all_degraded_is_not_ok(tmp_path, monkeypatch):
    """全样本仅单源可用 → 无一票可跨源校验 → ok=False(数据路径不完整), 但降级显式。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _patch_pool(monkeypatch, ["600519", "000001"])

    def one_src(_s):
        return [_obs("tencent"), Observation("tq", error="no data"),
                Observation("eastmoney", error="no data")]

    r = run_indicator_calibration(2, collector=one_src)
    assert r["ok"] is False and "跨源校验" in (r["reason"] or "")
    assert len(r["degraded_single_source"]) == 2
    assert r["cross_checked_symbols"] == 0


def test_run_partial_single_source_ok_but_degraded_surfaced(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _patch_pool(monkeypatch, ["600519", "000001"])

    def mixed(sym):
        if sym == "600519":
            # 仅 1 源可用 → 无法跨源校验(显式降级), 但同批另一票可校验 → 作业仍 ok=True
            return [_obs("tencent"), Observation("tq", error="x"),
                    Observation("eastmoney", error="y")]
        return _collect_ok(sym)

    r = run_indicator_calibration(2, collector=mixed)
    assert r["ok"] is True and r["cross_checked_symbols"] == 1
    assert [d["symbol"] for d in r["degraded_single_source"]] == ["600519"]  # 降级可见


def test_run_systemic_missing_source_alerts_and_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _patch_pool(monkeypatch, ["600519", "000001"])
    calls: list = []
    import src.core.datasource_failures as failures_mod

    monkeypatch.setattr(failures_mod, "record",
                        lambda prov, kind="fetch", **kw: calls.append((prov, kind)))

    def no_tq(sym):
        return [_obs("tencent"), Observation("tq", error="no data"), _obs("eastmoney")]

    r = run_indicator_calibration(2, collector=no_tq)
    assert r["ok"] is False and "tq" in (r["reason"] or "")
    assert ("tq", "fetch") in calls  # 系统性缺失显式落痕


def test_run_pool_failure_is_fail_soft(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def boom(_n):
        raise RuntimeError("db down")

    import src.core.indicator_calibration as ic

    monkeypatch.setattr(ic, "_sample_pool", boom)
    with pytest.raises(RuntimeError):
        # 抽样池异常由 daily_job 兜; 直调 run 会抛(便于上层 fail 判定)
        run_indicator_calibration(3, collector=_collect_ok)


# ── 作业框架诚实性 ──────────────────────────────────────────────────────────

class _FakeStore:
    def __init__(self):
        self.calls: list = []
        self._active = False
        self._create_returns: tuple | None = None

    def create(self, kind, label=""):
        self.calls.append(("create", kind))
        if self._create_returns is not None:
            return self._create_returns
        return "jid1", True

    def start(self, jid, stage=""):
        self.calls.append(("start", jid))

    def progress_reporter(self, jid, min_interval_sec=2.0):
        return lambda frac, stage="": None

    def finish(self, jid, out, context=""):
        self.calls.append(("finish", jid, out.get("ok")))
        return out.get("ok") is not False

    def fail(self, jid, error):
        self.calls.append(("fail", jid, error))


def test_daily_job_real_run_bad_collector_marks_failed(tmp_path, monkeypatch):
    """端到端: 真实 run_indicator_calibration + 注入坏 collector → 作业收尾为 failed。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.core.indicator_calibration as ic

    monkeypatch.setattr(ic, "_sample_pool", lambda n: ["600519"])
    monkeypatch.setattr(
        ic, "collect_observations",
        lambda s: [_obs("tencent"), _obs("tq", vol_shares=10_000), _obs("eastmoney")],
    )
    store = _FakeStore()
    r = daily_job(1, store=store)
    assert r["ok"] is False
    assert ("create", "indicator_calibration") in store.calls
    assert ("finish", "jid1", False) in store.calls


def test_daily_job_finish_receives_ok_false(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.core.indicator_calibration as ic

    monkeypatch.setattr(ic, "run_indicator_calibration",
                        lambda n=None, **kw: {"ok": False, "reason": "低"})
    store = _FakeStore()
    r = daily_job(1, store=store)
    assert r["ok"] is False
    assert ("finish", "jid1", False) in store.calls  # ok=False 交作业框架判 failed


def test_daily_job_reuses_active_job(tmp_path, monkeypatch):
    import src.core.indicator_calibration as ic

    ran = {"n": 0}
    monkeypatch.setattr(ic, "run_indicator_calibration",
                        lambda n=None, **kw: ran.__setitem__("n", ran["n"] + 1) or {"ok": True})
    store = _FakeStore()
    store._create_returns = ("existing", False)
    r = daily_job(1, store=store)
    assert r.get("reused") is True and ran["n"] == 0  # 复用 → 不起第二个


def test_daily_job_exception_marks_failed(monkeypatch):
    import src.core.indicator_calibration as ic

    def boom(n=None, **kw):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(ic, "run_indicator_calibration", boom)
    store = _FakeStore()
    r = daily_job(1, store=store)
    assert r["ok"] is False and any(c[0] == "fail" for c in store.calls)
