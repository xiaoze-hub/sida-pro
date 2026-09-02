import logging

from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException
from datetime import datetime
import time as _time

from pydantic import BaseModel, Field

from src.collectors.kline_collector import KlineCollector
from src.models.market import MarketCode

# L4 事件里 wencai 查询的硬超时秒数(两条查询串行, 行情服务不通时不拖垮 summary)
WENCAI_TIMEOUT_S = 10.0

logger = logging.getLogger(__name__)

router = APIRouter()

# 盘口(thsdk 实时快照)硬超时秒数: 行情服务不通时单次可卡 30s, 超时即放弃并显式"无数据"
ORDERBOOK_TIMEOUT_S = 8.0

# 2026-08-20: summary 接口开盘后 20-30s(主力意图逐笔翻页), 与前端并发请求叠加
# 撞 Caddy 30s 反代超时 → 502 Bad Gateway。加 30s 进程内缓存, 单次冷启动后秒回。
_SUMMARY_CACHE: dict = {}
_SUMMARY_TTL = 300.0  # v0.4.8.1: 30s→5min, 冷启动重算20-30s太贵; 技术指标分钟级刷新足够


class KlineItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")
    days: int | None = Field(default=60, description="K线天数")
    interval: str | None = Field(default="1d", description="周期: 1d/1w/1m")


class KlineBatchRequest(BaseModel):
    items: list[KlineItem]


class KlineSummaryItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")


class KlineSummaryBatchRequest(BaseModel):
    items: list[KlineSummaryItem]


def _parse_market(market: str) -> MarketCode:
    # 兼容数据源返回的交易所代码(SH/SZ/BJ 均属 A股 CN 市场)
    if (market or "").upper() in ("SH", "SZ", "BJ"):
        market = "CN"
    try:
        return MarketCode(market)
    except ValueError:
        raise HTTPException(400, f"不支持的市场: {market}")


