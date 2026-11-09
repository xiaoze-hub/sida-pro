"""市场情绪采集器:涨停池 + 涨跌家数统计 + 连板梯队。

数据源:东财 push2ex getTopicZTPool(涨停池) + push2 ulist.np(指数)。
替代 SIDA 缺失的 wudao short_term_emotion / limit_up_pool 能力,
纯东财 HTTP 直连,免 key,适配云服务器环境。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

# 修复(L-2, 2026-08-23): 双源各回溯 6 天,无总超时会导致最坏 240s(MCP 限流/源宕时)。
# 给 get_limit_up_pool 整体加 30s 总预算, 命中即短路, 避免整页扫描接口卡 4 分钟。
_LIMITUP_TOTAL_BUDGET_S = 30.0

from src.collectors.market_http import market_get
# P1(audit-20260915): 核心 safe_float 统一到 numutil; 本模块历史默认值为 0.0(展示层), 保留薄包装
from src.core.numutil import safe_float as _num_safe_float  # noqa: E402

logger = logging.getLogger(__name__)

_ZTPOOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
_INDEX_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"

_ZT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def _ymd_to_iso(ymd: str | None) -> str | None:
    """YYYYMMDD → YYYY-MM-DD; 非 8 位数字 → 原样返回(不硬转, 不编造)。"""
    s = str(ymd or "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s or None


def _window_bars(bars: dict[str, dict], ymd: str) -> tuple[dict[str, dict], bool]:
    """把每只票的日线截到 ≤ ymd, 并判断是否真正覆盖该交易日。

    返回 (截断后的 bars, covered)。covered=False(没有任何票的末根日期 == ymd)时
    调用方必须放弃该源 —— 用最新一天的 K 线回答"昨天"就是数据错配。
    日期列表与 Close/Amount 按下标对齐; 形态异常(非 list)的字段原样保留由
    build_pool_items 自己兜底(算不出连板数 → 不入池, 不猜)。
    """
    out: dict[str, dict] = {}
    covered = False
    for code, payload in (bars or {}).items():
        if not isinstance(payload, dict):
            continue
        dates = payload.get("Date")
        if not isinstance(dates, list) or not dates:
            continue
        keep = -1
        for i, d in enumerate(dates):
            if str(d).strip() <= ymd:
                keep = i
            else:
                break
        if keep < 0 or str(dates[keep]).strip() != ymd:
            continue
        cut: dict = {}
        for k, v in payload.items():
            cut[k] = v[: keep + 1] if isinstance(v, list) and len(v) == len(dates) else v
        out[code] = cut
        if str(dates[keep]).strip() == ymd:
            covered = True
    return out, covered


def _safe_float(v, default: float = 0.0) -> float:
    """展示层安全转 float: 缺失/非法回退 default(历史口径 0.0)。委托 numutil。"""
    return _num_safe_float(v, default)


class MarketSentimentCollector:
    """市场情绪采集器:涨停池 / 涨跌家数 / 连板梯队。"""

    def __init__(self) -> None:
        self._cache: dict | None = None
        self._cache_ts: float = 0.0
        self._cache_ttl = 300  # 5 分钟缓存

    # ── 通达信(TQ)涨停池: 主源(2026-09-23 起) ──────────────────────────
    #
    # ⚠️ 事故铁律(2026-09-23 实测): **单次请求标的数必须 ≤ _TQ_CHUNK**。
    # 那天为做本功能, 向 17709 发了单次 5576 只 / 269 板块的批量请求 ⇒ 客户端进假死态
    # (整条链路 ConnectionReset), 生产 more-info 报"TQ 未连接"、/api/stocks/{s}/l2 500、
    # 行情降级腾讯(0.1s→2.2s), 恢复只能整机重启。而 50 只/批 实测仅 0.05~0.17s,
    # 全市场 112 批约 6~20s —— 分批几乎不额外花钱, 却把风险降到零。改这里前先读这段。
    _TQ_CHUNK = 50
    _TQ_BARS = 12  # 连板数最多往回数 12 根(足够覆盖市场最高板)

    def _limit_up_pool_tq(self, date: str | None = None) -> list[dict]:
        """通达信涨停池: 全A列表 → 分片取日线(不复权) → 自算涨停与**连板数** → 池子。

        为什么用 K 线而不是快照: `get_market_snapshot` 传列表会**崩客户端**(实测 10 只即崩,
        只能单只调用); `get_market_data` 列表稳定(生产 amount_baseline 已在用)且 50 只仅 0.05s。

        口径: 连板数由 `limit_up_calc.consecutive_boards` 从收盘价自算 —— 修掉此前"东财 `days`
        字段读错(实为 `lbc`)导致连板股全塌成首板"的问题。板块(行业优先)由 `stock_blocks` 补齐。

        date(YYYYMMDD, 2026-11-09): 显式传入时**只认该交易日** —— 把每只票的日线截到
        ≤ 该日, 且要求末根日期恰好等于该日; 覆盖不到就返回 [] (让带日期参数的备源接手),
        绝不拿最新一天的 K 线冒充"昨日池"(那会让 promo_rate/排名变动全错)。
        不传 = 取客户端最新一根(盘前即上一交易日), 时点按真实日线日期标注。
        """
        from src.core.limit_up_calc import build_pool_items

        from marketdata.vendors.tq import tq_rpc

        # ① 全A代码(单次调用, 0.1s 级 —— 这条不会压垮客户端)
        try:
            v = tq_rpc("get_stock_list", {"market": "5", "list_type": 1}, timeout=15.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("涨停池(TQ): 取股票列表失败 %s", e)
            return []
        items = v if isinstance(v, list) else []
        codes = [str(i.get("Code")) for i in items if isinstance(i, dict) and i.get("Code")]
        names = {str(i.get("Code")): str(i.get("Name") or "") for i in items if isinstance(i, dict) and i.get("Code")}
        if not codes:
            logger.warning("涨停池(TQ): 股票列表为空")
            return []

        # ② 分片取日线(不复权; 判涨停必须不复权)
        bars: dict[str, dict] = {}
        fails = 0
        for i in range(0, len(codes), self._TQ_CHUNK):
            part = codes[i : i + self._TQ_CHUNK]
            try:
                v = tq_rpc(
                    "get_market_data",
                    {"stock_list": part, "period": "1d", "count": self._TQ_BARS, "dividend_type": "none"},
                    timeout=20.0,
                )
            except Exception as e:  # noqa: BLE001
                fails += 1
                logger.warning("涨停池(TQ): 第 %d 批失败(%s)", i // self._TQ_CHUNK + 1, e)
                # 连续失败即中止并降级 —— 绝不"压测式重试"(那正是把客户端压死的做法)
                if fails >= 2:
                    logger.error("涨停池(TQ): 连续 %d 批失败, 中止并降级(已取 %d 只)", fails, len(bars))
                    return []
                continue
            if isinstance(v, dict):
                bars.update({k: val for k, val in v.items() if isinstance(val, dict)})

        if not bars:
            return []

        # ②b 显式日期: 截断到该交易日; 覆盖不到 → 空池(不拿最新一天冒充请求日)
        requested = str(date).strip() if date else ""
        if requested:
            bars, covered = _window_bars(bars, requested)
            if not bars or not covered:
                logger.warning("涨停池(TQ): 日线未覆盖请求日 %s → 交回备源(不冒充)", requested)
                return []

        # ③ 判涨停 + 连板数(纯函数, 可单测)
        pool = build_pool_items(bars, names)

        # ③b 数据时点: 显式日期即请求日; 否则取日线真实日期(YYYYMMDD → YYYY-MM-DD),
        #     取不到就留空 —— 绝不用"今天"冒充(盘前跑的是上一交易日数据)。
        try:
            if requested:
                as_of = _ymd_to_iso(requested)
            else:
                dates = [
                    str(d).strip()
                    for payload in bars.values()
                    if isinstance(payload, dict)
                    for d in (payload.get("Date") or [])
                    if str(d).strip()
                ]
                as_of = _ymd_to_iso(max(dates)) if dates else None
            if as_of:
                for row in pool:
                    row["as_of"] = as_of
        except Exception as e:  # noqa: BLE001
            logger.debug("涨停池(TQ): 日线日期解析失败(不影响池子): %s", e)

        # ④ 板块(行业优先) —— 只对涨停的几十只反查, 每只日内缓存
        #    显式历史日期不补板块: 板块归属是"当前"成员关系, 拿今天的概念盖昨天会错配。
        if not requested or requested == datetime.now().strftime("%Y%m%d"):
            self._enrich_pool_blocks(pool)

        return pool

    def _enrich_pool_blocks(self, pool: list[dict]) -> list[dict]:
        """给池子补**行业(仅 881)** 与**概念(非 881, 独立字段)**, 安全富化。

        · 行业缺失 → 留空, **绝不**拿 880 概念名顶到 sector(概念冒充行业);
        · 概念缺失 → 用 TQ get_relation 全量补, 去空/去重, 标 block_source;
        · 单只失败/无数据 → 跳过, 不编造; 池子为空直接返回。
        """
        if not pool:
            return pool
        try:
            from src.core.tdx_boards import stock_blocks
        except Exception as e:  # noqa: BLE001
            logger.debug("涨停池: 板块反查不可用(不影响池子): %s", e)
            return pool

        for row in pool:
            if row.get("sector") and row.get("concepts"):
                continue
            try:
                blocks = stock_blocks(row.get("code") or "") or []
            except Exception:  # noqa: BLE001
                blocks = []
            ind = [b for b in blocks if str(b.get("code") or "").startswith("881")]
            if ind and not row.get("sector"):
                row["sector"] = str(ind[0].get("name") or "")
            # 概念板块放进**独立字段** `concepts`(展示/概念维度聚合用), **不写 theme** ——
            # 聚合的 `_resolve_group_name` 是"theme 优先、其次 sector", 而 TDX 概念有 269 个且
            # 每只票归属多个, 拼成 theme 会让每只票自成一个组(实测: 37 只票 → 37 个组 → 全部
            # below_min → ranked=0, 明明"通用设备"有 4 只够门槛却被拆散)。行业口径不动。
            #
            # 2026-11-09: 概念**全量保存**(此前 [:3]), 否则第 4+ 位的归属概念(如"商业航天")
            # 永远进不了概念维度 —— 实证: 880548 概念当日 20 只涨停却分属不同行业,
            # 只留前 3 会让该类题材在主线上完全消失。一股多概念由 concept 维度独立统计
            # (不累加成"家数"), 故全量保存不会污染行业排名。
            concept = [b for b in blocks if "881" not in str(b.get("code") or "")]
            names_seen: list[str] = []
            for b in concept:
                nm = str(b.get("name") or "").strip()
                if nm and nm not in names_seen:
                    names_seen.append(nm)
            if names_seen:
                row["concepts"] = names_seen
                row["block_source"] = "tq:get_relation"
        return pool

    def _limit_up_pool_zdt_tq(self) -> list[dict]:
        """涨跌停快照备源(TQ get_zdt_data, 审计 A-6, 2026-09-28)。

        主源 `_limit_up_pool_tq` 全市场 K线扫描挂掉(客户端断/连续失败)时,
        这里只对"真涨停"的分片取快照 —— 直接拿到 TQ 独有字段:
          · first_time   首封时间(快照真值, 主源 K线路径恒为空串)
          · open_times   炸板次数(TQ 独有, 主源/wudao/东财都没有)
          · order_amount 封单量(手×100→股, 主源恒为 0)
        连板数仍走 K 线自算(build_pool_items, 口径钉子不动), 主源不动。
        """
        from src.core.limit_pool_zdt import zdt_fallback_pool

        return zdt_fallback_pool(None)

    def _limit_up_pool_wudao(self, date: str) -> list[dict]:
        """wudao 涨停事件池(limit_up_filter) → 统一字段。

        覆盖东财 push2ex 在云服务器被断的场景;wudao 字段更丰富:
        primaryTheme(开盘啦主类题材) / reason_type / turnover_rate / order_amount / continue_num。
        """
        from src.collectors.wudao_mcp_client import WudaoMCPClient

        client = WudaoMCPClient()
        resp = client.call_tool(
            "limit_up_filter",
            {"date": date, "limit": 100, "sortBy": "continue_num", "format": "json"},
        )
        if not isinstance(resp, dict):
            return []
        # 结构化返回: rows 或 items(primaryThemeStats 是题材聚合,不用)
        rows = resp.get("rows") or resp.get("items") or []
        if not rows:
            return []
        out = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if not code:
                continue
            def _f(v) -> float:
                try:
                    return float(v) if v is not None else 0.0
                except (TypeError, ValueError):
                    return 0.0
            out.append(
                {
                    "code": code,
                    "name": name,
                    "price": _f(item.get("closePrice") or item.get("lastPrice") or item.get("price")),
                    "pct": _f(item.get("changeRate") or item.get("pct")),
                    "amount": _f(item.get("tradingAmount") or item.get("amount")),
                    "ltsz": _f(item.get("actualCurrencyValue") or item.get("currencyValue")),
                    "first_time": str(item.get("firstLimitUpTimeText") or item.get("first_limit_up_time") or ""),
                    "last_time": str(item.get("lastLimitUpTimeText") or item.get("last_limit_up_time") or ""),
                    "days": int(_f(item.get("continueNum") or item.get("continue_num") or 1)),
                    "sector": str(item.get("industry") or ""),
                    "theme": str(item.get("primaryTheme") or item.get("concept") or ""),
                    "reason": str(item.get("reasonType") or item.get("reason_type") or ""),
                    "turnover_rate": _f(item.get("turnoverRate")),
                    "order_amount": _f(item.get("orderAmount")),
                    # P2(2026-10-08): 统一标来源, 降级链各源可追溯
                    "source": "wudao",
                }
            )
        return out

    def get_limit_up_pool(self, date: str | None = None) -> list[dict]:
        """获取涨停池: **通达信(TQ) 主源** → TQ 涨跌停快照备源 → wudao → 东财兜底。

        2026-09-23 换主源: 原"wudao 优先 + 东财兜底"在 wudao 挂掉(wudao 当日全挂 SSLEOF)
        时降级到东财 —— 而东财两个缺陷(连板数读错字段、pagesize=60 截断且按封板时间排序丢尾盘)
        会让主线排名系统性失真。TQ 走本地客户端: 全A分片取日线自算连板数, 不受第三方字段命名影响。
        

        date: YYYYMMDD,默认今天。当天(盘前)无数据时自动回退最近交易日(最多5天)。
        返回: [{code, name, price, pct, amount, ltsz, first_time, last_time, days(连板数), sector/theme, ...}]
        wudao 源额外带: theme(开盘啦主类题材) / reason(涨停原因) / turnover_rate / order_amount(封单额)。

        修复(L-2, 2026-08-23): 双源各 6 次回溯最坏 12*10+2*backoff=240s, 加总预算 _LIMITUP_TOTAL_BUDGET_S(30s)
        后命中即返回, 超时直接放弃后续回溯避免阻塞整页扫描。
        """
        # 修复(L-2): 总预算封顶
        deadline = time.monotonic() + _LIMITUP_TOTAL_BUDGET_S
        explicit_date = str(date).strip() if date else ""
        date = date or datetime.now().strftime("%Y%m%d")
        from datetime import timedelta

        def _probe(back: int) -> str:
            if not back:
                return date
            try:
                return (datetime.strptime(date, "%Y%m%d") - timedelta(days=back)).strftime("%Y%m%d")
            except Exception:
                return date

        # ⓿ 通达信(TQ)主源(2026-09-23)。空/异常 → 先试快照备源, 再降级 wudao/东财。
        #    date 必须透传: 显式"昨日池"拿今日 K 线 = 数据错配(market_phase promo_rate 恒 0)。
        try:
            tq_pool = self._limit_up_pool_tq(explicit_date or None)
            if tq_pool:
                return tq_pool
            logger.warning("涨停池(TQ)为空 → 先试涨跌停快照备源, 再降级 wudao/东财兜底")
        except Exception as e:  # noqa: BLE001
            logger.warning("涨停池(TQ)异常 → 先试涨跌停快照备源, 再降级 wudao/东财兜底: %s", e)

        # ⓿b TQ 涨跌停快照备源(A-6, 2026-09-28): 主源全市场 K线扫描挂掉时,
        # 快照只需对"真涨停"分片取数, 且自带首封/炸板/封单真值。空/异常 → 继续 wudao/东财。
        try:
            zdt_pool = self._limit_up_pool_zdt_tq()
            if zdt_pool:
                return self._maybe_enrich_fallback(zdt_pool, explicit_date)
        except Exception as e:  # noqa: BLE001
            logger.warning("涨停池(TQ备源-快照)失败 → 继续 wudao/东财: %s", e)

        # ① wudao 优先:找最近非空交易日(最多 5 天)。wudao 字段更全(题材/原因/封单/换手)
        for back in range(6):
            # 修复(L-2, 2026-08-23): 总预算耗尽则直接跳过, 不再白白超时
            if time.monotonic() >= deadline:
                logger.warning("market_sentiment.get_limit_up_pool 超总预算 %ss, 跳过 wudao 回溯", _LIMITUP_TOTAL_BUDGET_S)
                break
            probe = _probe(back)
            try:
                pool = self._limit_up_pool_wudao(probe)
            except Exception as e:
                logger.warning("wudao 涨停池失败(%s): %s", probe, e)
                pool = []
            if pool:
                return self._maybe_enrich_fallback(pool, explicit_date)

        # ② 东财兜底: 同逻辑找最近非空交易日。
        #    2026-09-23 修两处缺陷(都在这里):
        #      ① 连板数读错字段 —— 原来 item.get("days"), 东财实际字段是 **lbc** ⇒ 恒为 1
        #         (实测 8 只连板股全塌成首板: 新华文轩/大亚圣象真 4 板报 1 板);
        #      ② pagesize=60 硬截断且 sort=fbt:asc(按首次封板时间) ⇒ 09-21 真实 tc=103 只取 60,
        #         丢掉的恰是尾盘涨停股(最集中在热门板块) ⇒ 主线排名系统性偏。
        #      现在按响应自带 tc 翻页取全(单页 200, 最多 5 页)。
        for back in range(6):
            if time.monotonic() >= deadline:
                logger.warning("market_sentiment.get_limit_up_pool 超总预算 %ss, 跳过东财回溯", _LIMITUP_TOTAL_BUDGET_S)
                break
            probe = _probe(back)
            pool: list[dict] = []
            tc = 0
            for page in range(5):
                params = {
                    "ut": "7eea3edcaed734bea9cbfc24409ed989",
                    "dpt": "wz.ztzt",
                    "Pageindex": str(page),
                    "pagesize": "200",
                    "sort": "fbt:asc",
                    "date": probe,
                }
                # 修复(M-12): market_get 已默认 max_total_s=10s, 此处显式传入同时收紧单次回溯。
                data = market_get(
                    _ZTPOOL_URL,
                    host_key="push2ex.eastmoney.com",
                    params=params,
                    headers=_ZT_HEADERS,
                    timeout=6,
                    retries=1,
                    max_total_s=8.0,
                    parse="json",
                    log_label="涨停池",
                )
                page_pool, page_tc = _parse_ztpool(data)
                if page_tc:
                    tc = page_tc
                if not page_pool:
                    break
                pool = _merge_ztpool(pool, page_pool)
                if tc and len(pool) >= tc:
                    break  # 已取全(不再翻页)
            if pool:
                if tc and len(pool) < tc:
                    # 没取全就说清楚, 不假装完整
                    logger.warning("涨停池(东财 %s): 取到 %d 只 < 总数 %d(翻页未取全)", probe, len(pool), tc)
                return self._maybe_enrich_fallback(pool, explicit_date)
        return []

    def _maybe_enrich_fallback(self, pool: list[dict], explicit_date: str) -> list[dict]:
        """降级源的**安全富化**: 能补概念就补, 补不到就空着(聚合层会显式标注)。

        只对"当日/无日期"的池子补板块归属 —— 历史日期的板块成员关系与当日不同,
        拿今天的概念盖昨天是另一种数据错配, 宁可空着说"概念不可用"。
        """
        if not pool:
            return pool
        today = datetime.now().strftime("%Y%m%d")
        if explicit_date and explicit_date != today:
            for row in pool:
                row.setdefault("block_source", "")
            return pool
        try:
            return self._enrich_pool_blocks(pool)
        except Exception as e:  # noqa: BLE001
            logger.debug("涨停池(降级源): 概念富化失败(不影响池子): %s", e)
            return pool

    def get_sentiment_summary(self) -> dict:
        """市场情绪摘要:涨停家数/连板梯队/最高板/涨停板块分布。"""
        pool = self.get_limit_up_pool()
        if not pool:
            return {"error": "无涨停池数据"}

        total = len(pool)
        # 连板梯队
        ladder = {}
        for p in pool:
            d = p["days"]
            ladder[d] = ladder.get(d, 0) + 1
        max_days = max(ladder.keys()) if ladder else 0

        # 最高板股票
        top_stocks = [p for p in pool if p["days"] == max_days][:5]

        # 涨停板块分布(从涨停股所属行业反推主线题材)
        # wudao 源 theme(开盘啦主类题材)优先,行业兜底
        sector_dist = {}
        for p in pool:
            sector = p.get("theme") or p.get("sector") or "其他"
            sector_dist[sector] = sector_dist.get(sector, 0) + 1
        top_sectors = sorted(
            sector_dist.items(), key=lambda x: x[1], reverse=True
        )[:6]

        # 主线题材龙头候选(全市场推荐:连板≥2 优先,其次早盘首板)
        def _first_time_str(p) -> str:
            ft = p.get("first_time")
            if ft is None:
                return ""
            s = str(ft).strip()
            # 东财 fbt 是 HHMMSS 数字(如 92501),wudao 是 HH:MM:SS
            if s.isdigit() and len(s) == 6:
                return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"
            if s.isdigit() and len(s) == 5:
                return f"{s[0:1]}:{s[1:3]}:{s[3:5]}"
            return s

        candidates = []
        for p in pool:
            if p["days"] >= 2:
                candidates.append(p)
        candidates.sort(key=lambda x: (-x["days"], _first_time_str(x)))
        # 补足早盘首板(首封 10:00 前)
        if len(candidates) < 10:
            for p in pool:
                if p["days"] < 2 and _first_time_str(p) and _first_time_str(p) <= "10:00":
                    candidates.append(p)
                if len(candidates) >= 12:
                    break
        candidate_list = [
            {
                "code": p["code"],
                "name": p["name"],
                "days": p["days"],
                "theme": p.get("theme") or p.get("sector") or "",
                "reason": p.get("reason") or "",
                "first_time": _first_time_str(p),
                "turnover_rate": p.get("turnover_rate"),
                "order_amount": p.get("order_amount"),
                "source": p.get("source") or "",
            }
            for p in candidates[:12]
        ]

        # P2(2026-10-08): 透传降级来源 —— 池子各条已带 source, 这里汇总出主源,
        # 让消费方/前端能一眼看出"这份涨停池来自哪个源"(tq/tq_zdt/wudao/eastmoney),
        # 不再把降级结果当主源静默使用。
        src_counts: dict[str, int] = {}
        for p in pool:
            s = p.get("source") or "unknown"
            src_counts[s] = src_counts.get(s, 0) + 1
        limit_up_source = max(src_counts, key=lambda k: src_counts[k]) if src_counts else "unknown"

        return {
            "limit_up_count": total,
            "max_streak": max_days,
            "ladder": dict(sorted(ladder.items(), reverse=True)),
            "top_stocks": [f"{p['name']}({p['code']}){p['days']}板" for p in top_stocks],
            "top_sectors": [
                {"name": k, "count": v} for k, v in top_sectors
            ],
            "candidates": candidate_list,
            "limit_up_source": limit_up_source,
            "limit_up_sources": src_counts,
        }

    def get_sector_rotation(self, top_n: int = 10) -> dict:
        """板块轮动:行业板块涨幅榜 + 概念板块涨幅榜(含主力净额)。

        返回: {"industries": [...], "concepts": [...]}
        每项: {name, pct(涨幅%), main_net(主力净额)}
        """
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
        base_params = {
            "pn": "1", "pz": str(top_n), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2",
            "fid": "f3",
            "fields": "f3,f12,f14,f62",
        }
        result = {}

        for key, fs in (("industries", "m:90+t:2+f:!50"), ("concepts", "m:90+t:3+f:!50")):
            params = {**base_params, "fs": fs}
            data = market_get(
                url,
                host_key="push2.eastmoney.com",
                params=params,
                headers=headers,
                timeout=10,
                retries=2,
                parse="json",
                log_label=f"板块轮动-{key}",
            )
            if not data:
                result[key] = []
                continue
            diff = (data.get("data") or {}).get("diff") or []
            items = []
            for item in diff:
                items.append(
                    {
                        "name": item.get("f14", ""),
                        "pct": _safe_float(item.get("f3")),
                        "main_net": _safe_float(item.get("f62")),
                    }
                )
            result[key] = items

        # 东财板块轮动失败(云服务器常断)时,用 ftshare 全板块最新行情兜底
        if not result.get("industries") and not result.get("concepts"):
            try:
                from marketdata.vendors.ftshare import _get_client

                client = _get_client({})
                rows = client.call_tool("ft_eastmoney_board_latest_kline", {"page": 1, "page_size": 100}) or []
                if rows:
                    # 行业(名字含行业词)与概念混合,统一按涨幅排序,取前 top_n
                    items = []
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        items.append(
                            {
                                "name": r.get("board_name") or r.get("name") or "",
                                "pct": _safe_float(r.get("change_rate") or r.get("change_pct")),
                                "main_net": 0,
                            }
                        )
                    items = [x for x in items if x["name"] and not x["name"].startswith(("上证", "深证", "沪深", "融资", "HS", "北证"))]
                    items.sort(key=lambda x: x["pct"], reverse=True)
                    result["industries"] = items[: top_n // 2]
                    result["concepts"] = items[top_n // 2 : top_n]
            except Exception as e:
                logger.warning(f"ftshare 板块轮动兜底失败: {e}")

        return result

    def get_index_snapshot(self) -> list[dict]:
        """主要指数快照(上证/深成/创业板)。优先腾讯接口(更稳),失败退回东财。"""
        # 腾讯行情接口(和 SIDA quote vendor 同源,稳定)
        try:
            import requests

            url = "https://qt.gtimg.cn/q=sh000001,sz399001,sz399006"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if r.status_code == 200 and r.text.strip():
                result = []
                for line in r.text.strip().split(";"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    parts = line.split("~")
                    if len(parts) < 6:
                        continue
                    result.append(
                        {
                            "name": parts[1],
                            "price": _safe_float(parts[3]),
                            "pct": _safe_float(parts[32]) if len(parts) > 32 else 0.0,
                            "change": _safe_float(parts[31]) if len(parts) > 31 else 0.0,
                        }
                    )
                if result:
                    return result
        except Exception as e:
            logger.debug("腾讯指数接口失败: %s", e)

        # 退回东财
        params = {
            "fltt": "2",
            "invt": "2",
            "fields": "f2,f3,f4,f12,f14",
            "secids": "1.000001,0.399001,0.399006",
        }
        data = market_get(
            _INDEX_URL,
            host_key="push2.eastmoney.com",
            params=params,
            headers=_ZT_HEADERS,
            timeout=10,
            retries=2,
            parse="json",
            log_label="指数快照",
        )
        if not data:
            return []
        diff = (data.get("data") or {}).get("diff") or []
        result = []
        for item in diff:
            result.append(
                {
                    "name": item.get("f14", ""),
                    "price": _safe_float(item.get("f2")),
                    "pct": _safe_float(item.get("f3")),
                    "change": _safe_float(item.get("f4")),
                }
            )
        return result


def _parse_ztpool(data) -> tuple[list[dict], int]:
    """东财涨停池响应 → ([统一结构条目], 真实总数 tc)。

    字段映射(2026-09-23 修正): `lbc` = 连板数 —— 此前误读 `days` 导致连板数恒为 1。
    """
    if not isinstance(data, dict):
        return [], 0
    inner = data.get("data") or {}
    raw = inner.get("pool") or []
    tc = int(_safe_float(inner.get("tc")) or 0)
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "code": item.get("c", ""),
                "name": item.get("n", ""),
                "price": _safe_float(item.get("p")) / 1000 if item.get("p") else 0,
                "pct": _safe_float(item.get("zdp")),
                "amount": _safe_float(item.get("amount")),
                "ltsz": _safe_float(item.get("ltsz")),
                "first_time": item.get("fbt", ""),
                "last_time": item.get("lbt", ""),
                "days": int(_safe_float(item.get("lbc")) or 1),  # ← 连板数(lbc, 不是 days)
                "sector": item.get("hybk", "") or "",
                "source": "eastmoney",
            }
        )
    return out, tc


def _merge_ztpool(base: list[dict], extra: list[dict]) -> list[dict]:
    """翻页合并: 按代码去重(先到的保留), 保持顺序稳定。"""
    seen = {str(r.get("code")) for r in base}
    merged = list(base)
    for r in extra:
        c = str(r.get("code"))
        if c and c not in seen:
            seen.add(c)
            merged.append(r)
    return merged

