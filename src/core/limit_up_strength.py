# -*- coding: utf-8 -*-
"""『首封时间 × 封成比』打板强度因子(2026-10-11 胜率创新 P0-1, **影子运行**)。

打板策略数据核心, TQ 独家(东财无等价字段)。

## 因子口径
    strength = f(首封时间分桶, 封成比分档) − 炸板降档
- **首封时间** = 当日首次封涨停的时刻(取 TQ `get_zdt_data.FirstTimeZT`;
  GP24 首回涨停时间为同类备源)。分四桶: 竞价 / 早盘 / 盘中 / 尾盘。
- **封成比** = 封单额 ÷ 流通市值(封单额取 `get_more_info.limit_up_amount`(元)或
  `get_gb_info` 流通股本 × 涨停价推得; 流通市值取 `get_more_info.Ltsz`)。分四档 C/B/A/S。
- **炸板次数** = TQ `get_zdt_data.OpenTimesZT`(打开次数), 作**降档参考**: ≥ 阈值即降一档。

## 分层标签 S/A/B/C
由「首封桶 × 封成比档」二维矩阵得出(见 `_MATRIX`), 再按炸板次数降档。矩阵语义:
早封 + 大封单 = 强; 尾封 / 小封单 = 弱。

## 边界(诚实铁律)
- 首封时间或封成比**任一缺数据** → `status="no_data"`、`label=None`, 并在 `missing`
  里显式列出缺哪一项 —— **绝不编造**、绝不用缺失值硬凑标签。
- 纯函数、无 I/O; 阈值全部走 `src.core.thresholds` 只读配置层, 禁硬编码。
- 本模块**只产标签/证据, 不是交易建议**(禁"建议买入"类措辞); 消费方(影子落库、
  只读展示)按"仅统计参考"呈现。
"""
from __future__ import annotations

from src.core import thresholds

#: 首封时间桶(时间序)。竞价 = 开盘集合竞价; 早盘 = 开盘后半小时; 盘中; 尾盘。
BUCKETS: tuple[str, ...] = ("竞价", "早盘", "盘中", "尾盘")

#: 封成比档(由弱到强)。
RATIO_GRADES: tuple[str, ...] = ("C", "B", "A", "S")

#: 强度标签(由弱到强)。
LABELS: tuple[str, ...] = ("C", "B", "A", "S")

#: 「首封桶 × 封成比档」→ 强度标签(降档前)。早封 + 大封单最强。
_MATRIX: dict[str, dict[str, str]] = {
    "S": {"竞价": "S", "早盘": "S", "盘中": "A", "尾盘": "B"},
    "A": {"竞价": "S", "早盘": "A", "盘中": "A", "尾盘": "B"},
    "B": {"竞价": "A", "早盘": "B", "盘中": "B", "尾盘": "C"},
    "C": {"竞价": "B", "早盘": "C", "盘中": "C", "尾盘": "C"},
}


def parse_clock(first_time) -> int | None:
    """首封时间 → 自 00:00 起的秒数; 取不到给 None(不猜)。

    接受 `'09:31:05'` / `'093105'` / `'93105'` / `92501`; 越界的时分秒返回 None。
    """
    s = str(first_time or "").strip()
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        if len(parts) < 2:
            return None
        try:
            hh, mm = int(parts[0]), int(parts[1])
            ss = int(parts[2]) if len(parts) > 2 and parts[2] != "" else 0
        except (TypeError, ValueError):
            return None
    elif s.isdigit():
        # 紧凑数字串: 92501→09:25:01(5位) / 093105→09:31:05(6位)
        digits = s.zfill(6)
        if len(digits) != 6:
            return None
        hh, mm, ss = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
    else:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    return hh * 3600 + mm * 60 + ss


def first_seal_bucket(first_time) -> str | None:
    """首封时间 → 桶名(竞价/早盘/盘中/尾盘); 取不到 → None。

    边界(秒, 走 thresholds 配置): ≤ auc_end 竞价; ≤ early_end 早盘;
    ≤ mid_end 盘中; 其余 尾盘。
    """
    sec = parse_clock(first_time)
    if sec is None:
        return None
    if sec <= thresholds.value("first_seal_auc_end"):
        return "竞价"
    if sec <= thresholds.value("first_seal_early_end"):
        return "早盘"
    if sec <= thresholds.value("first_seal_mid_end"):
        return "盘中"
    return "尾盘"


