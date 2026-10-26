"""威科夫量价结构纯规则单测(src/core/wyckoff.py)。

钉死(纯函数, 无网络/DB/时钟):
1. 四信号各至少一个正例 + 反例(阈值边界);
2. 脏 bar(缺字段/零振幅零量桩线)不抛、不误标, 索引仍对齐;
3. 不足 25 根 → 空 markers + unknown(不硬凑);
4. lookback 窗口语义(确认根索引绝对值不变);
5. 600111 真实 20 根回放: 10-15 spring_probe 待确认 + stage=C,
   10-13 强阳线不出滞涨标;
6. 文案红线: 全输出无"主力/建仓/出货/吸筹/派发", verdict 常带口径 note。
"""

from __future__ import annotations

from src.core.wyckoff import analyze_wyckoff


def _bar(d, o, h, l, c, v=100.0):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _flat(n, price=10.0, vol=100.0, start=1):
    return [_bar(f"2026-02-{start + i:02d}", price, price + 0.1,
                price - 0.1, price, vol) for i in range(n)]


def _names(res):
    return [(m["date"], m["name"]) for m in res["markers"]]


def _forbid_direction_words(res):
    parts = [m["name"] + "".join(m["basis"]) + m["definition"]
             for m in res["markers"]]
    parts.append(res["verdict"].get("stage_label", ""))
    blob = "".join(parts)
    for w in ("主力", "建仓", "出货", "吸筹", "派发", "建议买", "建议卖"):
        assert w not in blob, f"方向性/建议词 {w} 不许出现在量价结构标注文案里"
    # 免责必须在: 明确不得作主力意图/买卖结论
    assert "不得作为主力意图" in res["verdict"].get("note", "")


def test_放量滞涨_小实体():
    bars = _flat(24)
    # 均量 100, 尾根量 200(2.0×), 涨幅 +0.5%
    bars.append(_bar("2026-03-07", 10.0, 10.2, 9.95, 10.05, 200.0))
    res = analyze_wyckoff(bars)
    assert ("2026-03-07", "放量滞涨") in _names(res)
    hit = next(m for m in res["markers"] if m["code"] == "effort_divergence")
    assert hit["direction"] == "bearish" and hit["index"] == 24
    assert hit["date"] == "2026-03-07"
    assert isinstance(hit["basis"], list) and len(hit["basis"]) >= 1
    _forbid_direction_words(res)


def test_放量滞涨_冲高回落():
    bars = _flat(24)
    # 上影 0.3 ≥1.5×实体 0.1, 收盘下半部, 量 2×
    bars.append(_bar("2026-03-07", 10.0, 10.4, 9.9, 10.1, 200.0))
    res = analyze_wyckoff(bars)
    assert ("2026-03-07", "放量滞涨") in _names(res)


def test_放量大涨_不是滞涨():
    bars = _flat(24)
    bars.append(_bar("2026-03-07", 10.0, 10.5, 9.95, 10.4, 300.0))  # +4%
    res = analyze_wyckoff(bars)
    assert all(m["code"] != "effort_divergence"
               for m in res["markers"] if m["date"] == "2026-03-07")


def test_弹簧试探_收回且回收足():
    bars = _flat(24, price=10.0)
    # 近10日最低约 9.9; 跌破到 9.7, 收回 10.0(回收 3.1%)
    bars.append(_bar("2026-03-07", 9.9, 10.0, 9.7, 10.0, 120.0))
    res = analyze_wyckoff(bars)
    assert ("2026-03-07", "弹簧试探") in _names(res)
    hit = next(m for m in res["markers"] if m["code"] == "spring_probe")
    assert hit["direction"] == "neutral"


def test_弹簧试探_未收回不打标():
    bars = _flat(24, price=10.0)
    bars.append(_bar("2026-03-07", 9.9, 9.95, 9.6, 9.7, 300.0))  # 真破位
    res = analyze_wyckoff(bars)
    assert all(m["code"] != "spring_probe"
               for m in res["markers"] if m["date"] == "2026-03-07")


def test_弹簧试探_回收不足不打标():
    bars = _flat(24, price=10.0)
    # 跌破到 9.85 收回 9.9: 回收 0.5% <1% → 碎步, 不打
    bars.append(_bar("2026-03-07", 9.9, 9.95, 9.85, 9.9, 90.0))
    res = analyze_wyckoff(bars)
    assert all(m["code"] != "spring_probe"
               for m in res["markers"] if m["date"] == "2026-03-07")


def test_缩量沉淀_连续两根标最后一根():
    bars = _flat(24, vol=100.0)
    bars.append(_bar("2026-03-07", 10.0, 10.05, 9.95, 10.0, 50.0))
    bars.append(_bar("2026-03-08", 10.0, 10.05, 9.95, 10.01, 55.0))
    res = analyze_wyckoff(bars)
    got = [m["date"] for m in res["markers"] if m["code"] == "dry_up"]
    assert got == ["2026-03-08"]  # 只标最后一根, 不刷屏


def test_单根缩量_不标沉淀():
    bars = _flat(24, vol=100.0)
    bars.append(_bar("2026-03-07", 10.0, 10.05, 9.95, 10.0, 50.0))
    res = analyze_wyckoff(bars)
    assert "dry_up" not in [m["code"] for m in res["markers"]]