def _fmt_date(d) -> str:
    """统一日期格式为 YYYY-MM-DD(前端 parseBusinessDay 正则要求带横杠)。

    fallback 联网源(腾讯/新浪/TQ)返回 '20260828' 8位无横杠, PG 路径返回 '2026-08-28'。
    klines 表缺失走 fallback 时, 8位日期若不转横杠 → 前端 parseBusinessDay 全 null
    → 日K被过滤空白(2026-08-30 线上 bug)。
    """
    s = str(d or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _serialize_klines(klines) -> list[dict]:
    return [
        {
            "date": _fmt_date(k.date),
            "open": k.open,
            "close": k.close,
            "high": k.high,
            "low": k.low,
            "volume": k.volume,
        }
        for k in klines
    ]


def _aggregate_klines(klines, interval: str) -> list:
    """Aggregate daily klines to week/month."""

    iv = (interval or "1d").lower()
    if iv in ("1d", "day", "d"):
        return klines
    if iv not in ("1w", "1m", "week", "month", "w", "m"):
        return klines

    parsed = []
    for k in klines or []:
        try:
            dt = datetime.strptime(k.date, "%Y-%m-%d")
        except Exception:
            continue
        parsed.append((dt, k))

    parsed.sort(key=lambda x: x[0])
    buckets: dict[str, list] = {}
    for dt, k in parsed:
        if iv in ("1w", "week", "w"):
            y, w, _ = dt.isocalendar()
            key = f"{y:04d}-W{w:02d}"
        else:
            key = f"{dt.year:04d}-{dt.month:02d}"
        buckets.setdefault(key, []).append((dt, k))

    out = []
    for _, items in buckets.items():
        items.sort(key=lambda x: x[0])
        first = items[0][1]
        last = items[-1][1]
        high = max(it[1].high for it in items)
        low = min(it[1].low for it in items)
        vol = sum(it[1].volume for it in items)
        out.append(
            type(first)(
                date=items[-1][0].strftime("%Y-%m-%d"),
                open=first.open,
                close=last.close,
                high=high,
                low=low,
                volume=vol,
            )
        )
    out.sort(key=lambda k: k.date)
    return out


@router.get("/{symbol}")
def get_klines(symbol: str, market: str = "CN", days: int = 60, interval: str = "1d"):
    """获取单只股票/指数K线数据(指数代码自动识别,走指数K线源)"""
    market_code = _parse_market(market)
    # 指数识别: 已知指数代码 → 走指数K线(支持大盘详情页复用 InteractiveKline)
    from src.web.api.market import MARKET_INDICES

    is_index = any(idx["symbol"] == symbol for idx in MARKET_INDICES)
    if is_index:
        # 云服务器东财必失败(502) → 直接腾讯K线,避免每次白等 10s 超时
        import logging
        _log_k = logging.getLogger(__name__)

        idx_conf = next((i for i in MARKET_INDICES if i["symbol"] == symbol), None)
        tencent_code = idx_conf.get("tencent_symbol", "") if idx_conf else ""
        try:
            # 2026-08-13 修复: 裸 requests.get(timeout=8) 同步阻塞 asyncio 事件循环,
            # 海外节点 web.ifzq.gtimg.cn 偶发连接挂起(43.154.254.x HK CDN) → 事件循环堵死 → 全站无响应。
            # 改走 market_http: 短超时(5s)+ 退避重试, 失败快速抛错(不长时间卡住)。
            from src.collectors.market_http import market_get
            # v0.4.6.1: 腾讯被风控(501)会抛异常 — 单独捕获, 让新浪兜底有机会执行
            try:
                raw_resp = market_get(
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                    host_key="web.ifzq.gtimg.cn",
                    params={"param": f"{tencent_code},day,,,{days},qfq"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=5,
                    retries=1,
                    parse="json",
                    symbol=symbol,
                    log_label="腾讯指数K线",
                )
            except Exception as _tx_err:
                _log_k.warning(f"腾讯指数K线异常({tencent_code}): {_tx_err!r}, 尝试新浪兜底")
                raw_resp = None
            d = raw_resp
            data = (d.get("data") if isinstance(d, dict) else None) or {}
            data = data.get(tencent_code) or {}
            bars = data.get("day") or []
            if not bars and "." not in tencent_code and tencent_code[:2] in ("sh", "sz"):
                # v0.4.6 hotfix: 腾讯对生产云 IP 风控(501) → 新浪指数日K兜底(A股指数)
                import json as _json

                sina_resp = market_get(
                    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                    "CN_MarketData.getKLineData",
                    host_key="money.finance.sina.com.cn",
                    params={"symbol": tencent_code, "scale": "240", "ma": "no",
                            "datalen": str(min(max(days, 1), 1023))},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
                    timeout=8,
                    retries=1,
                    parse="text",
                    symbol=symbol,
                    log_label="新浪指数K线",
                )
                try:
                    sina_rows = _json.loads(sina_resp) if isinstance(sina_resp, str) else (sina_resp or [])
                except Exception:
                    sina_rows = []
                bars = [
                    [r.get("day"), r.get("open"), r.get("close"), r.get("high"),
                     r.get("low"), r.get("volume")]
                    for r in (sina_rows or [])
                    if isinstance(r, dict) and r.get("day")
                ]
            if not bars:
                raise RuntimeError(
                    f"指数K线不可用(腾讯+新浪均失败, {tencent_code})"
                )
            from src.collectors.kline_collector import KlineData

            raw = [
                KlineData(
                    date=b[0],
                    open=float(b[1]),
                    close=float(b[2]),
                    high=float(b[3]),
                    low=float(b[4]),
                    volume=float(b[5]) if len(b) > 5 else 0,
                )
                for b in bars
            ]
            klines = _aggregate_klines(raw, interval)
            return {
                "symbol": symbol,
                "market": market_code.value,
                "days": days,
                "interval": interval,
                "klines": _serialize_klines(klines),
                "is_index": True,
            }
        except Exception as e:
            # ⚠️ 指数K线失败必须显式 fail: 否则会回退到股票K线分支,
            # 导致"上证指数"页面显示平安银行数据(代码 000001 都是它)
            _log_k.error(f"指数K线获取失败({symbol}/{tencent_code}): {e}")
            raise HTTPException(503, f"指数K线不可用({symbol}): {e}")

    # 1. 优先查 PG klines hypertable(快, ~70ms)
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine, text
    try:
        from src.web.database import DB_URL as _DB_URL
        engine = create_engine(_DB_URL, pool_pre_ping=True)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume "
                    "FROM klines "
                    "WHERE symbol=:s AND market=:m AND period='1d' AND source='tencent' "
                    "  AND ts >= :c "
                    "ORDER BY ts ASC"
                ),
                {"s": symbol, "m": market_code.value, "c": cutoff},
            ).fetchall()
        engine.dispose()
        if rows:
            from src.collectors.kline_collector import KlineData
            klines = [
                KlineData(
                    date=str(r[0])[:10],
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5] or 0),
                )
                for r in rows
            ]
            # v0.4.9.2: PG 命中但数据过薄(<30根)视为无效 — 新加股回填失败时只有
            # 几天增量, 必须继续走联网源(含新浪兜底)拿完整历史
            if len(klines) >= min(30, days):
                klines = _aggregate_klines(klines, interval)
                return {
                    "symbol": symbol,
                    "market": market_code.value,
                    "days": days,
                    "interval": interval,
                    "klines": _serialize_klines(klines),
                    "source": "pg_klines_hypertable",
                }
    except Exception:
        # 库表可能不存在(SQLite/老库)或查询失败 → fallback 联网
        pass

    # 2. Fallback: 联网拉 KlineCollector
    collector = KlineCollector(market_code)
    klines = collector.get_klines(symbol, days=days)
    klines = _aggregate_klines(klines, interval)
    return {
        "symbol": symbol,
        "market": market_code.value,
        "days": days,
        "interval": interval,
        "klines": _serialize_klines(klines),
    }


