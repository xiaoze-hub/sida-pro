"""离线复盘（S0）单测 —— 只提议、不落盘（2026-10-23）。

守的是三件事：
1. 信号必须有证据（run_id + 具体数字），不许「整体偏慢」这种空话；
2. `propose` **一次配置文件都不读、一笔都不写** —— 用 monkeypatch 把 open/read_text/
   write_text 全部改成抛异常，跑通即证明它没碰盘；再对比 recipes/skills 的 sha256；
3. 轨迹不可用/样本为 0 时，报告写「样本不足」而不是伪造数字。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from _support_fake_db import FakeTraceDB
from src.core import harness_replay as hr

ROOT = Path(__file__).resolve().parents[1]
CONFIG_GLOBS = ("src/harness/recipes/*.yaml", "src/harness/skills/*.md")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _run(run_id, *, recipe="deep_diagnosis", tool_calls=3, status="ok", question="002050 现在怎么样",
         error=None, days_ago=1):
    return SimpleNamespace(
        run_id=run_id, created_at=_now() - timedelta(days=days_ago), recipe=recipe,
        question=question, status=status, error=error, tool_calls=tool_calls,
        elapsed_ms=5000, conversation_id=1, user_id="u1", finished_at=None,
    )


def _step(run_id, seq, tool, *, ok=True, error=None, elapsed_ms=800):
    return SimpleNamespace(run_id=run_id, seq=seq, tool=tool, args='{"symbol": "002050"}',
                           ok=ok, error=error, result_digest="…", elapsed_ms=elapsed_ms)


def _fixture_db():
    """5 轮正常 + 2 轮零工具 + 3 次 get_orderbook 失败 + 1 轮缺维度标记。"""
    runs, steps = [], []
    for i in range(1, 6):
        rid = f"run-{i}"
        runs.append(_run(rid))
        steps.append(_step(rid, 1, "get_trend_profile", elapsed_ms=600))
        # 前 3 轮失败（3 次）+ 后 2 轮成功（凑够 n>=5 让 p50/p90 出得来）
        steps.append(_step(rid, 2, "get_orderbook", ok=(i > 3),
                           error=None if i > 3 else "upstream timeout after 19s",
                           elapsed_ms=19000))
    for i in (6, 7):
        runs.append(_run(f"run-{i}", recipe=None, tool_calls=0, question="600519 还能拿吗"))
    runs.append(_run("run-8", error="failed_tools=1/3; first_error=get_dividend: 未取到"))
    steps.append(_step("run-8", 1, "get_dividend", ok=False, error="未取到 分红数据"))
    return FakeTraceDB(runs=runs, steps=steps)


RUN_IDS = [f"run-{i}" for i in range(1, 9)]


def _config_hashes() -> dict[str, str]:
    out = {}
    for pattern in CONFIG_GLOBS:
        for p in sorted(ROOT.glob(pattern)):
            out[p.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _config_mtimes() -> dict[str, float]:
    out = {}
    for pattern in CONFIG_GLOBS:
        for p in sorted(ROOT.glob(pattern)):
            out[p.as_posix()] = p.stat().st_mtime_ns
    return out


# ------------------------------------------------------------- collect_signals


def test_collect_signals_covers_all_six_kinds():
    sig = hr.collect_signals(_fixture_db(), days=14, limit=200)

    assert sig["available"] is True
    assert sig["n_runs"] == 8
    s = sig["signals"]
    assert set(s) == {
        "zero_tool_runs", "degraded_rate_by_recipe", "error_rate_by_recipe",
        "slow_tools", "repeated_failed_tool", "missing_dimension_markers",
    }

    # 零工具：只有「问题里有 6 位代码」的两轮算数
    assert s["zero_tool_runs"]["n"] == 2
    assert s["zero_tool_runs"]["samples"][0]["tool_calls"] == 0
    assert s["zero_tool_runs"]["samples"][0]["run_id"] in RUN_IDS

    # 慢工具表：n>=5 的工具都给 p50/p90（是否「慢」由 propose 用 8s 门槛判）
    slow = {t["tool"]: t for t in s["slow_tools"]}
    assert set(slow) == {"get_orderbook", "get_trend_profile"}
    assert slow["get_orderbook"]["n"] == 5
    assert slow["get_orderbook"]["p90_ms"] >= 8000
    assert slow["get_orderbook"]["sample_run_id"] in RUN_IDS
    assert slow["get_trend_profile"]["p90_ms"] == 600
    assert slow["get_trend_profile"]["n"] == 5

    # 反复失败：>=3 次且带错误原文
    rep = {t["tool"]: t for t in s["repeated_failed_tool"]}
    assert rep["get_orderbook"]["n_failed"] == 3
    assert rep["get_orderbook"]["errors"][0]["count"] == 3
    assert "timeout" in rep["get_orderbook"]["errors"][0]["error"]
    assert rep["get_orderbook"]["recipe"] == "deep_diagnosis"

    # 缺维度标记（回答不落库 → 从 error 侧看）
    assert s["missing_dimension_markers"]["n"] == 1
    assert s["missing_dimension_markers"]["samples"][0]["run_id"] == "run-8"


def test_collect_signals_survives_unreadable_db():
    """没建表 / 库不可达 → available=False + 原因，不补 0 冒充「没问题」。"""

    class _Boom:
        def query(self, *_a, **_k):
            raise RuntimeError("no such table: copilot_runs")

    sig = hr.collect_signals(_Boom(), days=0)
    assert sig["available"] is False
    assert "轨迹表不可读" in sig["reason"]
    assert sig["n_runs"] == 0


# --------------------------------------------------------------------- propose


def test_propose_every_evidence_has_run_id_and_number():
    prop = hr.propose(_fixture_db(), days=14)

    assert prop["available"] is True
    assert prop["readonly"] is True
    assert prop["n_runs"] == 8
    assert prop["proposals"], "有信号就必须出建议"
    assert "冻结环" not in json.dumps(prop["proposals"], ensure_ascii=False)

    for p in prop["proposals"]:
        assert set(p) == {"id", "kind", "target", "evidence", "suggested_change"}
        assert p["kind"] in {"recipe_when", "recipe_tools", "skill_text", "metric_only"}
        any_run = any(rid in p["evidence"] for rid in RUN_IDS)
        has_num = any(ch.isdigit() for ch in p["evidence"])
        assert any_run, f"证据里没有 run_id: {p['evidence']}"
        assert has_num, f"证据里没有数字: {p['evidence']}"
        assert p["target"], "target 不许为空"

    kinds = {p["kind"] for p in prop["proposals"]}
    assert "recipe_when" in kinds          # 零工具 → 路由没命中
    assert "recipe_tools" in kinds         # 反复失败的工具
    targets = " ".join(p["target"] for p in prop["proposals"])
    assert "src/harness/recipes/" in targets


def test_propose_never_touches_disk(monkeypatch):
    """把 open / read_text / write_text 全改成抛异常：propose 仍须跑通，且配置字节不变。"""
    db = _fixture_db()
    before_hash, before_mtime = _config_hashes(), _config_mtimes()
    assert before_hash, "夹具前提：仓库里确实有配方/手册文件"

    def _boom(*a, **k):
        raise AssertionError("propose 不许做任何文件 I/O")

    with monkeypatch.context() as m:
        m.setattr("builtins.open", _boom)
        m.setattr(Path, "read_text", _boom)
        m.setattr(Path, "write_text", _boom)
        m.setattr(Path, "open", _boom)
        prop = hr.propose(db, days=14)
        md = hr.render(prop)

    assert prop["proposals"] and "# 离线复盘建议" in md

    # 字节级不变：sha256 与 mtime 都必须一致（mtime 能抓到「写了又写回」）
    assert _config_hashes() == before_hash
    assert _config_mtimes() == before_mtime


def test_propose_uses_repo_recipe_paths_only_as_strings():
    """target 是拼出来的字符串：配方目录里不存在这个 id 也照样返回（不校验、不读）。"""
    db = FakeTraceDB(
        runs=[_run(f"run-{i}", recipe="ghost_recipe", tool_calls=3,
                   question="600519 还能拿吗") for i in (1, 2)]
        + [_run(f"run-{i}", recipe="ghost_recipe", tool_calls=0, question="600519 还能拿吗")
           for i in (10, 11)],
        steps=[],
    )
    prop = hr.propose(db, days=14)
    assert any(p["target"] == "src/harness/recipes/ghost_recipe.yaml" for p in prop["proposals"])
    assert not (ROOT / "src/harness/recipes/ghost_recipe.yaml").exists()


# ---------------------------------------------------------------------- render


def test_render_is_markdown_with_banner_and_evidence():
    md = hr.render(hr.propose(_fixture_db(), days=14))
    assert md.startswith("# 离线复盘建议")
    assert "只读：未修改任何配方/提示词/配置" in md
    assert "n=8" in md
    assert "## 冻结环" in md
    assert "run-" in md


def test_render_empty_window_says_sample_insufficient():
    md = hr.render(hr.propose(FakeTraceDB(), days=0))
    assert "样本不足" in md
    assert "只读：未修改任何配方/提示词/配置" in md
    # 不许出现伪命中率/伪耗时
    assert "ms" not in md or "p90" not in md


# ------------------------------------------------------------------ CLI smoke


def test_cli_smoke_writes_report_and_exits_zero(tmp_path):
    out = tmp_path / "rsi_review_smoke.md"
    env = dict(os.environ)
    env.update({
        "SIDA_ALLOW_SQLITE": "1",
        "SIDA_DB_URL": f"sqlite:///{tmp_path / 'empty.db'}",
    })
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "scripts/rsi_offline_review.py", "--days", "0", "--out", str(out)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    body = out.read_text(encoding="utf-8")
    assert "样本不足" in body
    assert "只读：未修改任何配方/提示词/配置" in body
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines[-1] == "只读：未修改任何配方/提示词/配置"
