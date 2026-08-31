# 模型层: Kronos / Chronos-Bolt / XGBoost / 线性回归
import os, sys, json, time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from fastapi import HTTPException

# Kronos 路径
KRONOS_ROOT = os.path.expanduser('~/Kronos')
KRONOS_MODEL_PATH = os.path.join(KRONOS_ROOT, 'model')
if os.path.isdir(KRONOS_MODEL_PATH) and os.path.isdir(KRONOS_ROOT):
    sys.path.insert(0, KRONOS_ROOT)
    sys.path.insert(0, KRONOS_MODEL_PATH)


# 全局缓存模型(只加载一次)
_predictor = None
_model_lock = False


def get_predictor():
    """懒加载 Kronos(首次 ~100MB 下载/加载,后续复用)。"""
    global _predictor, _model_lock
    if _predictor is not None:
        return _predictor
    if _model_lock:
        raise HTTPException(503, "模型加载中,请稍候")
    _model_lock = True
    try:
        from model import KronosPredictor, KronosTokenizer, Kronos
        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
        _predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
    finally:
        _model_lock = False
    return _predictor



def load_kline(symbol: str, days: int = 250) -> pd.DataFrame:
    """从 baostock 拉历史日K(不复权),转 Kronos 格式。

    为什么不复权: baostock 前复权对送转股有口径 bug(实测神剑
    116.53 vs 实际 11.70,差复权因子 9.96 倍)。不复权数据与
    真实价格一致,预测基于相对变化,不受影响。

    返回列: timestamp, open, high, low, close, volume, amount
    """
    import baostock as bs

    code = f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"
    lg = bs.login()
    if lg.error_code != "0":
        raise HTTPException(502, f"baostock 登录失败: {lg.error_msg}")

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days * 1.6)).strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(
        code,
        "date,open,high,low,close,volume,amount",
        start_date=start, end_date=end,
        frequency="d", adjustflag="3",  # 3=不复权(前复权有送转口径bug)
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    bs.logout()

    if not rows:
        raise HTTPException(502, f"baostock 无数据: {symbol}")

    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    df["timestamp"] = pd.to_datetime(df["date"])
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().sort_values("timestamp").reset_index(drop=True)
    return df[["timestamp", "open", "high", "low", "close", "volume", "amount"]]



def kronos_predict(df: pd.DataFrame, pred_len: int = 5, n_samples: int = 30):
    """Kronos 蒙特卡洛预测:返回中位数 + P5/P95 区间。"""
    predictor = get_predictor()

    x_df = df[["open", "high", "low", "close", "volume", "amount"]].copy()
    x_ts = pd.Series(df["timestamp"])

    # 未来交易日
    dates = []
    cur = df["timestamp"].iloc[-1]
    while len(dates) < pred_len:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            dates.append(cur)
    y_ts = pd.Series(pd.to_datetime(dates))

    # MC 采样
    preds = []
    for _ in range(n_samples):
        p = predictor.predict(
            df=x_df, x_timestamp=x_ts, y_timestamp=y_ts,
            pred_len=pred_len, T=1.0, top_k=0, top_p=0.9,
            sample_count=1, verbose=False,
        )
        preds.append(p["close"].values)
    arr = np.array(preds)  # (n_samples, pred_len)

    return {
        "median": [round(float(x), 2) for x in np.median(arr, axis=0)],
        "p5": [round(float(x), 2) for x in np.percentile(arr, 5, axis=0)],
        "p95": [round(float(x), 2) for x in np.percentile(arr, 95, axis=0)],
        "n_samples": n_samples,
    }



def xgboost_predict(df: pd.DataFrame, pred_len: int = 5):
    """XGBoost 滚动预测(轻量,作为第二模型)。"""
    import xgboost as xgb
    from sklearn.metrics import mean_absolute_error

    # 特征: 过去 N 日 close 序列
    closes = df["close"].values
    window = 20
    X, y = [], []
    for i in range(window, len(closes)):
        X.append(closes[i - window:i])
        y.append(closes[i])
    X, y = np.array(X), np.array(y)
    if len(X) < 50:
        return None

    # 简单滚动: 训练 80% 预测未来
    split = int(len(X) * 0.8)
    model = xgb.XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.05)
    model.fit(X[:split], y[:split])

    # 滚动预测未来 pred_len 天
    last = closes[-window:]
    preds = []
    for _ in range(pred_len):
        p = model.predict(last.reshape(1, -1))[0]
        preds.append(round(float(p), 2))
        last = np.append(last[1:], p)
    return preds



def linreg_predict(df: pd.DataFrame, pred_len: int = 5):
    """多元线性回归(第三模型,趋势外推)。"""
    from sklearn.linear_model import LinearRegression

    closes = df["close"].values
    n = len(closes)
    X = np.arange(n).reshape(-1, 1)
    model = LinearRegression()
    model.fit(X, closes)
    preds = []
    for i in range(1, pred_len + 1):
        preds.append(round(float(model.predict([[n + i]])[0]), 2))
    return preds