@router.post("/batch")
def get_klines_batch(payload: KlineBatchRequest):
    """批量获取K线数据"""
    if not payload.items:
        return []

    results = []
    for item in payload.items:
        market_code = _parse_market(item.market)
        collector = KlineCollector(market_code)
        days = item.days or 60
        interval = item.interval or "1d"
        klines = collector.get_klines(item.symbol, days=days)
        klines = _aggregate_klines(klines, interval)
        results.append(
            {
                "symbol": item.symbol,
                "market": market_code.value,
                "days": days,
                "interval": interval,
                "klines": _serialize_klines(klines),
            }
        )

    return results


def _tencent_code(code: str) -> str | None:
    """6 位 A 股代码 → 腾讯风格代码(sz/sh 前缀), 无法识别返回 None。
    与 src/core/dark_pool_flow._tencent_code 同口径, 本地实现避免跨模块引用私有函数。"""
    code = (code or "").strip()
    if code[:2].lower() in ("sz", "sh", "bj"):
        return code.lower()
    if code.isdigit() and len(code) == 6:
        if code[0] in ("6", "9") or code.startswith("688"):
            return f"sh{code}"
        if code[0] in ("0", "2", "3"):
            return f"sz{code}"
    return None


def _build_layer_data(symbol: str, market_code: MarketCode) -> dict:
    """P1 图层数据(2026-09-01): gs_signals / fund_flow / events。

    - gs_signals: 全量 GS 交叉序列(收盘定死=confirmed, 末根疑似=待确认)
    - fund_flow: 日级, 长度对齐 klines; dark_net=OHLC 分摊(L1 近似对照项),
      ming_net=当日 big_order_flow 全口径(历史逐日明盘无数据, 显式 null)
    - events: 涨停/跌停(K线自算); 龙虎榜/公告事件待 28 号数据源接入后补

    仅 A 股; 任一字段计算失败独立降级为 None, 不拖垮整体, 不编造。
    """
    out: dict = {"gs_signals": None, "fund_flow": None, "events": None,
                 "orderbook": None, "unlock_levels": None, "chips": None}
    if market_code.value != "CN":
        return out
    try:
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, "CN", days=120)
    except Exception as e:  # noqa: BLE001
        logger.debug("layer_data fetch_bars %s failed: %s", symbol, e)
        bars = []
    if not bars:
        # 2026-09-02 修: bars 为空(多 vendor 全挂)时**不再整体早退**。
        # orderbook / .tck 拆单撤单 / wencai 龙虎榜公告 / 筹码解套盘位 都不依赖 bars,
        # 早退会把它们一起拖成 None → 前端"整页无数据"(2026-09-02 生产实测)。
        # 仅 gs_signals / fund_flow 依赖 bars, 下面各自用 `if bars:` 门控跳过。
        logger.warning("layer_data %s bars 为空: gs_signals/fund_flow 跳过, 其余仍计算", symbol)

    # ⓪ orderbook: 盘口队列 + 托压单(设计稿 §3.1)
    #    优先 .img 离线文件(完整委托队列); 无 .img 时退回 thsdk 实时快照;
    #    都拿不到 → None(显式无数据, 不编造)。
    try:
        from src.core import orderbook_engine as obe

        snap = None
        img_path = obe.find_img_file(symbol, "CN")
        if img_path:
            snaps = obe.load_snapshots_from_img(img_path)
            if snaps:
                snap = snaps[-1]  # 最新一帧
                out["orderbook"] = {**obe.order_book_queue(snap), "img_path": img_path}
        if out["orderbook"] is None:
            # thsdk 实时快照带重试退避, 行情服务不通时单次可卡 30s(实测 -6 超时),
            # 三轮退避就是 90s, 会把 summary 接口拖到反代超时。加硬超时护栏:
            # 超时即放弃并显式"无数据", 绝不阻塞主链路。
            with ThreadPoolExecutor(max_workers=1) as ex:
                # fetch_snapshot 需 thsdk 代码(USZA/USHA), 不是腾讯 sz/sh 风格
                fut = ex.submit(obe.fetch_snapshot, obe.to_ths_code(symbol) or "")
                try:
                    snap = fut.result(timeout=ORDERBOOK_TIMEOUT_S)
                except Exception:  # noqa: BLE001  # 含 TimeoutError / thsdk 异常
                    snap = None
            out["orderbook"] = obe.order_book_queue(snap)
    except Exception as e:  # noqa: BLE001
        logger.debug("orderbook %s failed: %s", symbol, e)
        out["orderbook"] = None

    # ① gs_signals(依赖 bars: bars 空时跳过, 保持 None 由前端显式"无数据")
    if bars:
        try:
            from src.core.gs_strategy import compute_gs_signals

            out["gs_signals"] = compute_gs_signals(bars)
        except Exception as e:  # noqa: BLE001
            logger.debug("gs_signals %s failed: %s", symbol, e)

    # ② fund_flow(日级, 依赖 bars)
    if bars:
        try:
            from src.core.ohlc_dark import allocate_bar

            fund_flow = []
            for b in bars:
                a = allocate_bar(
                    o=b.get("open"), h=b.get("high"), l=b.get("low"),
                    c=b.get("close"), volume=b.get("volume"),
                    date=str(b.get("date", "")),
                )
                fund_flow.append({
                    "date": _norm_date(b.get("date")),
                    "ming_net": None,  # 历史逐日明盘: 无数据(big_order_flow 仅当日)
                    "dark_net": round(a.net, 2) if a else None,
                })
            # 当日明盘(big_order_flow 全口径, 与官方扩展1一致; 失败保持 null)
            try:
                from src.core import dark_l2, dark_split

                tc = _tencent_code(symbol)
                if tc:
                    ticks = dark_l2.fetch_l2_ticks(tc, "thsdk_big_order")
                    ming = dark_split.ming_net_from_big_orders(ticks)
                    if ming["count"] > 0 and fund_flow:
                        fund_flow[-1]["ming_net"] = ming["ming_net"]
            except Exception:  # noqa: BLE001
                pass
            out["fund_flow"] = fund_flow
        except Exception as e:  # noqa: BLE001
            logger.debug("fund_flow %s failed: %s", symbol, e)

    # ③ events(L4 事件标注): 涨停/跌停(K线自算) + 五类真实数据源
    #    kind 取值与前端 InteractiveKline 的 KlineEventKind 一一对应。
    #    任一数据源不可用 → 该类事件为空, 不编造(§5 诚实口径)。
    try:
        events = _build_events(symbol, bars)
        out["events"] = events
    except Exception as e:  # noqa: BLE001
        logger.debug("events %s failed: %s", symbol, e)

    # ④ 解套盘位 + 筹码结构: 复用**标准筹码接口** chip_distribution, 不自算
    #    腾讯当日分价表优先 / 新浪历史分价兜底; 取不到 → None(显式无数据)。
    try:
        from src.core.l4_events import chip_levels, unlock_levels_from_chips

        chips = _to_tencent(symbol)
        out["chips"] = chips
        out["unlock_levels"] = unlock_levels_from_chips(chips) or None
    except Exception as e:  # noqa: BLE001
        logger.debug("chips %s failed: %s", symbol, e)
    return out


