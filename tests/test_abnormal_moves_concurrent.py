"""analyze_for_symbols v0.4.78 并发版测试"""
import time

import pytest


def _import():
    from src.core.abnormal_moves import analyze_for_symbols
    return analyze_for_symbols


def test_analyze_for_symbols_concurrent_speedup():
    """并发版应该比串行快 — 模拟 10 只股 × 0.5s 串行=5s, 并发 4 路 ~1.5s。"""
    analyze_for_symbols = _import()

    def fake_analyzer(sym):
        time.sleep(0.5)  # 模拟 K线查询
        if sym == "FAIL":
            raise RuntimeError("mock fail")
        return {
            "available": True,
            "symbol": sym,
            "proximity": 0.9 if sym.startswith("A") else 0.3,
            "name": sym,
        }

    syms = [f"A{s:03d}" for s in range(10)] + ["FAIL"]  # 11 只, 1 只故意失败
    t0 = time.time()
    results = analyze_for_symbols(syms, min_proximity=0.5, analyzer=fake_analyzer,
                                  max_workers=4, per_symbol_timeout_s=8.0)
    elapsed = time.time() - t0
    # 串行 11 × 0.5s = 5.5s, 并发 4 路: ceil(11/4)=3 轮 × 0.5s = 1.5s
    assert elapsed < 4.0, f"太慢: {elapsed:.2f}s"
    # 验证 proximity 倒序 + 过滤
    assert all(r["proximity"] >= 0.5 for r in results)
    assert len(results) == 10  # FAIL 不算
    # 倒序
    proxs = [r["proximity"] for r in results]
    assert proxs == sorted(proxs, reverse=True)
    print(f"✓ 11 股并发 {elapsed:.2f}s, 命中 {len(results)} (proximity>=0.5)")


def test_analyze_for_symbols_per_symbol_timeout():
    """单只股超时 8s 后应跳过, 不阻塞全局。"""
    analyze_for_symbols = _import()

    def slow_analyzer(sym):
        time.sleep(10)  # 必超时
        return {"available": True, "symbol": sym, "proximity": 0.9, "name": sym}

    def fast_analyzer(sym):
        return {"available": True, "symbol": sym, "proximity": 0.9, "name": sym}

    # 一慢两快, 全局应在 9s 内完成(快股不被拖死)
    syms = ["SLOW", "FAST1", "FAST2"]
    t0 = time.time()

    def analyzer_dispatch(sym):
        return slow_analyzer(sym) if sym == "SLOW" else fast_analyzer(sym)

    results = analyze_for_symbols(syms, min_proximity=0.5, analyzer=analyzer_dispatch,
                                  max_workers=4, per_symbol_timeout_s=2.0)
    elapsed = time.time() - t0
    # SLOW 单股 2s 超时, FAST 立刻返回
    assert elapsed < 6.0, f"全局拖到 {elapsed:.2f}s, per_symbol_timeout 未生效"
    # SLOW 被跳过, FAST 命中
    assert len(results) == 2
    assert all(r["symbol"] in ("FAST1", "FAST2") for r in results)
    print(f"✓ SLOW 超时 2s, FAST 即回, 总耗时 {elapsed:.2f}s")


def test_analyze_for_symbols_empty():
    analyze_for_symbols = _import()
    assert analyze_for_symbols([], min_proximity=0.5, analyzer=lambda s: None) == []
    print("✓ 空列表返回 []")


def test_analyze_for_symbols_all_filter():
    analyze_for_symbols = _import()
    syms = ["A", "B", "C"]
    res = analyze_for_symbols(syms, min_proximity=0.5,
                               analyzer=lambda s: {"available": True, "symbol": s, "proximity": 0.1, "name": s},
                               max_workers=2)
    assert res == []
    print("✓ 全部 < min_proximity 返回 []")


def test_analyze_for_symbols_exception_isolation():
    """异常单只不应影响整体(并行容错)。"""
    analyze_for_symbols = _import()
    syms = ["OK", "BOOM", "OK2"]

    def boom_analyzer(sym):
        if sym == "BOOM":
            raise ValueError("boom")
        return {"available": True, "symbol": sym, "proximity": 0.7, "name": sym}

    results = analyze_for_symbols(syms, min_proximity=0.5, analyzer=boom_analyzer, max_workers=3)
    assert len(results) == 2
    assert {r["symbol"] for r in results} == {"OK", "OK2"}
    print("✓ BOOM 异常被隔离")
