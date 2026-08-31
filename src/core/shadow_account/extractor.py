"""Shadow Account — 从盈利回合提炼交易规则(移植自 HKUDS/Vibe-Trading, MIT)。

流水线:
    trades_df → FIFO 配对 → 过滤 (pnl > 0) → 特征工程
    → KMeans 聚类 (k 自动 2-5) → 每簇一条规则
    → 结构化 entry_condition dict → 中文自然语言翻译(无 LLM 时 f-string 兜底)

设计约束:
    * 不强制外部行情调用。交割单衍生特征(holding_days, pnl_pct,
      entry_hour/weekday, market)永远离线可用。价格上下文特征
      (entry_rsi14, prior_5d_return)按买入日从 K 线读取,数据不可用时
      NaN → 从特征矩阵剔除。
    * 必须扛住小样本: 盈利回合 < 5 → 显式报错;< 2 簇 → 降级单簇启发规则。
    * 规则是不可变 ShadowRule —— codegen 的唯一输入。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.core.shadow_account.journal import pair_trades_fifo
from src.core.shadow_account.models import PRICE_FEATURES, ShadowProfile, ShadowRule
from src.core.shadow_account.parsers import parse_file, records_to_dataframe

logger = logging.getLogger(__name__)

MIN_PROFITABLE_ROUNDTRIPS = 5
DEFAULT_MAX_RULES = 5
DEFAULT_MIN_SUPPORT = 3
_NUMERIC_FEATURES = ("holding_days", "pnl_pct", "entry_hour", "entry_weekday")
_CATEGORICAL_FEATURES = ("market",)

_PRICE_FEATURES = PRICE_FEATURES
_RSI_PERIOD = 14
_PRIOR_RETURN_WINDOW = 5
# 买入日前置缓冲天数,保证 RSI 预热有足够交易日 bar(跨周末/节假日)。
_PRICE_LOOKBACK_DAYS = 40


# ---------------- Public API ----------------

def extract_shadow_profile(
    journal_path: str | Path,
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
    max_rules: int = DEFAULT_MAX_RULES,
    llm_translator: Any | None = None,
) -> ShadowProfile:
    """从券商交割单文件提取 ShadowProfile。

    Args:
        journal_path: 受支持券商导出的 CSV/Excel。
        min_support: 支撑单条规则的最小盈利回合数。
        max_rules: 返回规则数上限。
        llm_translator: 可选 callable (dict) -> str,把结构化 entry_condition
            翻译成自然语言;None 时用确定性 f-string 兜底。

    Returns:
        ShadowProfile(未持久化 —— 是否保存由调用方决定)。

    Raises:
        ValueError: 盈利回合少于 MIN_PROFITABLE_ROUNDTRIPS。
    """
    path = Path(journal_path)
    fmt, records = parse_file(path)
    if not records:
        raise ValueError(f"No trade records parsed from {path} (format={fmt})")
    trades_df = records_to_dataframe(records)

    roundtrips = pair_trades_fifo(trades_df)
    total = len(roundtrips)
    if total == 0:
        raise ValueError("No complete buy→sell roundtrips found in journal.")

    profitable = [rt for rt in roundtrips if rt["pnl"] > 0]
    if len(profitable) < MIN_PROFITABLE_ROUNDTRIPS:
        raise ValueError(
            f"Insufficient profitable roundtrips: {len(profitable)} "
            f"(need ≥{MIN_PROFITABLE_ROUNDTRIPS}).",
        )

    features_df = _compute_features(profitable, trades_df)
    rules = _extract_rules(
        features_df,
        min_support=min_support,
        max_rules=max_rules,
        llm_translator=llm_translator,
    )

    source_market = _dominant(trades_df["market"])
    preferred_markets = tuple(trades_df["market"].value_counts().index.tolist())
    hold = features_df["holding_days"].dropna()
    typical_holding = (
        round(float(hold.median()), 2) if len(hold) else 0.0,
        round(float(hold.quantile(0.75)), 2) if len(hold) else 0.0,
    )
    date_range = (
        str(trades_df["datetime"].min()),
        str(trades_df["datetime"].max()),
    )
    profile_text = _render_profile_text(
        total_profitable=len(profitable),
        total_all=total,
        typical_holding=typical_holding,
        source_market=source_market,
        preferred_markets=preferred_markets,
    )

    return ShadowProfile(
        shadow_id=_new_shadow_id(),
        created_at=_now_iso(),
        journal_hash=_hash_journal(path),
        source_market=source_market,
        profitable_roundtrips=len(profitable),
        total_roundtrips=total,
        date_range=date_range,
        profile_text=profile_text,
        rules=tuple(rules),
        preferred_markets=preferred_markets,
        typical_holding_days=typical_holding,
    )


# ---------------- 特征工程 ----------------

def _compute_rsi(close: pd.Series, period: int = _RSI_PERIOD) -> pd.Series:
    """因果 Wilder-EWM RSI。

    RSI[t] 只依赖 <= t 的收盘价(因果性)。
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _fetch_price_history(
    symbol: str,
    market: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame | None:
    """按代码拉日 K(经 fork 回测数据适配层)。

    任何失败(无来源/空结果/代码缺失)降级为 None,让调用方剔除价格特征
    而不是 raise。
    """
    if market != "china_a":
        return None
    try:
        from src.core.backtest.data_adapter import load_price_history

        bars = load_price_history(symbol, "CN", days=400)
    except Exception as exc:  # pragma: no cover — loader/network edge cases
        logger.debug("Price fetch failed for %s (%s): %s", symbol, market, exc)
        return None
    if not bars:
        return None
    frame = pd.DataFrame(
        {
            "trade_date": [b.date for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.set_index("trade_date")
    return frame


def _as_of_index(frame: pd.DataFrame, buy_dt: pd.Timestamp) -> pd.DataFrame:
    """把日线价格框切片到 buy_dt 之前已完成的 bar。

    买入日当天的 bar 排除(日内入场时当日收盘价尚不可得)。
    """
    as_of = pd.Timestamp(buy_dt)
    if as_of.tzinfo is not None:
        as_of = as_of.tz_localize(None)
    as_of = as_of.normalize()
    return frame.loc[frame.index < as_of]


def _price_features_as_of(
    frame: pd.DataFrame | None,
    buy_dt: pd.Timestamp,
) -> dict[str, float]:
    """按 buy_dt 从价格框计算价格上下文特征。

    每个值只读买入日之前的已完成日 bar;历史不足则对应特征 NaN。
    """
    out: dict[str, float] = {name: float("nan") for name in _PRICE_FEATURES}
    if frame is None:
        return out

    history = _as_of_index(frame, buy_dt)
    close = history["close"].dropna()
    if close.empty:
        return out

    if len(close) >= _RSI_PERIOD:
        rsi = _compute_rsi(close).iloc[-1]
        out["entry_rsi14"] = float(rsi) if pd.notna(rsi) else float("nan")

    if len(close) >= _PRIOR_RETURN_WINDOW + 1:
        ret = close.pct_change(_PRIOR_RETURN_WINDOW).iloc[-1]
        out["prior_5d_return"] = float(ret) if pd.notna(ret) else float("nan")

    return out


def _attach_price_features(
    rows: list[dict[str, Any]],
) -> None:
    """就地附加价格上下文特征,每股一次批量拉取。

    按 symbol 分组,每股在 [min(buy_dt)-buffer, max(buy_dt)] 窗口拉一次,
    再按各自 buy_dt 读特征(不可用时 NaN)。
    """
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)

    for symbol, sym_rows in by_symbol.items():
        market = sym_rows[0]["market"]
        buy_dts = [pd.Timestamp(r["buy_dt"]) for r in sym_rows]
        end = max(buy_dts)
        start = min(buy_dts) - pd.Timedelta(days=_PRICE_LOOKBACK_DAYS)
        if end.tzinfo is not None:
            end = end.tz_localize(None)
        if start.tzinfo is not None:
            start = start.tz_localize(None)
        frame = _fetch_price_history(symbol, market, start=start, end=end)
        for row in sym_rows:
            row.update(_price_features_as_of(frame, pd.Timestamp(row["buy_dt"])))


def _compute_features(
    roundtrips: list[dict[str, Any]],
    trades_df: pd.DataFrame,
) -> pd.DataFrame:
    """每笔盈利回合算一行特征。

    Columns: symbol, market, holding_days, pnl, pnl_pct, entry_hour,
    entry_weekday, buy_dt, sell_dt, 加价格上下文特征(entry_rsi14,
    prior_5d_return —— 价格数据不可用时 NaN)。
    """
    market_by_symbol = (
        trades_df.drop_duplicates("symbol").set_index("symbol")["market"].to_dict()
    )
    rows: list[dict[str, Any]] = []
    for rt in roundtrips:
        buy_dt = pd.Timestamp(rt["buy_dt"])
        sell_dt = pd.Timestamp(rt["sell_dt"])
        rows.append({
            "symbol": rt["symbol"],
            "market": market_by_symbol.get(rt["symbol"], "other"),
            "holding_days": float(rt["hold_days"]),
            "pnl": float(rt["pnl"]),
            "pnl_pct": float(rt["pnl_pct"]),
            "entry_hour": int(buy_dt.hour),
            "entry_weekday": int(buy_dt.weekday()),
            "buy_dt": buy_dt,
            "sell_dt": sell_dt,
        })
    _attach_price_features(rows)
    return pd.DataFrame(rows)


# ---------------- 聚类 + 规则提取 ----------------

def _promoted_numeric_features(
    features_df: pd.DataFrame,
    *,
    min_support: int,
) -> tuple[str, ...]:
    """用于聚类的数值特征集。

    恒含交割单衍生 _NUMERIC_FEATURES。价格特征仅在至少 min_support 行
    非 NaN 时加入 —— 太稀疏的价格特征被排除,聚类行为与无价格数据基线一致。
    """
    promoted = list(_NUMERIC_FEATURES)
    for name in _PRICE_FEATURES:
        if name in features_df.columns and features_df[name].notna().sum() >= min_support:
            promoted.append(name)
    return tuple(promoted)


def _extract_rules(
    features_df: pd.DataFrame,
    *,
    min_support: int,
    max_rules: int,
    llm_translator: Any | None,
) -> list[ShadowRule]:
    """聚类盈利回合,每个稠密簇派生一条规则。"""
    available_price_features = tuple(
        f for f in _PRICE_FEATURES if f in features_df.columns
    )
    if len(features_df) < min_support:
        return [
            _heuristic_single_rule(
                features_df,
                min_support,
                llm_translator,
                price_features=available_price_features,
            )
        ]

    numeric_features = _promoted_numeric_features(features_df, min_support=min_support)
    promoted_price_features = tuple(
        f for f in numeric_features if f in _PRICE_FEATURES
    )
    cluster_labels = _auto_cluster(
        features_df, max_k=min(max_rules, 5), numeric_features=numeric_features,
    )
    rules: list[ShadowRule] = []
    total_profitable = len(features_df)
    used_markets: set[str] = set()

    for cluster_id in sorted(set(cluster_labels)):
        cluster_mask = cluster_labels == cluster_id
        cluster_df = features_df[cluster_mask]
        if len(cluster_df) < min_support:
            continue
        rule = _cluster_to_rule(
            cluster_df=cluster_df,
            rule_index=len(rules) + 1,
            total_profitable=total_profitable,
            llm_translator=llm_translator,
            price_features=promoted_price_features,
        )
        # 去重近似规则(同 market + 同持仓带)
        key = (rule.entry_condition.get("market"), rule.holding_days_range)
        if key in used_markets:
            continue
        used_markets.add(key)
        rules.append(rule)
        if len(rules) >= max_rules:
            break

    if not rules:
        rules = [
            _heuristic_single_rule(
                features_df,
                min_support,
                llm_translator,
                price_features=promoted_price_features,
            )
        ]
    return rules


def _auto_cluster(
    features_df: pd.DataFrame,
    *,
    max_k: int,
    numeric_features: tuple[str, ...] = _NUMERIC_FEATURES,
) -> np.ndarray:
    """silhouette 启发式选簇数(兜底 k=2)。

    用 z-score 缩放避免单特征主导;价格特征 NaN 用中位数填充(只影响分组,
    不影响规则边界)。
    """
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    numeric_df = features_df[list(numeric_features)].astype(float)
    numeric_df = numeric_df.fillna(numeric_df.median(numeric_only=True))
    numeric_df = numeric_df.dropna(axis=1, how="all")
    numeric = numeric_df.to_numpy()
    if len(numeric) <= 2 or max_k < 2 or numeric.shape[1] == 0:
        return np.zeros(len(numeric), dtype=int)
    scaled = StandardScaler().fit_transform(numeric)

    best_k, best_score = 2, -1.0
    try:
        from sklearn.metrics import silhouette_score

        for k in range(2, min(max_k, len(numeric) - 1) + 1):
            labels = KMeans(n_clusters=k, n_init=5, random_state=42).fit_predict(scaled)
            if len(set(labels)) < 2:
                continue
            score = silhouette_score(scaled, labels)
            if score > best_score:
                best_k, best_score = k, score
    except Exception as exc:  # pragma: no cover — sklearn edge cases
        logger.debug("silhouette selection failed, fallback k=2: %s", exc)

    return KMeans(n_clusters=best_k, n_init=5, random_state=42).fit_predict(scaled)


def _cluster_to_rule(
    *,
    cluster_df: pd.DataFrame,
    rule_index: int,
    total_profitable: int,
    llm_translator: Any | None,
    price_features: tuple[str, ...] = (),
) -> ShadowRule:
    """把一个簇总结为一条 ShadowRule。

    入场条件用 p10–p90 数值边界 + 主导市场。比决策树轻,小样本下保持可解释。
    """
    market = _dominant(cluster_df["market"])
    hold_days = cluster_df["holding_days"]
    hold_lo = max(1, int(round(float(hold_days.quantile(0.10)))))
    hold_hi = max(hold_lo, int(round(float(hold_days.quantile(0.90)))))
    hours = cluster_df["entry_hour"]
    hour_lo = int(round(float(hours.quantile(0.10))))
    hour_hi = int(round(float(hours.quantile(0.90))))

    entry_condition: dict[str, Any] = {
        "market": market,
        "entry_hour": {"min": hour_lo, "max": hour_hi},
    }
    for feature in price_features:
        if feature in cluster_df.columns:
            series = cluster_df[feature].dropna()
            if len(series) >= 2:
                lo = float(round(series.quantile(0.10), 4))
                hi = float(round(series.quantile(0.90), 4))
                entry_condition[feature] = {"min": lo, "max": hi}
    exit_condition: dict[str, Any] = {
        "holding_days": {"min": hold_lo, "max": hold_hi},
    }

    samples = tuple(
        f"{row.symbol}@{pd.Timestamp(row.buy_dt).date().isoformat()}"
        for row in cluster_df.head(3).itertuples(index=False)
    )
    support = int(len(cluster_df))
    coverage = round(support / max(total_profitable, 1), 3)

    human = _translate_rule(
        entry_condition=entry_condition,
        exit_condition=exit_condition,
        holding_range=(hold_lo, hold_hi),
        translator=llm_translator,
    )

    return ShadowRule(
        rule_id=f"R{rule_index}",
        human_text=human,
        entry_condition=entry_condition,
        exit_condition=exit_condition,
        holding_days_range=(hold_lo, hold_hi),
        support_count=support,
        coverage_rate=coverage,
        sample_trades=samples,
    )


def _heuristic_single_rule(
    features_df: pd.DataFrame,
    min_support: int,
    llm_translator: Any | None,
    *,
    price_features: tuple[str, ...] = (),
) -> ShadowRule:
    """聚类/决策树无结果时的退化兜底。

    转发 price_features 让单规则路径带同样的 RSI/收益边界;稀疏数据自然
    退化为纯行为规则。
    """
    return _cluster_to_rule(
        cluster_df=features_df,
        rule_index=1,
        total_profitable=max(len(features_df), min_support),
        llm_translator=llm_translator,
        price_features=price_features,
    )


# ---------------- 自然语言翻译 ----------------

_MARKET_LABELS = {
    "china_a": "A 股",
    "us": "美股",
    "hk": "港股",
    "other": "其他",
}

RULE_TEXT_MAX = 80


def _translate_rule(
    *,
    entry_condition: dict[str, Any],
    exit_condition: dict[str, Any],
    holding_range: tuple[int, int],
    translator: Any | None,
) -> str:
    """结构化规则 dict → 简洁中文句子(≤80 字)。"""
    if translator is not None:
        try:
            text = translator({
                "entry_condition": entry_condition,
                "exit_condition": exit_condition,
                "holding_range": holding_range,
            })
            if isinstance(text, str) and text.strip():
                return text.strip()[:RULE_TEXT_MAX]
        except Exception as exc:  # pragma: no cover — LLM failure, fallback
            logger.warning("LLM rule translator failed, falling back: %s", exc)

    market_label = _MARKET_LABELS.get(entry_condition.get("market", "other"), "其他")
    hour_range = entry_condition.get("entry_hour", {})
    hour_text = ""
    if hour_range:
        lo, hi = hour_range.get("min"), hour_range.get("max")
        hour_text = f"于 {lo} 点" if lo == hi else f"于 {lo}-{hi} 点"
    hold_lo, hold_hi = holding_range
    hold_text = f"持有 {hold_lo}-{hold_hi} 天" if hold_lo != hold_hi else f"持有 {hold_lo} 天"
    return f"{market_label} {hour_text}买入,{hold_text}"[:RULE_TEXT_MAX]


# ---------------- 工具 ----------------

def _dominant(series: pd.Series) -> str:
    """序列众数;平手取第一个。"""
    if series.empty:
        return "other"
    return str(series.value_counts().idxmax())


def _render_profile_text(
    *,
    total_profitable: int,
    total_all: int,
    typical_holding: tuple[float, float],
    source_market: str,
    preferred_markets: tuple[str, ...],
) -> str:
    """构建 Section 1 一段式画像(中文)。"""
    median, p75 = typical_holding
    markets_label = ", ".join(_MARKET_LABELS.get(m, m) for m in preferred_markets[:3])
    source_label = _MARKET_LABELS.get(source_market, source_market)
    return (
        f"你 {total_all} 笔已平仓回合中 {total_profitable} 笔盈利。"
        f"主市场: {source_label}(还活跃于 {markets_label})。"
        f"持仓中位数 {median:.1f} 天;多数仓位在 {p75:.1f} 天内了结。"
    )


def _hash_journal(path: Path) -> str:
    """交割单内容 SHA1(幂等)。"""
    import hashlib

    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _new_shadow_id() -> str:
    import uuid

    return f"shadow_{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
