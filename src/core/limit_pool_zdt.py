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

## 第二用途(2026-11-11): 主源**成功时**的富化步骤

生产实测: 主源 `_limit_up_pool_tq`(K线自算涨停)返回的 85 只涨停票
ltsz/turnover_rate/order_amount 全 0、first_time 全空(85/85) —— K 线里根本没有这些
字段, 只有 `get_zdt_data` 快照有。此前快照只在主源失败时当备源跑, 主源成功就永远
拿不到首封/炸板/封单。`enrich_pool_with_zdt()` 把同一份快照接成**主源的富化步骤**:
只补缺失字段、已有真值不覆盖、取不到就不写(详见该函数 docstring)。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_ZDT_CHUNK = 50  # 事故铁律: 单次 TQ 请求 ≤50 只
# 连板窗口与主源一致: 数出 N 连板要 N+1 根, 取 21 根覆盖到 20 连板
# (12 根只能数出 11 板, 12 连板龙头会被少算一板)。
_BARS = 21


from src.core.tq_chips import normalize_code

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
    names: dict[str, str] = {}
    if not codes:
        try:
            items = tqmod.tq_rpc("get_stock_list", {"market": "5", "list_type": 1}, timeout=15.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("涨停池(TQ备源): 取股票列表失败 %s", e)
            return []
        items = items if isinstance(items, list) else []
        codes = [str(i.get("Code")) for i in items if isinstance(i, dict) and i.get("Code")]
        # 名称是判 ST(主板 5% 涨停)的唯一依据; 与主源同口径传给 build_pool_items
        names = {
            str(i.get("Code")): str(i.get("Name") or "")
            for i in items
            if isinstance(i, dict) and i.get("Code")
        }
        if not codes:
            return []

    # ② 快照分片(单次 ≤50)
    snap: dict = {}
    fails = 0
    # 2026-09-28 实测: TQ get_zdt_data 要求带市场后缀, 传裸码会 ErrorId=2(codestr error:xxxxxx)。
    # 本模块对外可能被传裸 6 位码(如应用侧统一格式), 入口统一补后缀, 已带后缀的原样保留。
    codes = [normalize_code(str(c)) for c in codes]

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
                "code": bare, "name": names.get(tq_code) or names.get(bare) or "",
                "price": 0.0, "pct": 0.0, "amount": 0.0,
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


def enrich_pool_with_zdt(pool: list[dict], *, fetcher=None) -> list[dict]:
    """主源池子补齐快照独有字段(首封/炸板/封单) —— **只补缺失, 不覆盖已有真值**。

    背景(2026-11-11 生产实测): 主源 `_limit_up_pool_tq` 由 K 线自算涨停, 返回的 85 只
    涨停票 ltsz/turnover_rate/order_amount 全 0、first_time 全空(85/85) —— K 线没有这些
    字段, 只有 `get_zdt_data` 快照有。备源 `zdt_fallback_pool` 只在主源失败时跑, 主源
    成功就永远拿不到首封/炸板/封单; 本函数把同一份快照接成**主源的富化步骤**。

    口径:
      · 只对池内代码分片取快照(**≤50 只/次**, 事故铁律; 同码去重, 裸码/带后缀算同一只);
      · **只补缺失**: `first_time` 空串 / `open_times` 缺失 / `order_amount<=0` /
        `ltsz<=0` 才写; 已有非零值一律不动(其他源的真值优先, 同时保证幂等);
      · 改过至少一个字段的行才标 `enrich_source="tq_zdt"` 与 `enrich_as_of`
        (= 池内**统一**的 `as_of`, 即 K 线可证实的交易日; 缺失/不一致 → None,
        绝不用"今天"冒充);
      · 快照没有的字段不编造: `turnover_rate` 快照 14 字段里**没有**(保持原值);
        `ltsz` 只在记录自带 `Ltsz`(单位亿, 与 get_more_info 同源)时补 → ×1e8 元;
      · 空快照/全脏行/网关异常 → **原对象原样返回**(调用方零成本兜底);
        连续 2 批失败即中止(绝不压测式重试, 与主源同铁律);
      · `fetcher` 可注入(测试/其他快照源): `fetcher(codes) -> {TQ码: 记录}`。

    连板数不参与: 快照 `OpenTimesZT` 是"打开次数"(炸板), 不是连板数 —— `days` 只认 K 线。
    """
    if not pool:
        return pool
    rows = [r for r in pool if isinstance(r, dict)]
    if not rows:
        return pool

    codes: list[str] = []
    seen: set[str] = set()
    for r in rows:
        c = str(r.get("code") or "").strip()
        if not c:
            continue
        tq_code = normalize_code(c)
        if tq_code in seen:
            continue
        seen.add(tq_code)
        codes.append(tq_code)
    if not codes:
        return pool

    fetch = fetcher or _default_zdt_fetch
    snap: dict = {}
    fails = 0
    for i in range(0, len(codes), _ZDT_CHUNK):
        part = codes[i : i + _ZDT_CHUNK]
        try:
            s = fetch(part)
        except Exception as e:  # noqa: BLE001
            fails += 1
            logger.warning("涨停池补快照: 第 %d 批失败(%s)", i // _ZDT_CHUNK + 1, e)
            if fails >= 2:  # 与主源同一铁律: 连续失败即中止, 绝不压测式重试
                break
            continue
        if isinstance(s, dict):
            snap.update(s)

    by_bare = {
        str(k).split(".", 1)[0]: v for k, v in (snap or {}).items() if isinstance(v, dict)
    }
    if not by_bare:
        logger.warning("涨停池补快照: 无可用记录(盘前/非交易日/网关返回空) → 池子原样返回")
        return pool
    # 2026-11-11 实测: 收盘后 get_zdt_data 对每只票都返回**全 0 记录**
    # (ZDTStatusNow=0 / FirstTimeZT=0 / FDVolMaxZT=0.00)。若照单全收, 会把
    # 「没有数据」写成「首封 00:00、封单 0 手、炸板 0 次」并盖上 enrich_source —— 假事实。
    # 因此全 0 快照一律视为无可用快照, 池子原样返回(盘后不补, 盘中才有真值)。
    if _snapshot_all_zero(by_bare.values()):
        logger.info("涨停池补快照: 快照全 0(盘后已清零) → 池子原样返回, 不写 0 值")
        return pool

    as_of = _common_as_of(rows)
    for row in rows:
        rec = by_bare.get(str(row.get("code") or "").split(".", 1)[0])
        if not rec:
            continue
        filled = False
        if _fillable(row, "first_time"):
            ft = fmt_zdt_time(rec.get("FirstTimeZT"))
            if ft:
                row["first_time"] = ft
                filled = True
        if _fillable(row, "open_times") and rec.get("OpenTimesZT") is not None:
            row["open_times"] = _to_int(rec.get("OpenTimesZT"), 0)
            filled = True
        if _fillable(row, "order_amount"):
            hands = _to_int(rec.get("FDVolMaxZT"), 0)
            if hands > 0:
                # 封单量单位=手(成交量, 股=手×100); 与 zdt_fallback_pool 同口径
                row["order_amount"] = hands * 100.0
                filled = True
        if _fillable(row, "ltsz"):
            yi = _to_float(rec.get("Ltsz"))
            if yi > 0:
                # get_zdt_data 记录若带 Ltsz(亿), 换成池子的元口径; 实测 14 字段无此项
                row["ltsz"] = yi * 1e8
                filled = True
        if filled:
            row["enrich_source"] = "tq_zdt"
            row["enrich_as_of"] = as_of
    return pool


def _snapshot_all_zero(records) -> bool:
    """快照记录是否**全部为 0/空**(收盘后 TQ 会这样返回)。

    只要有一条记录出现非零封单/非零首封/非零炸板, 就认为快照可用 —— 单只票的
    0 值可能是真值(整天没开板、封单极小), 但**所有**票同时为 0 只能是没数据。
    """
    any_rec = False
    for rec in records:
        if not isinstance(rec, dict):
            continue
        any_rec = True
        if _to_int(rec.get("FDVolMaxZT"), 0) > 0:
            return False
        if _to_int(rec.get("OpenTimesZT"), 0) > 0:
            return False
        if str(rec.get("FirstTimeZT") or "0").strip() not in ("0", "", "None"):
            return False
    return any_rec


def _default_zdt_fetch(codes: list[str]) -> dict:
    """默认快照取数(惰性 import, 便于离线测试注入 fetcher)。"""
    import marketdata.vendors.tq as tqmod

    return tqmod.zdt_snapshot(codes)


def _fillable(row: dict, field: str) -> bool:
    """该字段是否"缺失到可以补" —— **已有非零/非空值一律不补**(不覆盖真值)。

    `0` 视同缺失: 池子里 0 是"没有这个字段"的默认值(主源 K 线路径直接给 0),
    允许被更新的快照刷新; 一旦有非零真值, 无论来自哪一路都不再动。
    """
    v = row.get(field)
    if field == "first_time":
        return not str(v or "").strip()
    return _to_float(v) <= 0


def _common_as_of(rows: list[dict]) -> str | None:
    """池内**统一**的 as_of(K 线可证实的数据日); 缺失/不一致/部分缺 → None(不猜今天)。"""
    vals = {str(r.get("as_of") or "").strip() for r in rows}
    vals.discard("")
    if len(vals) != 1:
        return None
    if any(not str(r.get("as_of") or "").strip() for r in rows):
        return None  # 有一只没日期 ⇒ 整体不许盖章
    return vals.pop()


def _to_int(v, default: int) -> int:
    """TQ 值(int/float/数字串) → int; 取不到给 default, 不猜。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v)
    return default


def _to_float(v) -> float:
    """TQ 值 → float; 取不到/非法 → 0.0(调用方用 <=0 判缺失)。"""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


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