def _norm_date(d) -> str | None:
    """日期统一规范化为 'YYYY-MM-DD'。

    2026-09-02 v0.4.55 生产实测: 同一 date 字段在不同数据源下**格式不一致** ——
      - 通达信 TQ 日K  → `20260827`
      - 东财 / 新浪日K → `2026-08-27`
      - .tck 文件名    → `20260827`(由 `_date_from_tck_name` 提取)
    混用会让前端做日期匹配/排序时错乱, 且主源从 TQ 降级到东财时**日期样式会突然变化**。
    故统一在此规范化; 无法识别的输入原样返回(不猜、不补假日期)。
    """
    s = str(d or "").strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _date_from_tck_name(path: str) -> str | None:
    """.tck 文件名里的交易日(YYYYMMDD) → 'YYYY-MM-DD'; 文件名无日期返回 None。

    .tck 是盘后落盘文件(如 `sz002361_20260827.tck`), 文件名带交易日。
    事件日期必须以它为准: 否则沿用 bars 末根日期(今日)会把 8-27 的拆单/撤单
    标成今天, 前端把历史事件画到今日 K 线上(日期错位)。
    """
    import os
    import re

    m = re.search(r"(\d{8})", os.path.basename(path or ""))
    if not m:
        return None
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _build_events(symbol: str, bars: list[dict]) -> list[dict]:
    """汇总 L4 事件: 涨停/跌停(K线自算) + 四类真实数据源.

    数据源与缺数处理(全部"缺即空", 不编造):
      - 涨停/跌停  : bars 自算, 阈值 ±9.8%(与前端 InteractiveKline 同口径)
      - 拆单簇/撤单: .tck 文件(需 PANWATCH_TCK_DIR), 无文件 → 空
      - 龙虎榜/公告: wencai(thsdk), 不可用 → 空
      - 我的买卖点: paper_trading 交割单(DB), 无记录 → 空

    ⚠️ wencai 走 thsdk, 行情服务不通时会卡住(实测单次 30s)。
    summary 接口有 5 分钟缓存, 但仍加硬超时护栏避免拖垮反代。
    """
    events: list[dict] = []

    # (1) 涨停 / 跌停(K线自算)
    from_idx = max(0, len(bars) - 60)
    for i in range(from_idx, len(bars)):
        prev = bars[i - 1] if i > 0 else None
        if not prev or not prev.get("close"):
            continue
        chg = (bars[i]["close"] - prev["close"]) / prev["close"] * 100
        if chg >= 9.8:
            events.append({"date": _norm_date(bars[i].get("date")), "kind": "limit_up", "label": "涨停"})
        elif chg <= -9.8:
            events.append({"date": _norm_date(bars[i].get("date")), "kind": "limit_down", "label": "跌停"})

    from src.core import l4_events

    # (2) 拆单簇 / 撤单异常(.tck)—— 不依赖 bars, bars 为空时按最新 .tck 文件照算
    try:
        from src.core.tdx_tick_parser import parse_tck

        tck_date = str(bars[-1].get("date", "")) if bars else ""
        tck_path = l4_events.find_tck_file(symbol, tck_date) or l4_events.find_tck_file(symbol)
        if tck_path:
            ev_date = _date_from_tck_name(tck_path) or tck_date
            trades, _orders, cancels = parse_tck(tck_path)
            events.extend(l4_events.split_clusters(trades, ev_date))
            events.extend(l4_events.cancel_anomalies(cancels, ev_date))
    except Exception as e:  # noqa: BLE001
        logger.debug(".tck 事件 %s failed: %s", symbol, e)

    # (3) 龙虎榜 / 公告(wencai)—— 加硬超时, 行情服务不通时不拖垮接口
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_wencai_event_pairs, symbol, today)
            try:
                events.extend(fut.result(timeout=WENCAI_TIMEOUT_S))
            except Exception:  # noqa: BLE001  # 含 TimeoutError / thsdk 异常
                pass
    except Exception as e:  # noqa: BLE001
        logger.debug("wencai 事件 %s failed: %s", symbol, e)

    # (4) 我的买卖点: 按用户要求**先不做**(2026-09-01)
    #     交割单是账户级数据, summary 接口不区分 user_id, 多用户会串号;
    #     等接口透传 user_id 后再接。暂不产出 my_trade 事件。

    # 过滤掉日期为空的事件(数据库脏数据不该污染前端)
    return [e for e in events if e.get("date")]


