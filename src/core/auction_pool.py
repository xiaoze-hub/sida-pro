"""竞价异动池 v0.3.2 (2026-08-24, 修正 gap_pct/withdraw_rate 计算口径)。

- fetch_auction_anomaly(market): 拉同花顺集合竞价异动股(默认 CN→沪A), 转 list[dict]
- sync_auction_to_db(records)   : 把异动股写入 DB 表 auction_anomaly_records(供历史追踪)
- get_anomaly_history(symbol)   : 从 DB 查某只股票近 N 天竞价异动历史
- register_cron(scheduler)      : 把"工作日 09:25 竞价异动同步" job 注册到**现有** APScheduler
                                  实例(report_scheduler 的底层调度器), 不新开 scheduler

进程内 30s 缓存(与 main_flow_compare 同思路, 避免每轮监控重复拉取)。

⚠️ 字段口径(2026-08-24 v0.3.2 二次修正 — 推翻 v0.3.1 错误假设):
- 实测 thsdk call_auction_anomaly 返回 6 列: 时间 / 价格 / 总金额 / 代码 / 名称 / 异动类型1。
  - "价格" 列**不是价格**, 而是异动幅度的小数比例 / 撤单率 / 占位 1.0。
  - "总金额" 列恒为 2147483648 (int32 上限占位垃圾), 直接忽略。
- gap_pct / withdraw_rate 由本模块**直接基于 异动类型 + 价格列**推导(无需 klines 昨收):
  * 急速上涨 / 急速下跌 / 大幅高开 / 大幅低开: gap_pct       = 价格列 × 100(round 2)
  * 涨停试盘 / 跌停试盘:                价格列恒为 1.0(占位无信息量) -> gap_pct = None
  * 涨停撤单 / 跌停撤单:                价格列 = 撤单率(0.5~0.9 区间) -> withdraw_rate = 价格列 × 100
  * 其他类型(兜底安全网):             若 |价格列| < 0.21 也按涨跌幅处理 -> gap_pct = 价格列 × 100
                                       否则视为脏数据 -> None
- volume_ratio 数据源不提供, 字段固定 None, API 响应 missing_fields 告知前端。
- v0.3.1 旧版 (价格/昨收-1)*100 二次计算已废弃: 价格列不是价格, 公式不成立。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# market -> thsdk 市场代码
_MARKET_MAP = {
    "": "USHA",
    "CN": "USHA",     # 默认: 沪 A(任务口径)
    "SH": "USHA",
    "USHA": "USHA",
    "SZ": "USZA",
    "USZA": "USZA",
    "BJ": "USTM",
    "USTM": "USTM",
    # 全市场(沪深合并拉取去重)
    "ALL": None,
}

# 2026-08-24 v0.3.2: 仅 volume_ratio 数据源**完全不**提供。
# withdraw_rate 已由"涨停撤单/跌停撤单"类型记录填入, 不再 always-missing。
MISSING_FIELDS: list[str] = ["volume_ratio"]
MISSING_NOTE: str = "thsdk 竞价异动数据源不提供量比,字段固定为空"

_CACHE_TTL = 30.0
_cache: dict[str, tuple[float, list[dict]]] = {}


def clear_cache() -> None:
    """清空进程内缓存(测试 / 运维手动刷新用)。"""
    _cache.clear()


# ── 异动类型分类(用于 gap_pct / withdraw_rate 推导) ────────────────────────
# 4 类: 价格列 = 涨跌幅小数比例(直接 ×100 当 gap_pct)
_GAP_RATIO_TYPES: tuple[str, ...] = ("急速上涨", "急速下跌", "大幅高开", "大幅低开")
# 2 类: 价格列恒为 1.0 占位, 无信息量 -> gap_pct = None
_LIMIT_PROBE_TYPES: tuple[str, ...] = ("涨停试盘", "跌停试盘")
# 2 类: 价格列 = 撤单率小数(0.5~0.9) -> 填到 withdraw_rate
_WITHDRAW_TYPES: tuple[str, ...] = ("涨停撤单", "跌停撤单")
# 其他类型兜底安全网: |价格列| < 此值视为涨跌幅小数比例, 否则视为脏数据(None)
_SAFE_RATIO_ABS: float = 0.21


def _to_records(df) -> list[dict]:
    """DataFrame -> list[dict]。列名兼容映射 + 按异动类型推导 gap_pct/withdraw_rate。

    2026-08-24 v0.3.2 字段口径(基于 thsdk call_auction_anomaly 实测 6 列):
    - "价格" 列**不是价格**: 是异动幅度小数比例(对急速/大幅异动)或撤单率(对撤单类型)
      或占位 1.0(对试盘类型)。
    - "总金额" 列恒为 2147483648 (int32 上限占位), 不读取。
    - gap_pct / withdraw_rate 在本函数内基于 异动类型 + 价格列**直接推导**, 不依赖 klines。
    """
    if df is None or len(df) == 0:
        return []

    norm = {c: c.strip().lower() for c in df.columns}

    def find(*keys: str) -> str | None:
        """按规范化关键词找列(返回原始列名), 找不到返回 None。"""
        for kk in keys:
            for c in df.columns:
                if norm[c] == kk or kk in norm[c]:
                    return c
        return None

    def val(row, col):
        try:
            v = row[col]
            return v.item() if hasattr(v, "item") else v
        except Exception:
            return None

    def num(row, col):
        v = val(row, col)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    to_code = find("代码", "代码代码", "股票代码", "证券代码", "code")
    to_name = find("名称", "股票名称", "证券名称", "name")
    to_price = find("价格", "price", "price_now")
    to_anomaly = find("异动类型1", "异动类型", "anomaly_type", "anomaly")

    records: list[dict] = []
    for _, row in df.iterrows():
        code = val(row, to_code) if to_code else None
        code = _normalize_symbol(str(code)) if code is not None else None

        name = val(row, to_name) if to_name else None
        price_raw = num(row, to_price) if to_price else None
        anomaly_type = str(val(row, to_anomaly)) if to_anomaly else ""
        atype = anomaly_type or ""

        # ── gap_pct / withdraw_rate 直接推导(2026-08-24 v0.3.2 真实口径) ─────
        gap_pct: float | None = None
        withdraw_rate: float | None = None

        if any(kw in atype for kw in _GAP_RATIO_TYPES):
            # 急速涨跌 / 大幅高低开: 价格列 = 涨跌幅小数比例
            if price_raw is not None:
                gap_pct = round(price_raw * 100.0, 2)
        elif any(kw in atype for kw in _LIMIT_PROBE_TYPES):
            # 涨停/跌停试盘: 价格列恒为 1.0 占位 -> gap_pct = None
            gap_pct = None
        elif any(kw in atype for kw in _WITHDRAW_TYPES):
            # 涨停/跌停撤单: 价格列 = 撤单率小数 -> withdraw_rate
            if price_raw is not None:
                withdraw_rate = round(price_raw * 100.0, 2)
        else:
            # 其他类型(兜底安全网): |价格列| < 0.21 才按涨跌幅处理, 否则 None
            if price_raw is not None and -_SAFE_RATIO_ABS < price_raw < _SAFE_RATIO_ABS:
                gap_pct = round(price_raw * 100.0, 2)

        rec = {
            "code": code,
            "symbol": code,
            "name": str(name) if name is not None else "",
            "gap_pct": gap_pct,
            "withdraw_rate": withdraw_rate,
            "volume_ratio": None,
            # 内部字段(供调试 / 测试断言可见)
            "price_raw": price_raw,
            "anomaly_type": atype,
        }
        # 保留其余原始字段(去掉已提取的, 避免重复), 归一化列名以避免重复列冲突。
        # 总金额(int32 上限占位垃圾)也归入 skip_norm, 不进 record。
        seen = set()
        skip_norm = {
            "代码", "名称", "价格", "price", "异动类型1", "异动类型", "总金额",
        }
        for c in df.columns:
            k = norm.get(c, c).replace("_", "")
            if k in skip_norm:
                continue
            if k in seen:
                k = f"{k}_{len(seen)}"
            seen.add(k)
            rec[k] = val(row, c)
        if code is not None:
            records.append(rec)
    return records


def _normalize_symbol(raw: str) -> str:
    """把竞价异动返回的代码归一化为 6 位 A 股代码(去除交易所后缀 / thsdk 前缀)。

    例: "USZA002361" / "002361.SZ" / "002361" / "sh600000" -> 6 位数字代码。
    归一化失败(无法识别)则原样返回(由上层容错)。
    """
    s = (raw or "").strip().upper()
    # thsdk 前缀: USZA/USHA/USTM
    for prefix in ("USZA", "USHA", "USTM"):
        if s.startswith(prefix):
            tail = s[len(prefix):]
            if tail.isdigit() and len(tail) == 6:
                return tail
            break
    # 交易所后缀: 002361.SZ / 600000.SH / 830001.BJ
    if "." in s:
        base = s.split(".")[0]
        if base.isdigit() and len(base) == 6:
            return base
    # 纯 6 位数字
    if s.isdigit() and len(s) == 6:
        return s
    # tencent 前缀: sh600000 / sz002361
    if len(s) == 8 and s[:2] in ("SH", "SZ") and s[2:].isdigit():
        return s[2:]
    return raw


def fetch_auction_anomaly(market: str = "CN") -> list[dict]:
    """拉取竞价异动池(30s 缓存)。market: CN/SH/SZ/BJ/ALL 或 thsdk 代码。

    数据源不可用(thsdk 未安装 / 调用异常)时返回 [] 并记日志, 不抛异常(供 cron/API 降级)。

    2026-08-24 v0.3.2: gap_pct / withdraw_rate 在 _to_records 内基于 异动类型 + 价格列
    直接推导(无需 klines 二次计算), volume_ratio 固定 None(数据源不提供)。
    """
    norm_market = (market or "CN").strip().upper()
    thsdk_market = _MARKET_MAP.get(norm_market, "USHA")

    cache_key = f"{norm_market}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        markets = [thsdk_market]
        if thsdk_market is None:  # ALL
            markets = ["USHA", "USZA"]

        from data_source.thsdk_l2 import get_auction_anomaly

        all_records: list[dict] = []
        seen_codes: set[str] = set()
        for m in markets:
            df = get_auction_anomaly(m)
            for rec in _to_records(df):
                sym = rec.get("symbol")
                if not sym:
                    continue
                if sym in seen_codes:
                    # 同一股票多条异动时保留信息量更大的一条:
                    # 撤单(有撤单率) > 涨跌停试盘(占位无信息) > 其他
                    _RANK = {"撤单": 2, "试盘": 0}
                    prev = next(r for r in all_records if r.get("symbol") == sym)
                    def _rank(rec: dict) -> int:
                        t = str(rec.get("anomaly_type") or "")
                        return max((v for k, v in _RANK.items() if k in t), default=1)
                    if _rank(rec) > _rank(prev):
                        all_records[all_records.index(prev)] = rec
                    continue
                seen_codes.add(sym)
                all_records.append(rec)
    except Exception as e:  # noqa: BLE001 - 数据源不可用统一降级
        logger.warning("[auction_pool] 竞价异动拉取失败 market=%r: %r", market, e)
        all_records = []

    _cache[cache_key] = (time.time(), all_records)
    return all_records


def sync_auction_to_db(records: list) -> int:
    """把异动股写入 DB 表 auction_anomaly_records。返回入库条数(0 表示无数据/失败)。"""
    if not records:
        return 0
    try:
        from src.web.database import SessionLocal, acquire_write
        from src.web.models import AuctionAnomalyRecord

        lock = acquire_write()
        try:
            db = SessionLocal()
            try:
                for r in records:
                    db.add(
                        AuctionAnomalyRecord(
                            symbol=str(r.get("symbol") or r.get("code") or "")[:16],
                            name=str(r.get("name") or "")[:64],
                            gap_pct=r.get("gap_pct"),
                            withdraw_rate=r.get("withdraw_rate"),
                            volume_ratio=r.get("volume_ratio"),
                            note=str(r.get("note") or "")[:255],
                        )
                    )
                db.commit()
                return len(records)
            finally:
                db.close()
        finally:
            lock.release()
    except Exception as e:  # noqa: BLE001 - DB 写入失败不崩
        logger.error("[auction_pool] 竞价异动入库失败: %r", e)
        return 0


def get_anomaly_history(symbol: str, days: int = 5) -> list[dict]:
    """从 DB 查询某只股票近 N 天竞价异动历史(按时间倒序, 最多 200 条)。"""
    from datetime import datetime, timedelta

    from src.web.database import SessionLocal
    from src.web.models import AuctionAnomalyRecord

    sym = (symbol or "").strip()
    if not sym:
        return []
    days = max(1, min(int(days or 5), 90))
    since = datetime.now() - timedelta(days=days)

    db = SessionLocal()
    try:
        rows = (
            db.query(AuctionAnomalyRecord)
            .filter(
                AuctionAnomalyRecord.symbol == sym,
                AuctionAnomalyRecord.created_at >= since,
            )
            .order_by(AuctionAnomalyRecord.created_at.desc())
            .limit(200)
            .all()
        )
        return [
            {
                "symbol": r.symbol,
                "name": r.name,
                "gap_pct": r.gap_pct,
                "withdraw_rate": r.withdraw_rate,
                "volume_ratio": r.volume_ratio,
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


def register_cron(scheduler) -> bool:
    """把竞价异动同步 job 注册到**传入的现有** APScheduler 实例(禁止新开 scheduler)。

    在 server.py 的 lifespan 里, report_scheduler.start() 之后调用本函数, 把
    工作日 09:25:00 的竞价异动入库 worker 挂到该调度器的底层 APScheduler 上。
    调度器尚未 start / 传入 None -> 返回 False, 不崩。
    """
    if scheduler is None or not hasattr(scheduler, "add_job"):
        return False

    def _auction_sync_once():
        from src.core.auction_pool import fetch_auction_anomaly, sync_auction_to_db

        try:
            recs = fetch_auction_anomaly("CN")
            n = sync_auction_to_db(recs)
            logger.info("[auction] 竞价异动同步完成: %d 条入库", n)
        except Exception as e:  # noqa: BLE001
            logger.error("[auction] 竞价异动同步异常: %r", e)

    try:
        scheduler.add_job(
            _auction_sync_once,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute=25,
            id="auction_anomaly_daily_sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("[auction] 竞价异动 job 已注册: 工作日 09:25 (%s)", scheduler.timezone)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[auction] 竞价异动 job 注册失败: %r", e)
        return False
