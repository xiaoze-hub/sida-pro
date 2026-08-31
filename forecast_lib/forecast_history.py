# 历史预测存储(SQLite)
import json
import sqlite3 as _sqlite3

try:
    from .forecast_paths import FORECAST_DB_PATH
except ImportError:  # forecast_server.py 将 forecast_lib 直接加入 sys.path
    from forecast_paths import FORECAST_DB_PATH

_HISTORY_DB = FORECAST_DB_PATH


# ── baostock socket 超时补丁(2026-08-22) ──────────────────────────────────
# 根因: baostock 的 socket connect/recv 均无超时, 服务端半死时 send_msg 永久
# 阻塞 → 预测任务卡在 get_stock_name(bs.login)、/history 卡在
# _fetch_kline_pairs(bs.logout), 线程永不返回(生产实测 py-spy 抓到两个挂死栈)。
# 修法: monkey-patch SocketUtil.connect / get_default_socket, 给 socket 设
# 15s 超时。超时后 baostock 内部抛异常被各调用点的 try/except 吞掉, 返回空,
# 不再永久阻塞。必须在 import baostock 之后、首次调用前打上。
_BAOSTOCK_TIMEOUT_SEC = 15


def patch_baostock_timeout() -> None:
    """给 baostock 所有网络操作加 socket 超时(幂等, 可重复调用)。

    forecast_models.load_kline / forecast_server 搜索接口也直接用 baostock,
    它们 import 本模块时即完成打补丁(forecast_server 顶层 import 了
    forecast_history, 覆盖全部入口)。
    """
    try:
        import baostock.util.socketutil as _su
        import baostock.common.context as _ctx
        import socket as _socket
    except Exception:
        return

    if getattr(_su, "_timeout_patched", False):
        return

    def _connect_with_timeout(self):
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(_BAOSTOCK_TIMEOUT_SEC)
            import baostock.common.contants as _cons
            sock.connect((_cons.BAOSTOCK_SERVER_IP, _cons.BAOSTOCK_SERVER_PORT))
            setattr(_ctx, "default_socket", sock)
        except Exception:
            print("服务器连接失败，请稍后再试。")

    _su.SocketUtil.connect = _connect_with_timeout
    _su._timeout_patched = True


patch_baostock_timeout()



