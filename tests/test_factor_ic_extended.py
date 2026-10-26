"""factor_ic 扩展三件套（2026-10-24）：去极值 / 截面正交化 / Quantile 分层回测。

创新 5「IC 联合选股」后端前置（交接备注 §10：缺 quantile 分层回测 + 正交化，先补 factor_ic.py）。
设计哲学延续 factor_ic：纯本地、样本不足直说、不编数字；零重依赖（不引 numpy/scipy）。
"""
from __future__ import annotations

import pytest

# ──────────────────── 去极值（winsorize_cross_section） ────────────────────


def test_winsorize_clips_extremes_keeps_normal_and_passes_missing_through():
    """@spec FIC-001 极值被截到边界；正常值原样；None/NaN 位置显式保持 None 且不参与统计。"""
    from src.core.factor_ic import winsorize_cross_section

    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0, None, float("nan")]
    out = winsorize_cross_section(values, n_mad=3.0)
    assert out["available"] is True
    assert out["n_input"] == 12 and out["n_valid"] == 10
    # None / NaN 位置显式保持 None（位置对齐，方便调用方 zip 回 symbol）
    assert out["values"][10] is None and out["values"][11] is None
    # 1..9 是正常值，原样返回
    for i in range(9):
        assert out["values"][i] == values[i]
    # 100 是极值，被截到上界（upper 有限且远小于 100）
    assert out["values"][9] == out["upper"]
    assert out["n_clipped"] == 1
    assert out["upper"] < 100.0
    assert out["method"] == "mad"


def test_winsorize_low_side_clipped_and_mad_zero_fallback_marked():
    """@spec FIC-001 下侧极值同样被截；MAD=0 退化截面回退分位截断并在 method 显式标注。"""
    from src.core.factor_ic import winsorize_cross_section

    out = winsorize_cross_section([-500.0, 5.0, 5.1, 4.9, 5.2, 4.8, 5.05, 4.95, 5.15, 5.0])
    assert out["available"] is True
    assert out["values"][0] == out["lower"]
    assert out["n_clipped"] == 1

    # 过半样本同值 → MAD=0 → 分位截断回退（显式标注，不静默、不把截面夹平）
    flat = [2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 3.0, 10.0, 50.0]
    out2 = winsorize_cross_section(flat)
    assert out2["available"] is True
    assert out2["mad"] == 0.0
    assert out2["method"].startswith("quantile_fallback")
    # 回退后正常值没有被夹平（若按 median±0 会全部变成 2.0）
    assert out2["values"][7] == 3.0 and out2["values"][8] > 3.0


def test_winsorize_insufficient_sample_is_explicit():
    """@spec FIC-002 有效值 < min_valid 时 available=False 显式报不足，不硬算。"""
    from src.core.factor_ic import winsorize_cross_section

    out = winsorize_cross_section([1.0, 2.0, None, 4.0])
    assert out["available"] is False
    assert "样本不足" in out["reason"]
    assert out["values"] is None and out["median"] is None
    assert out["n_valid"] == 3


# ──────────────────── 截面正交化（orthogonalize_cross_section） ────────────────────


def _orthogonal_complement(cols: list[list[float]]) -> list[float]:
    """Gram-Schmidt：返回一个与 cols 张成空间正交的向量（构造零投影噪声用）。"""
    basis: list[list[float]] = []
    for c in cols:
        w = [float(x) for x in c]
        for b in basis:
            d = sum(x * y for x, y in zip(w, b)) / sum(x * x for x in b)
            w = [x - d * y for x, y in zip(w, b)]
        if sum(x * x for x in w) > 1e-12:
            basis.append(w)
    v = [float(((i * 37) % 23) - 11) for i in range(len(cols[0]))]
    for b in basis:
        d = sum(x * y for x, y in zip(v, b)) / sum(x * x for x in b)
        v = [x - d * y for x, y in zip(v, b)]
    return v