def test_放量突破_创20日新高():
    bars = _flat(24, price=10.0, vol=100.0)
    bars.append(_bar("2026-03-07", 10.3, 10.6, 10.2, 10.5, 200.0))
    res = analyze_wyckoff(bars)
    assert ("2026-03-07", "放量突破") in _names(res)
    assert res["verdict"]["stage"] == "D/E"


def test_无量突破_不标():
    bars = _flat(24, price=10.0, vol=100.0)
    bars.append(_bar("2026-03-07", 10.3, 10.6, 10.2, 10.5, 100.0))
    res = analyze_wyckoff(bars)
    assert all(m["code"] != "markup_break"
               for m in res["markers"] if m["date"] == "2026-03-07")


def test_bar不足25根_不识别():
    res = analyze_wyckoff(_flat(10))
    assert res["markers"] == []
    assert res["verdict"]["stage"] == "unknown"
    assert "不足" in res["verdict"].get("reason", "")


def test_脏bar_跳过不抛索引对齐():
    bars = _flat(24)
    bars.append({"date": "2026-03-07"})  # 全缺
    bars.append(_bar("2026-03-08", 10.0, 10.2, 9.95, 10.05, 200.0))
    res = analyze_wyckoff(bars)  # 26 根, 尾根放量滞涨
    assert ("2026-03-08", "放量滞涨") in _names(res)
    hit = next(m for m in res["markers"] if m["date"] == "2026-03-08")
    assert hit["index"] == 25


def test_零振幅零量桩线_不误标():
    bars = _flat(24)
    bars.append({"date": "2026-03-07", "open": 10.0, "high": 10.0,
                 "low": 10.0, "close": 10.0, "volume": 0})
    bars.append(_bar("2026-03-08", 10.0, 10.2, 9.95, 10.05, 200.0))
    res = analyze_wyckoff(bars)
    assert all(m["date"] != "2026-03-07" for m in res["markers"])


def test_lookback_只返回窗口内但索引绝对():
    bars = _flat(24, vol=100.0)
    bars.append(_bar("2026-03-07", 10.0, 10.2, 9.95, 10.05, 200.0))  # idx24 滞涨
    bars.extend(_flat(5, start=8))  # idx25-29 平淡
    res = analyze_wyckoff(bars, lookback=3)
    assert res["markers"] == []  # 确认根 24 不在后 3 根内
    res2 = analyze_wyckoff(bars, lookback=60)
    assert res2["markers"] and res2["markers"][0]["index"] == 24


def test_600111真实回放_1015弹簧待确认_stageC():
    raw = [
        ("2026-09-10", 38.66, 38.66, 38.10, 38.18, 262646),
        ("2026-09-11", 37.89, 37.90, 36.93, 37.48, 368847),
        ("2026-09-14", 37.48, 37.48, 36.90, 37.12, 217153),
        ("2026-09-15", 37.10, 37.71, 37.03, 37.33, 273317),
        ("2026-09-16", 37.15, 37.75, 36.42, 37.49, 386061),
        ("2026-09-17", 37.05, 37.45, 36.70, 36.73, 309763),
        ("2026-09-18", 36.96, 37.20, 36.71, 36.86, 365381),
        ("2026-09-21", 36.88, 37.90, 36.88, 37.88, 499826),
        ("2026-09-22", 38.08, 38.44, 37.79, 37.90, 394779),
        ("2026-09-23", 37.90, 37.94, 37.37, 37.42, 224927),
        ("2026-09-24", 37.20, 37.44, 36.95, 37.06, 226549),
        ("2026-09-28", 36.91, 36.95, 35.42, 35.57, 405864),
        ("2026-09-29", 35.27, 35.85, 35.26, 35.41, 200359),
        ("2026-09-30", 35.50, 35.59, 35.05, 35.30, 217053),
        ("2026-10-08", 35.35, 35.35, 34.30, 34.50, 334146),
        ("2026-10-09", 34.40, 35.12, 34.00, 35.01, 400806),
        ("2026-10-12", 35.50, 35.83, 34.72, 34.85, 328670),
        ("2026-10-13", 35.23, 36.36, 35.23, 36.07, 652537),
        ("2026-10-14", 35.71, 35.71, 34.77, 34.86, 386567),
        ("2026-10-15", 34.06, 35.14, 33.96, 35.14, 422363),
    ]
    pre = [_bar(f"2026-09-0{i}", 39.0, 39.1, 38.7, 38.9, 250000)
           for i in range(1, 6)]
    bars = pre + [_bar(d, o, h, l, c, v) for d, o, h, l, c, v in raw]
    res = analyze_wyckoff(bars, lookback=60)
    # 10-15 弹簧试探(待确认)
    assert ("2026-10-15", "弹簧试探") in _names(res)
    # 10-13 +3.5% 强阳线不是滞涨 → 不打标(规则诚实)
    assert ("2026-10-13", "放量滞涨") not in _names(res)
    assert res["verdict"]["stage"] == "C"
    assert "缩量企稳" in res["verdict"]["stage_label"]
    assert "get_main_intent" in res["verdict"]["note"]
    _forbid_direction_words(res)


def test_verdict_全平序列_unknown():
    res = analyze_wyckoff(_flat(30, vol=100.0))
    assert res["markers"] == []
    assert res["verdict"]["stage"] == "unknown"
    assert res["verdict"]["effort_result"] == "unknown"
    assert res["verdict"]["note"]