def compute_seal_ratio(seal_amount, circulating_mktcap) -> float | None:
    """封成比 = 封单额 ÷ 流通市值。

    任一分量缺失 / 非正 / 非有限 → None(不猜)。分母 ≤ 0(数据异常)同样 None。
    """
    try:
        amt = float(seal_amount)
        cap = float(circulating_mktcap)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    ratio = amt / cap
    # NaN/inf 兜底(数据源偶发的脏值)
    if ratio != ratio or ratio in (float("inf"), float("-inf")):
        return None
    return ratio


def seal_ratio_grade(ratio) -> str | None:
    """封成比 → 档 C/B/A/S; 比值缺失 → None。

    阈值(走配置): <b → C; [b,a) → B; [a,s) → A; ≥s → S。
    """
    if ratio is None:
        return None
    try:
        v = float(ratio)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    if v < thresholds.value("seal_ratio_b"):
        return "C"
    if v < thresholds.value("seal_ratio_a"):
        return "B"
    if v < thresholds.value("seal_ratio_s"):
        return "A"
    return "S"


def _downgrade(label: str | None) -> str | None:
    """强度标签降一档(C 已是底, 不再降)。"""
    if label is None:
        return None
    idx = LABELS.index(label) if label in LABELS else 0
    return LABELS[max(0, idx - 1)]


def strength_label(
    first_time=None,
    seal_amount=None,
    circulating_mktcap=None,
    open_times=None,
    *,
    ratio: float | None = None,
) -> dict:
    """核心纯函数: 首封时间 × 封成比 (× 炸板参考) → 强度分层标签。

    返回 dict:
      - ``status``: ``"ok"``(两分量齐) / ``"no_data"``(任一缺)
      - ``label``: ``"S"/"A"/"B"/"C"`` 或 ``None``
      - ``label_raw``: 降档前矩阵标签(供回溯; 炸板降档便于对照)
      - ``downgraded``: 是否因炸板被降档
      - ``first_seal_bucket`` / ``seal_ratio`` / ``seal_ratio_grade`` / ``open_times``
      - ``missing``: 缺失分量名列表(``first_time`` / ``seal_ratio``)
      - ``note``: 人类可读口径说明(禁建议措辞)

    ``ratio`` 显式传入时优先于由 ``seal_amount/circulating_mktcap`` 现算(便于消费方直接
    复用已算好的封成比, 如 more-info 的 `limit_up_ratio`)。
    """
    bucket = first_seal_bucket(first_time)
    if ratio is None:
        ratio = compute_seal_ratio(seal_amount, circulating_mktcap)
    grade = seal_ratio_grade(ratio)

    missing: list[str] = []
    if bucket is None:
        missing.append("first_time")
    if grade is None:
        missing.append("seal_ratio")

    ot = _to_int(open_times)

    if missing:
        return {
            "status": "no_data",
            "label": None,
            "label_raw": None,
            "downgraded": False,
            "first_seal_bucket": bucket,
            "seal_ratio": ratio,
            "seal_ratio_grade": grade,
            "open_times": ot,
            "missing": missing,
            "note": "缺数据(" + "/".join(missing) + "), 不出强度标签 —— 不编造",
        }

    assert bucket is not None and grade is not None  # missing 为空时二者必非 None
    raw = _MATRIX[grade][bucket]
    label: str | None = raw
    downgraded = False
    if ot is not None and ot >= thresholds.value("blow_downgrade_open_times"):
        label = _downgrade(raw)
        downgraded = label != raw
    note = (
        f"首封{bucket} × 封成比{grade}({ratio:.2%}) → {label}"
        + (f"; 炸板{ot}次降档({raw}→{label})" if downgraded else "")
        + "。影子运行·仅统计参考, 非交易建议"
    )
    return {
        "status": "ok",
        "label": label,
        "label_raw": raw,
        "downgraded": downgraded,
        "first_seal_bucket": bucket,
        "seal_ratio": ratio,
        "seal_ratio_grade": grade,
        "open_times": ot,
        "missing": [],
        "note": note,
    }


def _to_int(v) -> int | None:
    """炸板次数 → int; 取不到给 None(不猜 0)。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None
