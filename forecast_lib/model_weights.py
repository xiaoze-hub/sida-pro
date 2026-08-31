"""模型权重动态调整 — 预测质量闭环(后端)

预测引擎 4 模型加权投票(Kronos / Chronos / XGBoost / 线性回归)的权重不再固定,
改为按历史回测命中率自动调整, 形成闭环:

    预测 → 到期验证(prediction_outcome) → 回测(forecast_server._do_backtest)
        → 各模型 accuracy_pct → 权重(命中率平方, 归一化) → predict() 下次生效

数据流:
- load_weights(): 从 ~/.panwatch_forecast.db 的 backtest_results 表读取各模型历史
  命中率(model_hits_json), 按"命中率平方 → 归一化"计算权重; 无任何可用数据时
  回退固定默认 MODEL_WEIGHTS。
  * 聚合所有回测行(而非只看最新一条): backtest_results 是 per-symbol 的, 存在
    1-2 样本的噪声行(如 2026-08-12 的 1 样本回测), 只取最新一条会把噪声当真理;
    跨行聚合 samples/hits 更稳。改为"只看最新一条"只需换 _load_pooled_model_stats。
  * (2026-08-13 修复) 只聚合新体系数据: 仅取 source='runs'/'live' 的行, 忽略
    source='legacy'(旧 LinearRegression 滚动回测, 样本数与命中口径都和新体系
    4 模型不一致)。此前 9 条 legacy 行把 linear_reg 聚合出 554 样本 51.6% 命中,
    是唯一过 MIN_SAMPLES 门槛的模型, 导致最弱的 linreg 权重最高(闭环反转)。
  * (2026-08-13 修复) 小样本贝叶斯收缩: MIN_SAMPLES 由 10 降到 3, 但命中率用
    拉普拉斯平滑 (hits+1)/(samples+2) 向 50% 收缩, 不再直接中性化 —— 小样本
    不再被排除, 但也不会被当真。
- update_weights_after_backtest(backtest_result): 每次回测算完 model_summary 后
  调用, 把最新命中率写回轻量 JSON 文件(~/.panwatch_model_weights.json), 作为
  DB 之外的兜底与审计记录(DB 侧由 save_backtest_result 负责写回, 双写保证闭环)。
  * (2026-08-13 修复) 权重口径与 load_weights 对齐: 直接基于 pooled DB 统计
    (save_backtest_result 已先落库, 最新回测行已含在池子里)计算, 避免单次回测
    小样本噪声; 落盘失败不再静默吞掉, 打印到 stderr 便于排查。

设计要点:
- 命中率平方: 强化优势模型(75% vs 57% → 权重差更大), 但保留全部 4 模型参与。
- 无数据默认 0.25: 某模型没有历史数据(或样本不足)时给中性权重, 不因缺数据被剔除。
- 权重下限 0.08: 即使某模型历史命中率极低也保留最低参与度(防过度拟合/黑天鹅)。
- 样本数下限 MIN_SAMPLES: 少于该样本数的命中率视为噪声, 按"无数据"处理。
- 历史遗留模型名映射: linear_reg → linreg(老代码命名); lag_llama 已被 chronos
  替代且非同源模型, 直接忽略(chronos 在有自身回测数据前用默认权重)。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

# 5 个参与投票的模型(与 forecast_server.predict 的 votes 名称一一对应)
MODEL_NAMES = ["xgboost", "kronos", "chronos", "timesfm", "linreg"]

# 兜底默认权重(无任何历史数据时使用)
DEFAULT_MODEL_WEIGHTS = {"xgboost": 0.35, "kronos": 0.25, "chronos": 0.20, "timesfm": 0.15, "linreg": 0.05}

# 单模型权重下限: 命中率再差也保留最低参与度, 避免某模型被完全剔除
WEIGHT_FLOOR = 0.08

# 样本数下限: 少于该样本数的 accuracy_pct 视为噪声, 按"无数据"给默认权重
# (2026-08-13 由 10 降到 3: 新体系回测数据本就稀少, 10 门槛导致只有 legacy 污染
#  数据达标; 降门槛后小样本命中率用拉普拉斯平滑向 50% 收缩, 不再直接中性化)
MIN_SAMPLES = 3

# 历史遗留模型名 → 当前模型名(lag_llama 已被 chronos 替代, 非同源, 不映射)
LEGACY_NAME_MAP = {"linear_reg": "linreg"}

# 轻量权重落盘文件(update_weights_after_backtest 写, load_weights 兜底读)
WEIGHTS_FILE = os.path.join(os.path.expanduser("~"), ".panwatch_model_weights.json")

try:
    from forecast_paths import FORECAST_DB_PATH  # forecast_lib 在 sys.path(direct 运行)
except ImportError:  # pragma: no cover
    try:
        from forecast_lib.forecast_paths import FORECAST_DB_PATH  # /tmp/PanWatch 在 sys.path
    except ImportError:  # pragma: no cover
        FORECAST_DB_PATH = os.path.join(os.path.expanduser("~"), ".panwatch_forecast.db")

# load_weights() 最近一次判定来源: "default" | "history" | "file"(供日志)
_last_source = "default"


def last_weights_source() -> str:
    """返回最近一次 load_weights() 的权重来源(default/history/file)。"""
    return _last_source


# ---------------------------------------------------------------------------
# DB 读取
# ---------------------------------------------------------------------------

def _load_pooled_model_stats(db_path: str | None = None) -> dict:
    """从 backtest_results 表聚合所有行的 model_hits_json, 按模型合并 samples/hits。

    返回 {model_name: {"samples": int, "hits": int, "accuracy_pct": float}}。
    聚合而非只取最新一条: 最新行可能是 1-2 样本的噪声回测, 跨行合并更稳。

    (2026-08-13 修复) 只聚合新体系回测: source IN ('runs','live')。
    source='legacy' 是旧 LinearRegression 滚动回测(样本口径、模型集都不同),
    参与聚合会把 linear_reg 的 61 样本/行 × 9 行 灌进 linreg, 造成权重闭环反转。
    """
    db_path = db_path or FORECAST_DB_PATH
    if not os.path.exists(db_path):
        return {}
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source, model_hits_json FROM backtest_results "
            "WHERE source IN ('runs','live') ORDER BY id"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return {}

    pooled: dict[str, dict] = {}
    for r in rows:
        try:
            mh = json.loads(r["model_hits_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(mh, dict):
            continue
        for raw_name, s in mh.items():
            name = LEGACY_NAME_MAP.get(raw_name, raw_name)
            if name not in MODEL_NAMES or not isinstance(s, dict):
                continue
            try:
                samples = int(s.get("samples") or 0)
                hits = int(s.get("hits") or 0)
            except (TypeError, ValueError):
                continue
            if samples <= 0:
                continue
            agg = pooled.setdefault(name, {"samples": 0, "hits": 0})
            agg["samples"] += samples
            agg["hits"] += hits
    for agg in pooled.values():
        agg["accuracy_pct"] = round(agg["hits"] / agg["samples"] * 100, 1)
    return pooled


def _has_usable_data(stats: dict) -> bool:
    """是否存在样本数达标(≥MIN_SAMPLES)的模型统计。"""
    return any(
        isinstance(s, dict) and int(s.get("samples") or 0) >= MIN_SAMPLES
        for s in stats.values()
    )


# ---------------------------------------------------------------------------
# 权重算法
# ---------------------------------------------------------------------------

def _compute_weights(stats: dict) -> dict:
    """按"收缩后命中率平方 → 归一化"计算 4 模型权重。

    - 某模型无数据/样本不足: 原始权重给 0.25(中性, 不剔除)
    - 有数据: 命中率先做贝叶斯收缩(拉普拉斯平滑) (hits+1)/(samples+2),
      向 50% 收缩 —— 小样本不会被当真, 大样本趋近真实命中率;
      原始权重 = 收缩命中率 ** 2(强化优势模型)
    - 归一化 + 下限保护(≥0.08)
    """
    raw: dict[str, float] = {}
    for name in MODEL_NAMES:
        s = stats.get(name)
        if isinstance(s, dict) and int(s.get("samples") or 0) >= MIN_SAMPLES:
            try:
                samples = int(s.get("samples") or 0)
                hits = int(s.get("hits") or 0)
            except (TypeError, ValueError):
                samples, hits = 0, 0
            if samples <= 0:
                raw[name] = 0.25
                continue
            # 贝叶斯收缩: (hits+1)/(samples+2), 小样本向 50% 拉回
            acc = max(0.0, min(1.0, (hits + 1.0) / (samples + 2.0)))
            raw[name] = acc ** 2  # 收缩命中率平方, 强化优势模型
        else:
            raw[name] = 0.25  # 无数据默认
    return _normalize(raw)


def _normalize(raw: dict) -> dict:
    """归一化 + 权重下限保护(迭代重分配, 最终和=1)。"""
    total = sum(raw.values())
    if total <= 0:
        return dict(DEFAULT_MODEL_WEIGHTS)
    w = {k: v / total for k, v in raw.items()}
    # 迭代: 低于下限的模型提到 0.08, 差额按比例从高于下限的模型征收
    for _ in range(8):
        below = [k for k in w if w[k] < WEIGHT_FLOOR]
        if not below:
            break
        deficit = sum(WEIGHT_FLOOR - w[k] for k in below)
        above = [k for k in w if w[k] >= WEIGHT_FLOOR]
        above_total = sum(w[k] for k in above)
        if above_total <= 1e-12:  # 全部低于下限: 等分
            share = 1.0 / len(w)
            return {k: round(share, 4) for k in w}
        for k in below:
            w[k] = WEIGHT_FLOOR
        for k in above:
            w[k] -= deficit * w[k] / above_total
    total = sum(w.values())
    if total > 0:
        w = {k: v / total for k, v in w.items()}
    return {k: round(v, 4) for k, v in w.items()}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def load_weights() -> dict:
    """加载模型权重: 历史命中率动态权重, 无数据时回退固定默认。

    优先级: DB backtest_results 聚合统计 → 最近回测落盘文件 → 默认权重。
    """
    global _last_source
    stats = _load_pooled_model_stats()
    if _has_usable_data(stats):
        _last_source = "history"
        return _compute_weights(stats)
    file_weights = _load_weights_file()
    if file_weights is not None:
        _last_source = "file"
        return file_weights
    _last_source = "default"
    return dict(DEFAULT_MODEL_WEIGHTS)


def _load_weights_file() -> dict | None:
    """读轻量权重文件(update_weights_after_backtest 写的兜底/审计记录)。"""
    if not os.path.exists(WEIGHTS_FILE):
        return None
    try:
        with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        w = payload.get("weights")
        if not isinstance(w, dict) or not all(k in w for k in MODEL_NAMES):
            return None
        return {k: float(w[k]) for k in MODEL_NAMES}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def update_weights_after_backtest(backtest_result: dict) -> dict:
    """回测后更新权重: 计算权重并落盘(轻量 JSON 文件)。

    backtest_result: _do_backtest 算出的 model_summary
        {model_name: {"samples": int, "hits": int, "accuracy_pct": float}}
    返回计算出的权重(便于调用方日志)。

    (2026-08-13 修复) 权重口径与 load_weights 完全对齐: 直接基于 pooled DB 统计
    计算(_do_backtest 先调 save_backtest_result, 最新回测行已含在池子里; 若池子
    为空则用本次回测统计兜底), 保证文件落盘 = load_weights 从 DB 读出的同一套
    权重, 双写闭环一致。落盘失败打印 stderr(不再静默吞掉)。
    """
    stats = _canonicalize_stats(backtest_result or {})
    pooled = _load_pooled_model_stats()
    if not pooled:
        pooled = stats
    weights = _compute_weights(pooled)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "backtest",
        "model_stats": pooled,
        "weights": weights,
    }
    try:
        tmp = WEIGHTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, WEIGHTS_FILE)  # 原子替换, 避免写一半
    except OSError as e:  # 落盘失败不阻断回测主流程, 但必须可见
        print(f"[model_weights] 权重文件落盘失败(不影响回测): {e}", file=__import__("sys").stderr)
    return weights


def _canonicalize_stats(backtest_result: dict) -> dict:
    """把回测 model_summary 归一化: 遗留名映射 + 只保留 4 个当前模型。"""
    stats: dict[str, dict] = {}
    for raw_name, s in (backtest_result or {}).items():
        name = LEGACY_NAME_MAP.get(raw_name, raw_name)
        if name not in MODEL_NAMES or not isinstance(s, dict):
            continue
        try:
            samples = int(s.get("samples") or 0)
            hits = int(s.get("hits") or 0)
        except (TypeError, ValueError):
            continue
        if samples <= 0:
            continue
        stats[name] = {
            "samples": samples,
            "hits": hits,
            "accuracy_pct": round(hits / samples * 100, 1),
        }
    return stats


if __name__ == "__main__":
    w = load_weights()
    print(f"权重来源: {last_weights_source()}")
    print(w)
