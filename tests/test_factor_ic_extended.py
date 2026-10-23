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
