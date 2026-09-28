# -*- coding: utf-8 -*-
"""涨停池 TQ 涨跌停快照备源(审计 A-6, 2026-09-28)。

背景: 涨跌停快照此前只有主源(TQ K线全市场自算, `_limit_up_pool_tq`)与两个外部
兜底(wudao / 东财 push2ex)。TQ 网关的 `get_zdt_data` 有**独有字段**
(封单量 FDVolMaxZT / 首封时间 FirstTimeZT / 炸板次数 OpenTimesZT),
本模块把该快照接成**第三条备源**:

  主源 `_limit_up_pool_tq` 全市场 K线扫描挂掉(客户端断/连续失败)时,
  `zdt_fallback_pool()` 只对"真涨停"的分片调快照, 直接拿到
  封单/首封/炸板三真值 + K线自算连板数。

口径钉子(与 test_limit_up_pool_source.py 同族):
  · 单次 TQ 请求 ≤50 只(事故铁律, 2026-09-23 假死态教训), 快照与 K线都分片;
  · 连板数仍走 K 线自算(`build_pool_items`), 不拿快照字段猜
    (OpenTimesZT 是"打开次数"即炸板次数, 不是连板数);
  · 快照里没有的字段(ltsz/amount/pct/sector)留 0/空, **不编造**;
  · `source="tq_zdt"` 与主源 `"tq"` / `"eastmoney"` 区分, 消费方可标注来源。

包边界: marketdata 包不 import 宿主 `src/`(架构禁止), 纯解析函数放本模块,
TQ 侧复用包内现成封装 `zdt_snapshot()`(get_zdt_data)与 `tq_rpc()`。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_ZDT_CHUNK = 50  # 事故铁律: 单次 TQ 请求 ≤50 只
_BARS = 12  # 连板数最多往回数 12 根(与主源一致)


def parse_zdt_snapshot(snap: dict | None) -> dict[str, dict]:
    """TQ get_zdt_data 原始快照 → {TQ码: 真涨停记录}。

    判定"真涨停": 记录里 OpenTimesZT / FDVolMaxZT **字段存在**(值可为 0)。
    没涨停的票这两组字段整体缺失(或为 None), 缺 ⇒ 不猜, 直接剔除。
    非 dict 记录(脏行)剔除。
    """
    out: dict[str, dict] = {}
    for code, rec in (snap or {}).items():
        if not isinstance(rec, dict):
            continue
        if _present(rec.get("OpenTimesZT")) or _present(rec.get("FDVolMaxZT")):
            out[str(code)] = rec
    return out


def _present(v) -> bool:
    """字段存在判定: 0 合法(全天未开板), 仅 None/缺失 视为没有。"""
    return v is not None


def zdt_fallback_pool(codes: list[str] | None = None) -> list[dict]:
    """涨跌停快照备源: 对 codes(缺省=全A)取 TQ 快照 → 涨停池条目。

    步骤:
      ① get_stock_list 拿全A TQ 码(codes 缺省时; 单次 0.1s 级, 不会压垮客户端)
      ② 分片 zdt_snapshot(≤50 只/次) → parse_zdt_snapshot 挑真涨停
      ③ 只对真涨停股分片取 K 线(≤50/次) → build_pool_items 自算连板数
      ④ 快照真值回填: first_time(首封) / open_times(炸板次数) /
         order_amount(封单量, 股), source 标 "tq_zdt"

    降级不编造: 快照全空/网关异常 → []; K线取不到的涨停股保留(days=0=未知,
    不猜 1), 快照字段独立回填。
    """
    import marketdata.vendors.tq as tqmod

    zdt_snapshot = tqmod.zdt_snapshot
    if not codes:
        try:
            items = tqmod.tq_rpc("get_stock_list", {"market": "5", "list_type": 1}, timeout=15.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("涨停池(TQ备源): 取股票列表失败 %s", e)
            return []
        items = items if isinstance(items, list) else []
        codes = [str(i.get("Code")) for i in items if isinstance(i, dict) and i.get("Code")]
        if not codes:
            return []

    # ② 快照分片(单次 ≤50)
    snap: dict = {}
    fails = 0
    for i in range(0, len(codes), _ZDT_CHUNK):
        part = codes[i : i + _ZDT_CHUNK]
        try:
            s = zdt_snapshot(part)
        except Exception as e:  # noqa: BLE001
            fails += 1
            logger.warning("涨停池(TQ备源): 快照第 %d 批失败(%s)", i // _ZDT_CHUNK + 1, e)
            if fails >= 2:  # 与主源同铁律: 连续失败即中止, 绝不压测式重试
                return []
            continue
        if isinstance(s, dict):
            snap.update(s)
    live = parse_zdt_snapshot(snap)
    if not live:
        logger.warning("涨停池(TQ备源): 快照无真涨停条目(盘前/非交易日/网关返回空)")
        return []

    # ③ 只对真涨停股取 K 线(连板数自算口径不变)
    from src.core.limit_up_calc import build_pool_items

    bars: dict[str, dict] = {}
    names: dict[str, str] = {}
    for i in range(0, len(live), _ZDT_CHUNK):
        part = list(live.keys())[i : i + _ZDT_CHUNK]
        try:
            v = tqmod.tq_rpc(
                "get_market_data",
                {"stock_list": part, "period": "1d", "count": _BARS, "dividend_type": "none"},
                timeout=20.0,
            )
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, dict):
            bars.update({k: val for k, val in v.items() if isinstance(val, dict)})
    pool = build_pool_items(bars, names)

    # ④ 快照真值回填 + K线取不到的涨停股也保留(days=0=未知, 不猜 1)
    by_code: dict[str, dict] = {}
    for row in pool:
        by_code[str(row["code"])[:6]] = row
    for tq_code, rec in live.items():
        bare = tq_code.split(".", 1)[0]
        row = by_code.get(bare)
        if row is None:
            row = {
                "code": bare, "name": "", "price": 0.0, "pct": 0.0, "amount": 0.0,
                "ltsz": 0.0, "first_time": "", "last_time": "", "days": 0,
                "sector": "", "theme": "", "reason": "", "turnover_rate": 0.0,
                "order_amount": 0.0, "source": "tq",
            }
            pool.append(row)
        row["first_time"] = fmt_zdt_time(rec.get("FirstTimeZT"))
        ot = rec.get("OpenTimesZT")
        row["open_times"] = _to_int(ot, 0)
        fd = rec.get("FDVolMaxZT")
        # 封单量单位=手(成交量, 股=手×100); 与池子 amount(元)不是同量纲, 故独立字段
        row["order_amount"] = _to_int(fd, 0) * 100.0
        row["source"] = "tq_zdt"
    return pool


def _to_int(v, default: int) -> int:
    """TQ 值(int/float/数字串) → int; 取不到给 default, 不猜。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v)
    return default


def fmt_zdt_time(v) -> str:
    """TQ 首封时间 → 'HH:MM:SS'。数字串 92501→'09:25:01'、'09:31:05' 原样; 取不到留空。"""
    s = str(v or "").strip()
    if not s:
        return ""
    if s.isdigit():
        s = s.zfill(6)
        return f"{s[0:2]}:{s[2:4]}:{s[4:6]}" if len(s) >= 6 else s
    if ":" in s:
        return s[:8]
    return s