def test_orthogonalize_residuals_are_orthogonal_to_controls():
    """@spec FIC-003 残差与每个对照因子及截距的内积 ≈ 0；r_squared = 对照解释比例。"""
    from src.core.factor_ic import orthogonalize_cross_section

    # y = 2*c1 - c2 + 5 + noise：噪声与设计空间精确正交（Gram-Schmidt 构造），
    # 因此 OLS 残差必须精确还原噪声、系数必须精确还原 —— 断言不留模糊地带。
    n = 40
    c1 = [float(i) for i in range(n)]
    c2 = [float((i * 7) % 13) - 6.0 for i in range(n)]
    ones = [1.0] * n
    noise = _orthogonal_complement([ones, c1, c2])
    y = [2.0 * c1[i] - 1.0 * c2[i] + 5.0 + noise[i] for i in range(n)]

    out = orthogonalize_cross_section(y, [c1, c2], control_names=["mom", "vol"])
    assert out["available"] is True
    res = out["residuals"]
    assert out["n_dropped"] == 0 and out["n_used"] == n
    assert all(r is not None for r in res)
    # 残差 = noise（线性部分被对照完全吸收，正交噪声原样留下）
    for r, e in zip(res, noise):
        assert abs(r - e) < 1e-9
    # 正交性：sum(res)≈0（对截距）、sum(res*c_j)≈0（对每个对照）
    assert abs(sum(r for r in res if r is not None)) < 1e-9
    for c in (c1, c2):
        assert abs(sum(r * cv for r, cv in zip(res, c))) < 1e-9
    # r_squared = 1 - 噪声平方和/总平方和；系数还原精确
    ss_noise = sum(e * e for e in noise)
    my = sum(y) / n
    ss_tot = sum((v - my) ** 2 for v in y)
    assert abs(out["r_squared"] - (1.0 - ss_noise / ss_tot)) < 1e-9
    assert abs(out["coefficients"]["mom"] - 2.0) < 1e-9
    assert abs(out["coefficients"]["vol"] + 1.0) < 1e-9
    assert abs(out["coefficients"]["intercept"] - 5.0) < 1e-9


