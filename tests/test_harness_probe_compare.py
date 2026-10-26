"""P0-0.3 对照执行器：把「改一句提示词有没有用」变成可比数据。

铁律（计划书 §1.3）：任何"优化"都必须有对照 —— 同任务/同模型/同环境，
pass_rate 与成本同时给出；**无收益即回滚**，且不看 LLM 评审（主观分不可复核）。

这里测的是判定逻辑本身，不联网：
- summarize 的 cost 代理是 `tool_calls + chars/1000`（拿不到 usage 时不编 token 数）；
- decide 对"合规率下降 / 失败增加 / 成本涨幅超限"三种退化都要判 rollback。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def d3():
    path = Path(__file__).resolve().parents[1] / "scripts" / "harness_d3_probe.py"
    spec = importlib.util.spec_from_file_location("harness_d3_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(structured=True, tools=3, chars=600, secs=10.0, err=None):
    if err:
        return {"agent": "x", "question": "q", "error": err}
    return {"agent": "x", "question": "q", "structured": structured,
            "tool_calls": tools, "chars": chars, "seconds": secs}


# ── summarize ─────────────────────────────────────────────────────────────
def test_汇总四项指标(d3):
    s = d3.summarize([_row(), _row(structured=False, tools=5, chars=400)])
    assert s["n"] == 2
    assert s["structure_n"] == 1
    assert s["pass_rate"] == 0.5
    assert s["tool_calls"] == 8
    assert s["avg_tool_calls"] == 4.0
    assert s["chars"] == 1000
    # cost 代理：8 次调用 + 1000 字 / 1000
    assert s["cost"] == 9.0


def test_失败的_case_不计入合规率但有单独计数(d3):
    s = d3.summarize([_row(), _row(err="timeout")])
    assert s["n"] == 1
    assert s["errors"] == 1
    assert s["pass_rate"] == 1.0, "失败的不能算分母，否则一次超时就把分数拉低成假退化"


def test_空批次不炸也不编数(d3):
    s = d3.summarize([])
    assert s["n"] == 0 and s["pass_rate"] == 0.0 and s["cost"] == 0.0 and s["errors"] == 0


# ── decide ────────────────────────────────────────────────────────────────
def _sum(**kw):
    base = {"n": 3, "errors": 0, "pass_rate": 0.67, "structure_n": 2,
            "tool_calls": 9, "avg_tool_calls": 3.0, "chars": 1500, "cost": 10.5, "seconds": 30.0}
    base.update(kw)
    return base


def test_持平或无退化即保留(d3):
    r = d3.decide(_sum(), _sum(pass_rate=0.67, cost=10.5))
    assert r["verdict"] == "keep"
    assert r["reasons"] == []


def test_合规率下降立刻回滚(d3):
    r = d3.decide(_sum(), _sum(pass_rate=0.33, cost=9.0))
    assert r["verdict"] == "rollback"
    assert any("结构合规率下降" in x for x in r["reasons"])
    assert r["delta_pass_rate"] == pytest.approx(-0.34)


def test_失败数增加立刻回滚(d3):
    r = d3.decide(_sum(), _sum(errors=1))
    assert r["verdict"] == "rollback"
    assert any("失败数增加" in x for x in r["reasons"])


def test_成本涨幅超上限回滚(d3):
    r = d3.decide(_sum(cost=10.0), _sum(cost=13.0))  # +30% > 20%
    assert r["verdict"] == "rollback"
    assert any("成本上升" in x for x in r["reasons"])


def test_成本涨幅在上限内保留(d3):
    r = d3.decide(_sum(cost=10.0), _sum(cost=11.5))  # +15% <= 20%
    assert r["verdict"] == "keep"


def test_合规提升但成本暴涨_仍回滚(d3):
    """两件事要同时成立，不能"效果变好就不算成本"。"""
    r = d3.decide(_sum(pass_rate=0.33, cost=10.0), _sum(pass_rate=1.0, cost=40.0))
    assert r["verdict"] == "rollback"
    assert any("成本上升" in x for x in r["reasons"])


def test_基线成本为0而候选有成本_视为退化(d3):
    r = d3.decide(_sum(cost=0.0), _sum(cost=2.0))
    assert r["verdict"] == "rollback"


def test_多个退化原因全部列出_不只报第一条(d3):
    r = d3.decide(_sum(cost=10.0), _sum(pass_rate=0.0, cost=30.0, errors=2))
    assert len(r["reasons"]) == 3


# ── compare（网络层打桩）──────────────────────────────────────────────────
def test_compare_同时跑对照与候选并给结论(d3, monkeypatch):
    calls: list[str | None] = []

    def fake_run_case(base, token, agent, q, sym):
        calls.append(agent)
        # 候选配方工具调用更少、合规一致
        return _row(structured=True, tools=2 if agent else 4, chars=600)

    monkeypatch.setattr(d3, "run_case", fake_run_case)
    res = d3.compare("http://x", "t", [("q1", None), ("q2", None)], "postmarket_reviewer", None)

    assert res["baseline_agent"] == "default"
    assert res["candidate_agent"] == "postmarket_reviewer"
    assert res["baseline"]["tool_calls"] == 8
    assert res["candidate"]["tool_calls"] == 4
    assert res["decision"]["verdict"] == "keep"
    # 基线先跑、候选后跑，各两问
    assert calls == [None, None, "postmarket_reviewer", "postmarket_reviewer"]


def test_compare_某个_case抛异常不拖垮整批(d3, monkeypatch):
    def flaky(base, token, agent, q, sym):
        if agent and q == "q2":
            raise TimeoutError("boom")
        return _row()

    monkeypatch.setattr(d3, "run_case", flaky)
    res = d3.compare("http://x", "t", [("q1", None), ("q2", None)], "cand")
    assert res["candidate"]["errors"] == 1
    assert res["decision"]["verdict"] == "rollback", "候选失败必须判回滚"


# ── P0-0.4：三条客观判据（2026-10-25）────────────────────────────────────
# 为什么单独测：`decide()` 只看 pass_rate + cost，会让"结构没退化但关键位变差"的改动静默通过。
# 这里钉死「任一维度退化即 rollback」，以及「不可用维度不参与比较」。


def _rows(texts, agent=None):
    return [{"question": "q", "text": t, "structured": True, "tool_calls": 3, "chars": 300,
             "seconds": 1.0, "run_id": f"r{i}", "error": None}
            for i, t in enumerate(texts)]


def test_acceptance_缺段能说出缺哪段(d3):
    m = d3.acceptance_metrics(_rows(["【结论】只有结论"]), "deep_diagnosis")
    assert m["available"] is True
    assert m["acceptance_rate"] == 0.0
    assert m["missing_top"], "必须报出缺了哪几段，不能只给一个 0"


def test_acceptance_段落全齐为满(d3):
    want = d3._expected_section_list("deep_diagnosis")
    text = "".join(f"【{w}】内容" for w in want)
    m = d3.acceptance_metrics(_rows([text]), "deep_diagnosis")
    assert m["acceptance_rate"] == 1.0


def test_fitness_无库必须明说不可用_不冒充(d3):
    m = d3.fitness_metrics(["r1", "r2"], db=None)
    assert m["available"] is False
    assert "未接本地库" in m["reason"]
    assert m["hit_rate"] is None, "没对账过就不能给命中率"


def test_fitness_行情没走完不计入分母但报覆盖率(d3, monkeypatch):
    calls = {"n": 0}

    def fake_score_run(db, rid, horizon_td=3):
        calls["n"] += 1
        if rid == "r1":
            return {"available": True, "hit_resist": True, "hit_support": False}
        return {"available": False, "reason": "行情尚未走完"}

    import src.core.fitness_keypoints as fk
    monkeypatch.setattr(fk, "score_run", fake_score_run)
    m = d3.fitness_metrics(["r1", "r2"], db=object())
    assert m["scored"] == 1 and m["n"] == 2
    assert m["coverage"] == 0.5
    assert m["hit_rate"] == 0.5, "1 命中 / (1 样本 × 支撑阻力 2 次)"


def test_结构没退化但关键位变差也要回滚(d3):
    base = {"errors": 0, "cost": 100.0,
            "acceptance": {"available": True, "acceptance_rate": 1.0},
            "fitness": {"available": True, "hit_rate": 0.8, "coverage": 1.0}}
    cand = {"errors": 0, "cost": 100.0,
            "acceptance": {"available": True, "acceptance_rate": 1.0},
            "fitness": {"available": True, "hit_rate": 0.4, "coverage": 1.0}}
    d = d3.decide3(base, cand)
    assert d["verdict"] == "rollback"
    assert any("关键位命中率" in r for r in d["reasons"])


def test_两侧fitness都不可用时不比该维度(d3):
    base = {"errors": 0, "cost": 10.0,
            "acceptance": {"available": True, "acceptance_rate": 1.0},
            "fitness": {"available": False, "hit_rate": None}}
    cand = {"errors": 0, "cost": 10.0,
            "acceptance": {"available": True, "acceptance_rate": 1.0},
            "fitness": {"available": False, "hit_rate": None}}
    assert d3.decide3(base, cand)["verdict"] == "keep", "不可用维度不能判退化"


def test_三维新工具全提升则保留(d3):
    base = {"errors": 0, "cost": 100.0,
            "acceptance": {"available": True, "acceptance_rate": 0.5},
            "fitness": {"available": True, "hit_rate": 0.3}}
    cand = {"errors": 0, "cost": 110.0,
            "acceptance": {"available": True, "acceptance_rate": 0.9},
            "fitness": {"available": True, "hit_rate": 0.6}}
    d = d3.decide3(base, cand)
    assert d["verdict"] == "keep"
    assert d["criteria"]["fitness"]["cand"] == 0.6
