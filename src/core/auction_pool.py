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
- volume_ratio 数据源(thsdk)不提供 → 基础值为 None; TQ 逐票增强成功后由 `OpenVolPre1`
  归一化填充(2026-10-18: 该字段量纲无文档, 只在落进 0.3~30 的合理区间才落值, 否则置空并给
  `volume_ratio_note` 说明 —— 旧实现把原值当倍率透传, 生产上出现 163,513 倍的假量比)。
- v0.3.1 旧版 (价格/昨收-1)*100 二次计算已废弃: 价格列不是价格, 公式不成立。
"""
from __future__ import annotations

import logging
import math
import re
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

#: 最近一次拉取失败原因(None=最近一次成功)。用于把"空池"拆成可诊断的 reason:
#: 数据源断链 / 非竞价时段 / 今日真的无异动(A-03 要求三义不能合一)。
_LAST_FETCH_ERROR: str | None = None


def clear_cache() -> None:
    """清空进程内缓存(测试 / 运维手动刷新用)。"""
    global _LAST_FETCH_ERROR
    _cache.clear()
    _LAST_FETCH_ERROR = None


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

    例: "USZA002361" / "USHT603922" / "002361.SZ" / "002361" / "sh600000" -> 6 位数字。
    归一化失败(无法识别)则原样返回(由上层容错)。

    2026-10-18 P0(审计 F19): 旧实现只认 USZA/USHA/USTM 三个硬编码前缀, 沪市
    USHT 前缀直接漏到页面上(竞价表整列 `USHT603922`, 破坏复制/跳转/搜索),
    且 TQ 逐票增强按 `len(sym)==6` 跳过这些行 —— 量比/竞价额因此整列为空。
    改为通用 `US + 两位交易所码 + 6 位数字`。
    """
    s = (raw or "").strip().upper()
    # thsdk 前缀: USZA(深A)/USHA(沪A)/USHT(沪)/USBJ(北)/USTM ...
    m = re.match(r"^US[A-Z]{2}(\d{6})$", s)
    if m:
        return m.group(1)
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


# ── 竞价量比量纲(2026-10-18 P0, 审计 §4.2) ────────────────────────────────
# TQ `OpenVolPre1` 无单位文档, 生产实测原值 857 ~ 163,513: 直接当倍率展示就是
# 「163,513 倍量比」—— 全站最像真数据、实际最不可用的一列。
# 实战量比合理区间 0.3 ~ 30, 归一规则(全批同一量纲, 不逐行换算法 —— 否则同一列
# 混着 bp/pct 两种倍率, 又是另一种量纲错乱):
#   ① 原值已在 [0.3, 30]  → 认定已是倍率(少数源直接给倍率);
#   ② 原值 / 10000 落区间  → 万分位(主假设: 857~163,513 → 0.09~16.4);
#   ③ 都落不进            → 置空 + 原因(宁缺, 不给 16 万倍的假量比)。
AUCTION_VR_MIN = 0.3
AUCTION_VR_MAX = 30.0
AUCTION_VR_BP_DIVISOR = 10000.0