def _wencai_event_pairs(symbol: str, date_: str) -> list[dict]:
    """一次线程里跑完龙虎榜 + 公告两条 wencai 查询(供超时护栏包裹)."""
    from src.core import l4_events

    out: list[dict] = []
    out.extend(l4_events.dragon_tiger_events(symbol, date_))
    out.extend(l4_events.announcement_events(symbol, date_))
    return out


def _to_tencent(symbol: str) -> str:
    """A 股 symbol → 腾讯代码(sh/sz 前缀). 已在 main 项目内, 简单实现即可."""
    s = (symbol or "").strip().lower()
    if not s:
        return s
    if s.startswith(("sh", "sz")):
        return s
    if s.startswith("6"):
        return "sh" + s
    if s.startswith(("0", "3")):
        return "sz" + s
    return s


@router.get("/{symbol}/summary")
def get_kline_summary(symbol: str, market: str = "CN"):
    """获取单只股票K线摘要

    2026-08-20: 加 30s 进程内缓存(主力意图+筹码逐笔翻页冷启动 ~20-30s 撞 502)。
    盘中 30s 内同标的请求命中缓存, 直接秒回。
    """
    market_code = _parse_market(market)
    cache_key = f"summary:{market_code.value}:{symbol}"
    now = _time.time()
    cached = _SUMMARY_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _SUMMARY_TTL:
        return cached[1]
    collector = KlineCollector(market_code)
    summary = collector.get_kline_summary(symbol)
    # 主力意图+筹码(2026-08-11): A股附加, 供前端个股窗口独立展示
    main_intent = None
    main_intent_structured = None
    if market_code.value == "CN":
        try:
            # 2026-08-12 性能优化: 一次 compute_dark_flow 同时产出字符串+结构化,
            # 避免 summary+structured 各调一次(逐笔翻页/分价表各跑一遍)
            from src.agents.intraday_monitor import _main_intent_both
            main_intent, main_intent_structured = _main_intent_both(symbol)
        except Exception:
            try:
                from src.agents.intraday_monitor import _main_intent_summary
                main_intent = _main_intent_summary(symbol)
            except Exception:
                main_intent = None
            try:
                from src.agents.intraday_monitor import _main_intent_structured
                main_intent_structured = _main_intent_structured(symbol)
            except Exception:
                main_intent_structured = None

    # v2.0 §6.2 + A4 派活: A4 dark_clusters 暗盘资金(委托号级拆单簇)纳入 summary 接口,
    # 让前端资金面板的「暗盘为主/还原为辅」双口径可落地。
    # 独立 try/except: 数据源不可用 → available:false 走 §12 兜底规范,不编造。
    dark_clusters: dict = {"available": False, "note": "未接入"}
    try:
        from src.core.postmarket_review import dark_review_from_tck

        review = dark_review_from_tck(symbol)
        dark_ = review.get("dark") or {}
        ming_ = review.get("ming") or {}
        dark_clusters = {
            "available": bool(review.get("available")),
            "dark_net": dark_.get("net"),        # 拆单簇暗盘净额(元)
            "dark_buy": dark_.get("buy"),
            "dark_sell": dark_.get("sell"),
            "cluster_count": len(review.get("clusters") or []),
            "ming_net": ming_.get("net"),        # 明盘(单笔>30万)净额(元)
            "main_net": review.get("main_net"),  # 明+暗主力净额(元)
            "cancel_rate": review.get("cancel_rate"),
            "active_passive_ratio": review.get("active_passive_ratio"),
        }
        if review.get("note"):
            dark_clusters["note"] = review.get("note")
    except Exception as e:  # noqa: BLE001
        logger.debug("dark_clusters summary 失败 %s: %s", symbol, e)

    result = {
        "symbol": symbol,
        "market": market_code.value,
        "summary": summary,
        "main_intent": main_intent,
        "main_intent_structured": main_intent_structured,
        # P1 图层数据(2026-09-01): gs_signals / fund_flow / events
        **_build_layer_data(symbol, market_code),
        # A4 拆单簇暗盘(2026-09-01 接入 summary,前端资金面板双口径展示)
        "dark_clusters": dark_clusters,
    }
    _SUMMARY_CACHE[cache_key] = (now, result)
    return result


@router.post("/summary/batch")
def get_kline_summary_batch(payload: KlineSummaryBatchRequest):
    """批量获取K线摘要"""
    if not payload.items:
        return []

    results = []
    for item in payload.items:
        market_code = _parse_market(item.market)
        collector = KlineCollector(market_code)
        summary = collector.get_kline_summary(item.symbol)
        results.append(
            {
                "symbol": item.symbol,
                "market": market_code.value,
                "summary": summary,
            }
        )

    return results