def _init_history_db():
    conn = _sqlite3.connect(_HISTORY_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            stock_name TEXT DEFAULT '',
            last_close REAL,
            last_date TEXT,
            target_date TEXT DEFAULT '',
            pred_days INTEGER,
            direction TEXT,
            expected_pct REAL,
            prediction TEXT,
            action TEXT,
            tone TEXT,
            confidence TEXT,
            target_price REAL,
            stop_loss REAL,
            summary TEXT,
            sentiment_adj REAL,
            models TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 迁移: 旧表无 stock_name/target_date 列则补(ALTER TABLE ADD COLUMN)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(forecasts)").fetchall()]
        if "stock_name" not in cols:
            conn.execute("ALTER TABLE forecasts ADD COLUMN stock_name TEXT DEFAULT ''")
        if "target_date" not in cols:
            conn.execute("ALTER TABLE forecasts ADD COLUMN target_date TEXT DEFAULT ''")
        if "sentiment_notes" not in cols:
            conn.execute("ALTER TABLE forecasts ADD COLUMN sentiment_notes TEXT DEFAULT ''")
        if "capital_flow" not in cols:
            conn.execute("ALTER TABLE forecasts ADD COLUMN capital_flow TEXT DEFAULT ''")
        if "dragon_tiger" not in cols:
            conn.execute("ALTER TABLE forecasts ADD COLUMN dragon_tiger TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    conn.close()



_init_history_db()



def get_stock_name(symbol: str) -> str:
    """查股票名称(baostock query_stock_basic,需带市场前缀,失败返回空)。"""
    try:
        import baostock as bs
        code = f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"
        lg = bs.login()
        if lg.error_code != "0":
            return ""
        rs = bs.query_stock_basic(code=code)
        name = ""
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            # query_stock_basic(code) 返回 [code, code_name, ipoDate, outDate, type, status]
            if len(row) >= 2:
                name = row[1]
        bs.logout()
        return name or ""
    except Exception:
        return ""



def save_forecast(rec: dict):
    """保存一次预测到历史库。"""
    try:
        conn = _sqlite3.connect(_HISTORY_DB)
        conn.execute(
            """INSERT INTO forecasts
               (symbol, stock_name, last_close, last_date, target_date, pred_days, direction, expected_pct,
                prediction, action, tone, confidence, target_price, stop_loss,
                summary, sentiment_adj, sentiment_notes, models, capital_flow, dragon_tiger)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.get("symbol", ""), rec.get("stock_name", ""),
                rec.get("last_close"), rec.get("last_date"),
                rec.get("target_date", ""),
                rec.get("pred_days"), rec.get("direction"), rec.get("expected_pct"),
                json.dumps(rec.get("prediction", []), ensure_ascii=False),
                rec.get("action", ""), rec.get("tone", ""), rec.get("confidence", ""),
                rec.get("target_price"), rec.get("stop_loss"),
                rec.get("summary", ""), rec.get("sentiment_adj"),
                rec.get("sentiment_notes", "[]"),
                json.dumps(rec.get("models", {}), ensure_ascii=False, default=str),
                json.dumps(rec.get("capital_flow", []), ensure_ascii=False),
                json.dumps(rec.get("dragon_tiger", []), ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"保存历史失败: {e}")



def list_forecasts(limit: int = 50, symbol: str = ""):
    """查询历史预测列表(含到期对照 outcome 字段, 2026-08-12 增加)。"""
    conn = _sqlite3.connect(_HISTORY_DB)
    conn.row_factory = _sqlite3.Row
    q = "SELECT * FROM forecasts"
    params: list = []
    if symbol:
        q += " WHERE symbol = ?"
        params.append(symbol)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    for r in rows:
        try:
            r["prediction"] = json.loads(r["prediction"])
        except Exception:
            pass
        _attach_outcome(r)
    return rows


def _attach_outcome(r: dict) -> None:
    """给一条 forecast 记录附加到期对照: outcome_return_pct / outcome_status。

    status: hit(方向对) / miss(方向错) / pending(未到期) / no_data(取不到K线)。
    优先经 8000 /api/klines 取实际行情(与 UI 同源、key/配置实时),
    失败兜底 baostock(预测引擎自带依赖, 不依赖 PanWatch src — 修复
    预测进程 import src.collectors 失败导致 outcome 恒为 no_data 的问题)。
    按 target_date 收盘 vs last_close 判方向。失败/无数据 → no_data(不阻断列表)。
    """
    r.setdefault("outcome_return_pct", None)
    r.setdefault("outcome_status", "pending")
    try:
        from datetime import date
        target_date = str(r.get("target_date") or "")[:10]
        last_close = r.get("last_close")
        direction = r.get("direction")
        if not target_date or not last_close or not direction:
            r["outcome_status"] = "no_data"
            return
        if target_date > date.today().isoformat():
            r["outcome_status"] = "pending"
            return
        symbol = str(r.get("symbol") or "").split(".")[0]
        # 同源 K 线序列取基准价与到期价 → 相对涨跌, 避免复权口径(前复权价 vs 预测时绝对价)导致失真
        pairs = _fetch_kline_pairs(symbol, target_date)
        if not pairs:
            r["outcome_status"] = "no_data"
            return
        last_date = str(r.get("last_date") or "")[:10]
        baseline = None
        actual = None
        for d, c in pairs:
            if d <= last_date:
                baseline = c
            if d <= target_date:
                actual = c
        if baseline is None or actual is None or baseline <= 0 or actual <= 0:
            r["outcome_status"] = "no_data"
            return
        r["outcome_return_pct"] = round((actual / baseline - 1) * 100, 2)
        actual_dir = "up" if actual > baseline else "down" if actual < baseline else "flat"
        if actual_dir == "flat":
            r["outcome_status"] = "pending"  # 平盘视为未定
            return
        r["outcome_status"] = "hit" if actual_dir == direction else "miss"
    except Exception:
        r["outcome_status"] = "no_data"


def _fetch_kline_pairs(symbol: str, end_date: str) -> list:
    """取截至 end_date 的日K (date, close) 列表(升序)。同源用于相对涨跌计算。

    1) 优先经 8000 /api/klines(与 UI 同源; 预测进程配置了 PANWATCH/AUTH
       凭据即自动使用, 否则落 2)
    2) 兜底 baostock(预测引擎自带依赖, 无需任何配置)
    """
    # 1) 8000 K线 API
    try:
        try:
            from .panwatch_client import request_json
        except ImportError:  # forecast_server.py 将 forecast_lib 直接加入 sys.path
            from panwatch_client import request_json
        resp = request_json(f"/api/klines/{symbol}?market=CN&days=90&interval=1d", timeout=15)
        if resp is not None:
            data = resp.get("data", resp) if isinstance(resp, dict) else resp
            items = data.get("klines") if isinstance(data, dict) else data
            if isinstance(items, list):
                pairs = []
                for k in items:
                    d = str(k.get("date", ""))[:10]
                    c = k.get("close")
                    if d and c:
                        pairs.append((d, float(c)))
                if pairs:
                    pairs.sort()
                    return pairs
    except Exception:
        pass
    # 2) baostock 兜底
    try:
        import baostock as bs
        code = f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"
        lg = bs.login()
        try:
            rs = bs.query_history_k_data_plus(
                code, "date,close",
                start_date="1990-01-01", end_date=end_date,
                frequency="d", adjustflag="3",
            )
            pairs = []
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if len(row) >= 2 and row[0] and row[1]:
                    pairs.append((row[0], float(row[1])))
            if pairs:
                return pairs
        finally:
            bs.logout()
    except Exception:
        pass
    return []