def normalize_auction_volume_ratio(raw) -> tuple[float | None, str | None]:
    """`OpenVolPre1` → (量比, 采用的量纲标签); 无法落进 0.3~30 则 (None, 原因)。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "invalid"
    if not math.isfinite(value) or value <= 0:
        return None, "invalid"
    if AUCTION_VR_MIN <= value <= AUCTION_VR_MAX:
        return round(value, 2), "ratio"
    scaled = value / AUCTION_VR_BP_DIVISOR
    if AUCTION_VR_MIN <= scaled <= AUCTION_VR_MAX:
        return round(scaled, 2), "bp"
    return None, "out_of_range"



def fetch_auction_anomaly(market: str = "CN") -> list[dict]:
    """拉取竞价异动池(30s 缓存)。market: CN/SH/SZ/BJ/ALL 或 thsdk 代码。

    数据源不可用(thsdk 未安装 / 调用异常)时返回 [] 并记日志, 不抛异常(供 cron/API 降级)。

    2026-08-24 v0.3.2: gap_pct / withdraw_rate 在 _to_records 内基于 异动类型 + 价格列
    直接推导(无需 klines 二次计算), volume_ratio 固定 None(数据源不提供)。
    """
    global _LAST_FETCH_ERROR
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
        _LAST_FETCH_ERROR = None
    except Exception as e:  # noqa: BLE001 - 数据源不可用统一降级
        logger.warning("[auction_pool] 竞价异动拉取失败 market=%r: %r", market, e)
        _LAST_FETCH_ERROR = f"{type(e).__name__}: {e}"
        all_records = []

    # ── 通达信 TQ 盘口增强: 补齐缺失的 涨幅、量比、竞价金额、撤单率与妖股评分 ──
    try:
        from marketdata.vendors.tq import _rpc
        from src.core.demon_factors import top_demon_factors
        demon_map = top_demon_factors(100) or {}

        for r in all_records:
            sym = r.get("symbol")
            if not sym or len(sym) != 6:
                continue
            tq_code = f"{sym}.SH" if sym.startswith("6") else f"{sym}.SZ"
            info = _rpc("get_more_info", {"stock_code": tq_code})
            if isinstance(info, dict):
                # 1. 竞价真实涨幅 (解决截图全部显示 -- 的核心痛点: OpenZAF 是开盘竞价涨跌幅)
                if r.get("gap_pct") is None and info.get("OpenZAF") is not None:
                    try:
                        r["gap_pct"] = round(float(info["OpenZAF"]), 2)
                    except Exception:
                        pass
                elif r.get("gap_pct") is None and info.get("ZAF") is not None:
                    try:
                        r["gap_pct"] = round(float(info["ZAF"]), 2)
                    except Exception:
                        pass

                # 2. 竞价量比 (OpenVolPre1: 该字段量纲无文档 → 归一到 0.3~30 才落值)
                vr = info.get("OpenVolPre1")
                if vr is not None:
                    value, label = normalize_auction_volume_ratio(vr)
                    r["volume_ratio"] = value
                    if value is None:
                        # 溯源信息: 原始值 + 置空原因(不落 DB 列, 只随响应给排障用)
                        r["volume_ratio_raw"] = vr
                        r["volume_ratio_note"] = f"OpenVolPre1={vr} 量纲异常({label}), 已置空"
                    else:
                        r["volume_ratio_scale"] = label

                # 3. 竞价真实成交金额 (OpenAmo, 单位元)
                amo = info.get("OpenAmo")
                if amo is not None:
                    try:
                        r["open_amount"] = round(float(amo), 2)
                    except Exception:
                        pass

                # 4. 真实撤单率 (解决撤单率全部显示 --: BCancel / (TotalBVol + BCancel))
                bc = float(info.get("BCancel") or 0.0)
                tb = float(info.get("TotalBVol") or 0.0)
                if bc > 0 and (tb + bc) > 0:
                    r["withdraw_rate"] = round((bc / (tb + bc)) * 100.0, 1)

                # 5. 涨停封单金额
                if info.get("OpenZTBuy"):
                    try:
                        r["open_zt_buy"] = round(float(info["OpenZTBuy"]), 2)
                    except Exception:
                        pass

            # 6. 妖股基因匹配
            dfactor = demon_map.get(sym)
            if dfactor:
                r["demon_score"] = dfactor.get("total")
                r["demon_grade"] = dfactor.get("grade")
                r["demon_streak"] = dfactor.get("max_streak")

            # 7. 盘口意图判定(只给事实标签, 不给买卖建议 / 不做"出货"定性)
            gap = r.get("gap_pct") or 0.0
            open_amt = r.get("open_amount") or 0.0
            v_ratio = r.get("volume_ratio") or 0.0
            w_rate = r.get("withdraw_rate") or 0.0

            if gap >= 9.8 and (open_amt > 1e7 or r.get("open_zt_buy", 0) > 2e7):
                r["verdict_tag"] = "👑 涨停抢筹"
                r["verdict_action"] = "顶级游资一字顶板，买盘坚决，注意龙头身位"
                r["verdict_level"] = "strong_buy"
            elif gap >= 2.5 and open_amt >= 1.5e7 and v_ratio >= 2.5:
                r["verdict_tag"] = "🔥 爆量抢筹"
                r["verdict_action"] = "主力资金超预期高开抢筹，量能充沛，重点关注开盘秒板"
                r["verdict_level"] = "buy"
            elif gap >= 1.0 and v_ratio >= 3.0 and open_amt >= 8e6:
                r["verdict_tag"] = "🛡️ 弱转强"
                r["verdict_action"] = "竞价量比急剧放大，分歧后弱转强，观察承接低吸"
                r["verdict_level"] = "buy"
            elif w_rate >= 40.0 and gap > 2.0:
                r["verdict_tag"] = "⚠️ 诱多试盘"
                r["verdict_action"] = "买盘撤单率过高，存在假托单掩护开盘派发的可能"
                r["verdict_level"] = "warning"
            elif gap <= -3.0 and open_amt >= 1e7:
                r["verdict_tag"] = "⚡ 恐慌砸盘"
                r["verdict_action"] = "大资金竞价核按钮抢跑，回避风险，勿盲目抄底"
                r["verdict_level"] = "risk"
            else:
                r["verdict_tag"] = "常态试盘"
                r["verdict_action"] = "竞价表现平稳，等待开盘 9:30 确立方向"
                r["verdict_level"] = "neutral"

        # 排序优化: 告别杂毛，按【竞价金额】从大到小降序排列！主力真金白银买的优先排在前面！
        all_records.sort(
            key=lambda x: (
                1 if x.get("verdict_level") in ("strong_buy", "buy") else 0,
                x.get("open_amount") or 0.0,
                x.get("gap_pct") or -99.0,
            ),
            reverse=True,
        )
    except Exception as tq_err:
        logger.debug("[auction_pool] 通达信 TQ 竞价量比补齐跳过: %s", tq_err)

    _cache[cache_key] = (time.time(), all_records)
    return all_records


def _last_sync_at():
    """最近一次竞价池落库时刻(DB)。读不到(未初始化/无表/无行)返回 None, 不抛。"""
    try:
        from src.db.models import AuctionAnomalyRecord
        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            row = (
                db.query(AuctionAnomalyRecord)
                .order_by(AuctionAnomalyRecord.created_at.desc())
                .first()
            )
            return getattr(row, "created_at", None) if row else None
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - 元数据缺失不能拖垮数据本身
        logger.debug("[auction_pool] 读取最近同步时刻失败: %r", e)
        return None


def _source_clock(records: list[dict]) -> str | None:
    """源端自带的时间列(thsdk 竞价异动 6 列里的"时间"), 原样返回不做格式化。

    这是判断"这份池子到底是哪一刻的"唯一一手证据; 源不给就如实 None, 不拿 now() 顶替。
    """
    for r in records or []:
        for k in ("时间", "time", "timestamp", "Time"):
            v = r.get(k)
            if v not in (None, ""):
                return str(v)
    return None


def fetch_auction_payload(market: str = "CN", *, now=None) -> dict:
    """竞价池**带数据日期/交易阶段**的响应体(2026-10-18 审计 A-03 P0 修复)。

    原响应 `{available,count,records,missing_fields,note}` 缺 `trade_date/as_of/is_live`,
    前端只能用 `new Date()`(浏览器时钟)渲染"同步 HH:MM:SS" —— 周日打开会把上周五 9:25
    的池子说成"刚刚同步"。本函数把这些语义补齐:

    - `data_date`     : 池子所属交易日(非交易日回看最近交易日; 推不出就 None)
    - `trade_phase`   : 当前交易阶段(non_trading_day/pre_open/auction/continuous/post_close…)
    - `is_live`       : **仅竞价窗口内(09:15-09:25)且数据就是今天的** 才为 True
    - `as_of`         : **服务端**组装时刻(替代浏览器时钟)
    - `data_clock`    : 源端自带时间(如 "09:25:03"), 没有就 None
    - `reason`        : 空池原因码, 把原来"三义合一"的一句话拆开
                        (`source_unavailable` / `non_trading_day` / `non_auction_session` / `no_anomaly`)

    数据为空时一律 `available=False` 且 `reason` 可诊断, 绝不编造。
    """
    from src.core.trade_phase import (
        PHASE_AUCTION,
        as_of_iso,
        cn_now,
        phase_meta,
        trading_day_anchor,
    )

    records = fetch_auction_anomaly(market)
    meta = phase_meta(now)
    phase = meta["trade_phase"]
    today = cn_now(now).date()
    clock = _source_clock(records)

    # ① 源端带完整日期 → 以源为准; ② 只带时分秒/没有 → 归属"最近一个交易日"
    #    (非交易日回看上一个交易日, 这正是 A-03 要修的那一格)
    sync_at = _last_sync_at()
    sync_date = None
    if sync_at is not None and hasattr(sync_at, "date"):
        sync_date = sync_at.date()

    data_date = None
    if clock:
        from src.core.trade_phase import parse_date  # noqa: PLC0415 - 源时钟解析

        data_date = parse_date(clock)
    if data_date is None:
        # 有行 → 归属最近交易日; 无行 → 只认"库里最后一次落库日", 两者都没有就诚实地给 None
        # (绝不在"没有任何数据"时编一个数据日出来)
        data_date = trading_day_anchor(today) if records else sync_date

    live = bool(records) and phase == PHASE_AUCTION and data_date == today

    if records:
        if live:
            reason = None
            reason_text = f"实时竞价池（{meta['phase_label']}）"
        elif data_date == today:
            reason = None
            reason_text = (
                f"当前{meta['phase_label']}，展示今日 09:25 集合竞价结果（已定型）"
            )
        else:
            reason = None
            reason_text = (
                f"非实时：当前{meta['phase_label']}，展示 {data_date} 的竞价快照"
            )
    elif not meta["is_trading_day"]:
        # 闭市是主导事实; 若同时拉取失败, 把错误也写出来(不隐藏运维信号)
        reason = "non_trading_day"
        reason_text = "非交易日，竞价池无当日数据"
        if _LAST_FETCH_ERROR:
            reason_text += f"（数据源同时不可达：{_LAST_FETCH_ERROR}）"
    elif _LAST_FETCH_ERROR:
        reason = "source_unavailable"
        reason_text = "数据源不可用（thsdk 竞价异动拉取失败），非「今日无异常」"
    elif phase != PHASE_AUCTION:
        reason = "non_auction_session"
        reason_text = f"非竞价时段（当前{meta['phase_label']}），无当日竞价快照"
    else:
        reason = "no_anomaly"
        reason_text = "今日集合竞价无异动标的"

    note = MISSING_NOTE + (f" | {reason_text}" if reason_text else "")
    return {
        "available": bool(records),
        "count": len(records),
        "records": records,
        "missing_fields": list(MISSING_FIELDS),
        "note": note,
        # ── A-03: 数据日期 / 交易阶段 / 是否实时 ──────────────────────────
        "data_date": data_date.isoformat() if data_date else None,
        "trade_phase": phase,
        "phase_label": meta["phase_label"],
        "is_trading_day": meta["is_trading_day"],
        "is_live": live,
        "is_today": data_date == today,
        "as_of": as_of_iso(now),
        "data_clock": clock,
        "data_synced_at": sync_at.isoformat() if sync_at is not None else None,
        "reason": reason,
        "reason_text": reason_text,
        "source": "thsdk 集合竞价异动池 + 通达信 TQ 逐票增强",
    }


def sync_auction_to_db(records: list) -> int:
    """把异动股写入 DB 表 auction_anomaly_records。返回入库条数(0 表示无数据/失败)。"""
    if not records:
        return 0
    try:
        from src.db.dialect import acquire_write
        from src.db.session import SessionLocal
        from src.db.models import AuctionAnomalyRecord

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

    from src.db.session import SessionLocal
    from src.db.models import AuctionAnomalyRecord

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
            misfire_grace_time=300,
        )
        logger.info("[auction] 竞价异动 job 已注册: 工作日 09:25 (%s)", scheduler.timezone)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[auction] 竞价异动 job 注册失败: %r", e)
        return False