# ════ Chronos-Bolt 全局缓存 ════
_chronos_pipeline = None
_chronos_lock = False


def get_chronos_predictor():
    """懒加载 Chronos-Bolt-small(首次 ~30MB 下载/加载,后续复用,CPU 单次 ~0.06s)。"""
    global _chronos_pipeline, _chronos_lock
    if _chronos_pipeline is not None:
        return _chronos_pipeline
    if _chronos_lock:
        raise HTTPException(503, "Chronos-Bolt 加载中,请稍候")
    _chronos_lock = True
    try:
        import torch
        from chronos.chronos_bolt import ChronosBoltPipeline

        _chronos_pipeline = ChronosBoltPipeline.from_pretrained(
            "amazon/chronos-bolt-small",
            device_map="cpu",
            torch_dtype=torch.float32,
        )
    finally:
        _chronos_lock = False
    return _chronos_pipeline


# ════ TimesFM (Google, 最轻量) 全局缓存 — 2026-08-25 接入替代 Lag-Llama ════
_timesfm_model = None
_timesfm_lock = False


def get_timesfm_predictor():
    """懒加载 TimesFM (Google, CPU ~0.2s, 单变量最轻)。"""
    global _timesfm_model, _timesfm_lock
    if _timesfm_model is not None:
        return _timesfm_model
    if _timesfm_lock:
        raise HTTPException(503, "TimesFM 加载中,请稍候")
    _timesfm_lock = True
    try:
        import timesfm  # pip: timesfm
        # TimesFM 官方: TimesFm(hparams=..., checkpoint=...)
        _timesfm_model = timesfm.TimesFm(
            context_len=512,
            horizon_len=5,
            input_patch_len=32,
            output_patch_len=128,
            num_layers=20,
            model_dims=1280,
            backend="cpu",
        )
        # 加载 checkpoint (首次自动下载)
        _timesfm_model.load_from_checkpoint(repo_id="google/timesfm-1.0-200m")
    except Exception as e:
        _timesfm_lock = False
        raise HTTPException(502, f"TimesFM 加载失败: {e}")
    _timesfm_lock = False
    return _timesfm_model


def timesfm_predict(df: pd.DataFrame, pred_len: int = 5):
    """TimesFM 预测(第5模型, Google 单变量时序基础模型, 最轻量 ~200M)。

    输入 close 单变量，输出 median + p10/p90，与 chronos 口径对齐。
    未安装 timesfm 包时静默返回 None，不阻断投票。
    """
    try:
        model = get_timesfm_predictor()
        closes = df["close"].astype("float32").values
        if len(closes) < 32:
            return None
        # TimesFM 推理: 输入 (1, context_len), 输出 (1, horizon)
        import numpy as np
        freq = [0] * len(closes)  # 日频
        point_forecast, quantiles = model.forecast(
            inputs=[closes],
            freq=freq,
        )
        # point_forecast: (1, pred_len), quantiles 含 p10/p90
        median = [round(float(x), 2) for x in point_forecast[0][:pred_len]]
        # quantiles 形状依版本: 取 0.1/0.9 若有，否则用 median 扩展
        try:
            p10 = [round(float(x), 2) for x in quantiles[0][0][:pred_len]]
            p90 = [round(float(x), 2) for x in quantiles[0][1][:pred_len]]
        except Exception:
            p10, p90 = median, median
        return {"median": median, "p10": p10, "p90": p90, "n_samples": 1}
    except Exception as e:
        print(f"TimesFM 预测失败(未安装或推理异常): {e}")
        return None


def chronos_predict(df: pd.DataFrame, pred_len: int = 5):
    """Chronos-Bolt-small 预测(第4模型,时序基础模型,替代 Lag-Llama)。

    输出 shape (1, 9, pred_len): 9 个分位数 [0.1,0.2,...,0.9],
    直接取 0.5 分位(索引4)为中位数,0.1/0.9 分位为区间。
    输入先按最后收盘价缩放(输出随输入线性,乘回还原),避免量级敏感。
    """
    try:
        import torch

        pipeline = get_chronos_predictor()

        closes = df["close"].astype("float32").values
        if len(closes) < 10:
            return None
        scale = float(closes[-1])
        if scale <= 0:
            scale = 1.0
        inputs = torch.tensor(closes / scale, dtype=torch.float32)

        out = pipeline.predict(inputs=inputs, prediction_length=pred_len)  # (1, 9, pred_len)
        arr = out[0].numpy() * scale  # (9, pred_len) 每行一个分位数路径
        # 分位索引: 0.1→0, 0.5→4, 0.9→8(官方 Chronos-Bolt 固定 9 分位)
        return {
            "median": [round(float(x), 2) for x in arr[4]],
            "p10": [round(float(x), 2) for x in arr[0]],
            "p90": [round(float(x), 2) for x in arr[8]],
            "n_samples": 9,  # 9 个分位路径
        }
    except Exception as e:
        print(f"Chronos-Bolt 预测失败: {e}")
        return None