def test_orthogonalize_drops_missing_rows_explicitly():
    """@spec FIC-003 缺值整行剔除（listwise），残差同位置显式 None，n_dropped 记录。"""
    from src.core.factor_ic import orthogonalize_cross_section

    y = [1.0, 2.0, None, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
    c = [2.0, 4.0, 6.0, 8.0, None, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0]
    out = orthogonalize_cross_section(y, [c])
    assert out["available"] is True
    assert out["n_input"] == 12 and out["n_used"] == 10 and out["n_dropped"] == 2
    assert out["residuals"][2] is None and out["residuals"][4] is None
    assert out["residuals"][0] is not None
    # y 与 c 完全线性 → 残差 ≈ 0
    for r in out["residuals"]:
        if r is not None:
            assert abs(r) < 1e-9


def test_orthogonalize_insufficient_and_collinear_are_explicit():
    """@spec FIC-004 样本不足 / 对照完全共线 → available=False 带原因；结构错误抛 ValueError。"""
    from src.core.factor_ic import orthogonalize_cross_section

    # 有效行 6 < max(10, 2×(1+1)) = 10
    y = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    c = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
    out = orthogonalize_cross_section(y, [c])
    assert out["available"] is False
    assert "样本不足" in out["reason"] and out["residuals"] is None

    # c2 = 2*c1 完全共线 → 正规方程奇异，不硬算
    c1 = [float(i) for i in range(20)]
    c2 = [2.0 * v for v in c1]
    y2 = [v + 1.0 for v in c1]
    out2 = orthogonalize_cross_section(y2, [c1, c2], control_names=["a", "b"])
    assert out2["available"] is False
    assert "共线" in out2["reason"]

    # 结构错误属调用方 bug → ValueError（与数据条件不足是两类事情）
    with pytest.raises(ValueError):
        orthogonalize_cross_section([1.0, 2.0], [[1.0, 2.0, 3.0]])
    with pytest.raises(ValueError):
        orthogonalize_cross_section([1.0, 2.0], [])


# ──────────────────── Quantile 分层回测（quantile_backtest_core / quantile_backtest） ────────────────────


def _strong_factor_samples(
    dates: int = 6,
    symbols: int = 30,
    *,
    one_word_at: tuple[int, int] | None = None,
) -> list[dict]:
    """构造已知强因子横截面：因子值 i 越大，未来收益越高（各 horizon 同向，单位 %）。

    one_word_at=(symbol_idx, date_idx)：把该样本的 T+1 设为一字涨停（untradable=True，
    前瞻收益 +50% —— 若没被剔除，会显著污染 Top 层均值）。
    """
    out: list[dict] = []
    for d in range(dates):
        day = f"2026101{d + 1}"
        for i in range(symbols):
            is_untradable = one_word_at is not None and (i, d) == one_word_at
            fwd = (
                {h: 50.0 for h in (1, 3, 5)}
                if is_untradable
                else {h: (i - 15) * 0.2 + 0.5 * h for h in (1, 3, 5)}
            )
            out.append({
                "date": day, "symbol": f"{i:06d}", "value": float(i),
                "fwd": fwd, "untradable": is_untradable,
            })
    return out


def test_quantile_backtest_top_beats_bottom_for_strong_factor():
    """@spec FIC-005 已知强因子：Top 层均值 > Bottom 层均值，多空价差 > 0，胜率同向，基准居中。"""
    from src.core.factor_ic import quantile_backtest_core

    m = quantile_backtest_core(_strong_factor_samples())
    assert m["available"] is True
    assert m["n_dates"] == 6 and m["samples_used"] == 6 * 30
    for h in (1, 3, 5):
        blk = m["by_horizon"][h]
        top, bottom = blk["buckets"][-1], blk["buckets"][0]
        # 因子值与收益严格正相关 → Top 必须赢 Bottom（分层分不出强弱 = 实现错）
        assert top["avg_return_pct"] > bottom["avg_return_pct"]
        assert blk["top_minus_bottom"]["avg_spread_pct"] > 0
        assert blk["top_minus_bottom"]["median_spread_pct"] > 0
        assert top["win_rate_pct"] == 100.0
        assert blk["top_minus_bottom"]["win_rate_diff_pp"] > 0
        # 同池无条件基准落在两层之间
        assert bottom["avg_return_pct"] < blk["baseline"]["avg_return_pct"] < top["avg_return_pct"]
    # 胜率梯度钉死：T+1 时 Bottom 全负；T+5 时 +0.5h 项让 Bottom 恰好一半转正
    assert m["by_horizon"][1]["buckets"][0]["win_rate_pct"] == 0.0
    assert m["by_horizon"][5]["buckets"][0]["win_rate_pct"] == 50.0
    # 数值钉死（h=1）：Bottom=-2.0 / 基准=0.4 / Top=2.8
    b1 = m["by_horizon"][1]
    assert abs(b1["buckets"][0]["avg_return_pct"] - (-2.0)) < 1e-6
    assert abs(b1["baseline"]["avg_return_pct"] - 0.4) < 1e-6
    assert abs(b1["buckets"][-1]["avg_return_pct"] - 2.8) < 1e-6


def test_quantile_backtest_reports_baseline_and_exclusion_marks():
    """@spec FIC-006 三件套：各层统计 + 同池无条件基准 + 是否剔除一字涨停显式标注（含口径缺口）。"""
    from src.core.factor_ic import quantile_backtest_core, render_quantile_backtest

    m = quantile_backtest_core(_strong_factor_samples())
    for h in (1, 3, 5):
        base = m["by_horizon"][h]["baseline"]
        assert set(base) >= {"sample_count", "win_rate_pct", "avg_return_pct", "median_return_pct"}
        assert base["sample_count"] == 6 * 30
    assert m["exclude_limit_up"] is True
    assert "一字" in m["exclusion_note"] and "backtest_lab" in m["exclusion_note"]

    text = render_quantile_backtest(m)
    assert "同池无条件基准" in text and "多空价差" in text and "一字" in text

    # 口径缺口路径：untradable=None → 显式「未剔除」，禁止装作已剔除
    samples = _strong_factor_samples()
    for s in samples:
        s["untradable"] = None
    m2 = quantile_backtest_core(samples)
    assert m2["exclude_limit_up"] is False
    assert "口径缺口" in m2["exclusion_note"] and "未剔除" in m2["exclusion_note"]


def test_quantile_backtest_insufficient_reported_explicitly():
    """@spec FIC-007 交易日 <5 或单日横截面 <20 只 → available=False 直说（延续既有门槛）。"""
    from src.core.factor_ic import quantile_backtest_core

    m3 = quantile_backtest_core(_strong_factor_samples(dates=3))
    assert m3["available"] is False
    assert "交易日" in m3["reason"] and m3["by_horizon"] is None

    # 每天只有 10 只票（< 20）→ 全部日期被剔 → 有效交易日 0
    m10 = quantile_backtest_core(_strong_factor_samples(dates=6, symbols=10))
    assert m10["available"] is False
    assert "交易日" in m10["reason"] and "横截面" in m10["reason"]


def test_quantile_backtest_excludes_one_word_limit_up_samples():
    """@spec FIC-008 T+1 一字涨停样本：计入 untradable_skipped，不进任何分层与基准的分子分母。"""
    from src.core.backtest_lab import MIN_SAMPLE
    from src.core.factor_ic import MIN_BUCKET_SAMPLE, quantile_backtest_core

    # 钉住与 backtest_lab 同一套纪律：门槛常量同值
    assert MIN_BUCKET_SAMPLE == MIN_SAMPLE

    samples = _strong_factor_samples(one_word_at=(29, 0))  # symbol 29 在第 1 天 T+1 一字
    m = quantile_backtest_core(samples)
    assert m["untradable_skipped"] == 1
    assert m["samples_used"] == 6 * 30 - 1
    top = m["by_horizon"][1]["buckets"][-1]
    assert top["sample_count"] == 6 * 6 - 1
    # 该样本前瞻收益 +50%，若混入 Top 层均值会到 (100.8+50)/36 ≈ 4.19；
    # 剔除后应为 (6×16.8 − 3.3)/35 = 97.5/35 ≈ 2.7857（输出按 backtest_lab 惯例保留 4 位小数）
    assert top["avg_return_pct"] == round(97.5 / 35, 4)
    assert top["avg_return_pct"] < 3.0
    base = m["by_horizon"][1]["baseline"]
    assert base["sample_count"] == 6 * 30 - 1


def test_quantile_backtest_db_path_reuses_factor_table(monkeypatch):
    """DB 入口复用 compute() 的取数路径（stock_factor_daily × load_qfq_bars），一字剔除同口径生效。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.core.factor_ic import quantile_backtest, render_quantile_backtest
    from src.db.models import Base, StockFactorDaily

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[StockFactorDaily.__table__])
    db = sessionmaker(bind=eng)()

    factor_days = [f"2026101{d}" for d in range(2, 8)]  # 6 个因子日
    all_days = factor_days + [f"2026101{d}" for d in range(8, 10)] + ["20261020", "20261021", "20261022"]
    iso = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"  # noqa: E731

    for d in factor_days:
        for i in range(25):
            db.add(StockFactorDaily(symbol=f"{i:06d}", trade_date=d, momentum_20=float(i)))
    db.commit()

    def fake_bars(sym, market, days=250):
        i = int(sym)
        out = []
        for k, d in enumerate(all_days):
            c = 10.0 + (i - 12) * 0.05 * (k + 1)
            if i == 24 and k == 4:  # (symbol 24, 因子日 k=3=20261015) 的 T+1（k=4）→ 一字涨停
                c = (10.0 + (i - 12) * 0.05 * 4) * 1.06
                out.append({"date": iso(d), "open": c, "high": c, "low": c, "close": c})
            else:
                out.append({
                    "date": iso(d), "open": c * 1.001, "high": c * 1.005,
                    "low": c * 0.995, "close": c,
                })
        return out

    monkeypatch.setattr("src.db.klines_repo.load_qfq_bars", fake_bars)

    m = quantile_backtest(db, "momentum_20")
    assert m["available"] is True
    assert m["n_dates"] == 6 and m["exclude_limit_up"] is True
    assert m["untradable_skipped"] == 1
    assert m["samples_used"] == 25 * 6 - 1
    assert "未使用任何外部调仓数据" in m["data_source"]
    b1 = m["by_horizon"][1]
    assert b1["buckets"][-1]["avg_return_pct"] > 0 > b1["buckets"][0]["avg_return_pct"]
    text = render_quantile_backtest(m)
    assert "同池无条件基准" in text and "已剔除 T+1 一字涨停" in text

    # 未知因子显式拒绝
    bad = quantile_backtest(db, "no_such_factor")
    assert bad["available"] is False and "未知因子" in bad["reason"]
