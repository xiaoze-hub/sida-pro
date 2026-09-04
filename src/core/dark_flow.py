"""暗盘资金计算器 v5(2026-08-11 优化版, 截图验证对齐同花顺结构)。

数据基础(腾讯接口, 全部实测):
- 逐笔 appn=detail: 全天全量成交, 每笔 B=主动买/S=主动卖/M=中性
- dadan 档10 = 网页"大单数据"页(大单口径: 成交额/量阈值)
- 分价表 appn=price: 价位分布(价格维度)

优化点(v4 → v5):
1. 三分类: M 中性盘不再忽略(统计但不算净额)
2. 大单/暗盘分层: 大单(≥100万或≥1000手)=明盘, 中小单=暗盘(拆单藏身处)
   验证: 大单净买+4,843手 vs 中小单净买+32,460手 → 暗盘流入结构
3. 分价表价格维度: 低价承接比(价格<VWAP的买量占比) → 吸筹价位
4. 时段分解: 早盘/午盘/午后/尾盘
5. 信号: 暗盘显著流入+明盘流出 = 拆单吸筹(同花顺核心信号)
"""
from __future__ import annotations

import logging
import re
import urllib.request

from marketdata.symbol import Symbol

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

# 大单阈值(元): 网页"大单数据"页筛选口径(成交额≥100万 或 量≥1000手)
BIG_AMOUNT = 100e4   # 100万元
BIG_VOLUME = 1000    # 1000手

# ── 主力意图增强算法阈值(2026-08-14, 全部为经验值, 可调)──────────────────
# 三个纯函数: _detect_big_mid_divergence / _detect_price_divergence / _detect_rhythm。
# 单位: 金额阈值一律为"元", 百分比阈值为"涨跌幅%"。调参只改这里, 函数内不写死。
# 1) 超大单/大单背离(托盘出货 vs 压盘吸筹)
_DIV_BIG_NET = 800e4      # 超大单(≥100万)净额阈值: ±800万
_DIV_FLAT_PCT = 1.0       # 托盘出货: 价格滞涨判定 |涨跌幅%| < 1.0
_DIV_NO_DROP_PCT = -0.5   # 压盘吸筹: 价格抗跌判定 涨跌幅% >= -0.5
# 2) 量价背离(净流入滞涨 vs 净流出抗跌)
_DIV_MAIN_NET = 500e4     # 主力(≥20万)净额阈值: ±500万
_DIV_STALL_PCT = 0.5      # 净流入滞涨: 涨跌幅% < +0.5(价格不涨)
_DIV_HOLD_PCT = -0.5      # 净流出抗跌: 涨跌幅% > -0.5(价格不跌)
# 3) 时段节奏(早吸尾抛 / 早压尾拉 / 尾盘异动)
_RHYTHM_SEG_NET = 300e4   # 单时段净额阈值: ±300万
_RHYTHM_DAY_NET = 500e4   # 尾盘异动: 全天四段合计 |净额| > 500万
_RHYTHM_TAIL_RATIO = 0.4  # 尾盘异动: 尾盘 |净额| > 40% * 四段绝对值之和

# ── 主力意图判据共享常量(2026-08-25 抽公共, 防逻辑漂移)──────────────────────
# 审计建议5(docs/audit_main_intent_20260825.md §6.2): 以下阈值此前在
#   src/core/dark_flow.py::_judge_signal 与 src/agents/intraday_monitor.py::
#   _main_intent_both_inner 两处独立写死(500e4/35/48), 存在漂移风险。
# 现收敛为公共常量, dark_flow 内直接引用; intraday_monitor 应改为 import 本模块
# 引用同一常量, 避免阈值不一致导致主力意图口径漂移(本次因"不改其他文件"约束, 仅落地 dark_flow 侧)。
MAIN_NET_LIMIT = 500e4    # 主力(≥20万)净额阈值: ±500万元
ABSORB_INTENSITY = 35.0   # 强吸筹: 主力参与度(占全市场成交) ≥ 35%
ABSORB_BUY_RATIO = 48.0   # 强吸筹: 主力买占比(买占主力成交) ≥ 48%

# 暗盘数据源(2026-08-11 预留): 环境变量 PANWATCH_DARK_SOURCE 可切换
#   tencent_ticks = 腾讯逐笔(免费, 默认, 盘中实时, 方向自解析)
#   tdx_tck       = 通达信 .tck 超盘回放落盘(盘后精确, 官方方向 2B/2S; 2026-08-31 接入,
#                   找不到文件自动回退腾讯逐笔, 见 src/core/dark_l2.py + tdx_tick_parser.py)
#   l2_tencent    = 腾讯L2(预留, 需付费账号)
#   l2_sina       = 新浪L2(预留, 需购买)
#   l2_itick      = iTick L2(预留, 99 USDT/月)
# 未来接入付费L2时, 在 _fetch_all_ticks 处按 source 分发即可。
DARK_SOURCE = __import__("os").environ.get("PANWATCH_DARK_SOURCE", "tencent_ticks")

# 2026-09-04 #2: per-request 源覆盖(ContextVar, 线程/协程安全)。
# API ?source=thsdk 单股灰度验证, 默认源不动, 验证ok再切。
import contextvars as _ctxvars

_DARK_SOURCE_CTX: _ctxvars.ContextVar[str | None] = _ctxvars.ContextVar(
    "panwatch_dark_source", default=None)


def _active_source() -> str:
    """当前生效数据源: 请求级覆盖优先, 否则环境变量默认值。"""
    return _DARK_SOURCE_CTX.get() or DARK_SOURCE


def _tencent_code(symbol: Symbol) -> str | None:
    code = (symbol.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return None
    if code[0] in ("6", "9") or code.startswith("688"):
        return f"sh{code}"
    if code[0] in ("0", "2", "3"):
        return f"sz{code}"
    return None


# 逐笔缓存: {code -> (ts, ticks, last_page, last_seq, day)}。盘中 TTL 30s, 避免每轮监控重复翻页。
# day = 交易日(YYYY-MM-DD), 2026-08-13 修复: 腾讯逐笔页码按天重置, 缓存跨日残留时
# 增量续拉会从昨天的 last_page 往后拉 → 拉不到今天 0 页起的数据, 返回昨天残留(实测: 2 条)。
# 跨日(day != 今天)强制全量重拉; 旧 4 元组缓存(无 day)视为无效同样重拉。
# 2026-08-12 增量续拉: 缓存记录"拉到的页码+末条序号", 过期后只从上次页码往后拉新增页
# (9:35 拉 19 页 → 9:50 只拉 19 页之后), 序号断裂(盘中数据重置)才全量重拉。
_TICKS_CACHE: dict[str, tuple[float, list[dict], int, int, str]] = {}
# 2026-08-20 修复: 分钟接口冷启动 ~15s(swings 需翻页拉全量逐笔)。前 _TICKS_TTL=30s
# 配合前端 30s 轮询, 几乎每次都冷启动。改为 90s TTL, 保证连续轮询命中缓存。
_TICKS_TTL = 90.0


def _cache_day() -> str:
    """当前交易日字符串(本地日期; 腾讯逐笔页码按自然日重置, 与本地日对齐足够)。"""
    import datetime
    return datetime.date.today().isoformat()


def _cache_put(code: str, now: float, ticks: list[dict], last_page: int, last_seq: int) -> None:
    """统一写缓存(带交易日)。"""
    _TICKS_CACHE[code] = (now, ticks, last_page, last_seq, _cache_day())


def _cache_stale(code: str, cached: tuple) -> bool:
    """缓存是否跨日/旧格式(4 元组无 day) → 需要全量重拉。"""
    if len(cached) < 5:
        return True
    return cached[4] != _cache_day()


def clear_ticks_cache(code: str | None = None) -> int:
    """清逐笔缓存(2026-09-04 运维杠杆)。

    背景: main_net 钉死(-125035724 一字不动)时, 内存+磁盘双层快照都保留旧
    tick, 增量续拉"无新增"分支原样返回, 去重修复够不着。手动清后下次全量重拉。
    code=None 清全部(返回清除的 code 数), 否则只清单个腾讯 code(如 sz002361,
    返回 1/0)。同步落盘, 避免重启从磁盘回血。
    """
    if code is None:
        n = len(_TICKS_CACHE)
        _TICKS_CACHE.clear()
    else:
        n = 1 if _TICKS_CACHE.pop(code, None) is not None else 0
    try:
        _ticks_persist()
    except Exception:  # noqa: BLE001
        pass
    return n


def _drop_future_ticks(ticks: list[dict], now=None) -> list[dict]:
    """丢未来时刻 tick(2026-09-04 P0-1: 未来 tick 根治)。

    实证: 14:19 拉到末笔 15:15:45(比钟还晚)—— 腾讯翻页漂移/跨日残留会带未来
    时刻, tick 又只有 HH:MM:SS 无日期, 混入后 main_net 全歪。超 now+60s(容差)
    的直接丢弃。空列表原样返回; 解析失败的单笔保留(宁可多算不断链)。
    2026-09-05 周末/盘后复盘放行: 非工作日交易时段不过滤(看最近完整交易日，
    前端 trade_date 标注基准日); 盘中才严格丢未来。now 可注入单测。
    """
    import datetime as _dt
    if not ticks:
        return ticks

    def _s(t: str) -> int:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)

    try:
        _now = now if now is not None else _dt.datetime.now()
        now_s = _s(_now.strftime("%H:%M:%S"))
        limit = now_s + 60
    except Exception:  # noqa: BLE001
        return ticks
    # 周末/盘后复盘放行: 非工作日交易时段(周末/09:25前/15:05后)不过滤，
    # 直接看最近完整交易日(前端 trade_date 标注基准日)。盘中才严格丢未来。
    try:
        in_session = _now.weekday() < 5 and _s("09:25:00") <= now_s <= _s("15:05:00")
    except Exception:  # noqa: BLE001
        in_session = True
    if not in_session:
        return ticks
    out = []
    for t in ticks:
        try:
            if _s(t.get("t", "00:00:00")) <= limit:
                out.append(t)
        except Exception:  # noqa: BLE001
            out.append(t)
    return out


def _dedup_ticks(ticks: list[dict]) -> list[dict]:
    """(时间t, 价格price, 成交额amt)三元组指纹去重(2026-08-21)。

    背景: 盘后腾讯重排页码/seq, 盘中并发翻页页内容漂移, 同一笔成交会被拉
    到两次(不同 seq/不同页)。同一笔无论 seq 怎么变三元组不变; 同指纹保留
    后出现的。2026-09-04 事故: 全量路径缺去重, 主力买卖 10.54亿 vs 成交
    6.05亿(174% 熔断)—— 全量/增量统一走本函数。
    """
    dedup: dict[tuple, dict] = {}
    for t in ticks:
        fp = (t.get("t", ""), t.get("price"), round(t.get("amt", 0), 2))
        dedup[fp] = t
    return sorted(
        dedup.values(),
        key=lambda t: (t.get("t", ""), t.get("_seq", 0)),
    )

# 2026-08-12 磁盘持久化: 逐笔快照落盘(/app/data/cache), 重启后同交易日
# 直接从 last_page 增量续拉, 免全量翻页(冷启动 3.7s → ~0.5s)。
# 2026-09-04 P0-1: key 按日分(all:YYYY-MM-DD), 只读今天。旧 "all" key 曾跨版本
# 携带脏快照(含未来时刻 tick), 改为 orphan(TTL 86400 自然过期) + 首次 delete。
_TICKS_DISK = None
_TICKS_DISK_FLUSH_TTL = 60.0  # 写盘节流: 缓存 30s 一更新, 每 60s 刷一次即可


def _ticks_disk_key(day: str | None = None) -> str:
    return f"all:{day or _cache_day()}"


def _ticks_persist(load_only: bool = False):
    """逐笔快照写盘(惰性初始化 + 节流由 DiskCache 控制)。

    load_only=True 时只从磁盘加载到内存(模块 import 时用), 不写盘。
    """
    global _TICKS_DISK
    try:
        if _TICKS_DISK is None:
            from src.core.disk_cache import DiskCache, register
            _TICKS_DISK = DiskCache("dark_flow_ticks", ttl=86400.0, flush_interval=_TICKS_DISK_FLUSH_TTL)
            snap = _TICKS_DISK.get(_ticks_disk_key())
            if isinstance(snap, dict) and snap:
                _TICKS_CACHE.update(snap)
            else:
                # 脏 key 迁移: 删掉无日期旧 key, 避免未来某天被误读
                try:
                    _TICKS_DISK.delete("all")
                except Exception:  # noqa: BLE001
                    pass
            register(_TICKS_DISK)
        if not load_only:
            _TICKS_DISK.set(_ticks_disk_key(), dict(_TICKS_CACHE))
    except Exception:
        pass


# 2026-08-12: 模块 import 时加载磁盘缓存(重启后首次调用前就绪), 不写盘
_ticks_persist(load_only=True)


# 2026-09-04 P0-2: 拉取观测(运维 sanity 用): {tcode: {pages(拉到页数), ticks}}。
# wrapper 每次从缓存元组回填, 全量/增量/TTL命中路径统一有数。
_LAST_FETCH: dict[str, dict] = {}

# 2026-09-04 P1-4: 上次结论缓存(结论翻转注记用)。单例常驻, 随进程落盘。
_VERDICT_DISK = None


def _verdict_cache():
    global _VERDICT_DISK
    if _VERDICT_DISK is None:
        from src.core.disk_cache import DiskCache, register
        _VERDICT_DISK = DiskCache("dark_flow_verdict", ttl=86400.0)
        register(_VERDICT_DISK)
    return _VERDICT_DISK


def _in_trading_hours() -> bool:
    """是否在交易时段(工作日 09:25-15:05)。空拉取此时段才值得重试+告警。"""
    import datetime as _dt
    try:
        now = _dt.datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.strftime("%H:%M:%S")
        return "09:25:00" <= t <= "15:05:00"
    except Exception:  # noqa: BLE001
        return False


def _fetch_all_ticks_inner(code: str, max_pages: int = 200) -> list[dict]:
    """翻页拉取全天全量逐笔(增量续拉), 返回 [{direction, amount, volume, time}]。

    2026-08-11 打磨: 加 30s 缓存(盘中每轮监控复用) + 单页重试(腾讯偶发限流)。
    2026-08-12 增量续拉(用户设计): 缓存记录 last_page+last_seq, 过期后只拉
      新增页合并; 并发拉页(全天 19 页串行 2.9s → 并发 ~0.9s); 空页判定 ['']。
    数据源: 默认 tencent_ticks; 未来 L2 接入在此分发(PANWATCH_DARK_SOURCE)。
    """
    import time as _time
    now = _time.time()
    # 2026-09-04 #2: 灰度请求(source 覆盖且≠默认)不读写共享缓存, 直拉直返,
    # 避免 L2 tick 污染默认源快照(回滚零残留)。
    _gray = _active_source() != DARK_SOURCE
    cached = None if _gray else _TICKS_CACHE.get(code)

    # 2026-08-12: TTL 内直接返回(盘中 30s 窗口零请求; 重构时勿丢此分支)
    if cached and now - cached[0] < _TICKS_TTL:
        return cached[1]

    # 2026-08-13 跨日修复: 缓存属于昨天(或旧 4 元格式) → 丢弃走全量重拉,
    # 否则增量续拉从昨天 last_page 往后拉, 拉不到今天 0 页起的数据(实测返回 2 条残留)。
    if cached and _cache_stale(code, cached):
        _TICKS_CACHE.pop(code, None)
        cached = None

    # L2 数据源分发(2026-09-04 #2: _active_source 支持 per-request 覆盖灰度)。
    # 预留模块, 接入L2时实现
    _src = _active_source()
    if _src != "tencent_ticks":
        try:
            from src.core.dark_l2 import fetch_l2_ticks  # 预留模块, 接入L2时实现
            ticks = fetch_l2_ticks(code, _src)
            if not _gray:
                _cache_put(code, now, ticks, 0, 0)
                _ticks_persist()  # 2026-08-12: 快照落盘
            return ticks
        except Exception:
            pass  # L2 未接入/异常, 回退腾讯逐笔

    def _fetch_page(p: int) -> tuple[int, list[dict]]:
        """拉单页, 返回 (页码, ticks)。失败/空页返回 (p, [])。"""
        url = f"https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={code}&p={p}"
        body = None
        # 单页重试(最多2次): 腾讯偶发限流/超时
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=_HEADERS)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read().decode("gbk", "replace")
                break
            except Exception:
                if attempt == 1:
                    break
        if body is None:
            return p, []
        m = re.search(r'\[(\d+),"(.*?)"\]', body)
        if not m:
            return p, []
        rows = m.group(2).split("|")
        # 空页判定(2026-08-12 修复): 只有 1 行(如竞价首笔 09:25)是正常数据页,
        # 不能按 len<2 当空页丢弃! 真正的空页是 [''](腾讯翻页到尾返回空字符串)。
        if not rows or (len(rows) == 1 and len(rows[0].strip()) == 0):
            return p, []
        out: list[dict] = []
        for r in rows:
            parts = r.split("/")
            if len(parts) < 7:
                continue
            try:
                amt = float(parts[5])
                vol = float(parts[4])
                price = float(parts[2])
                direction = parts[6]
                t = parts[1]
                seq = int(parts[0]) if parts[0].isdigit() else -1
            except (ValueError, IndexError):
                continue
            if amt > 0:
                out.append({"d": direction, "amt": amt, "vol": vol, "price": price, "t": t, "_seq": seq,
                            # v6 平盘误标修正用: chg==0=相对前笔持平(涨停封板时腾讯几乎全标S)
                            "chg": float(parts[3]) if len(parts) > 3 else 1.0})
        return p, out

    def _drain_pages(start: int, max_pages: int, ticks: list[dict], batch: int = 10) -> tuple[list[dict], int, int]:
        """分批并发拉页: 每批 batch 页, 尾部连续2空页即停。避免一次性提交 200 个 future
        (ThreadPool with 退出会等待未运行任务, 空页判定后仍慢)。

        2026-09-04 P0-2: 同批结果先收齐再按页码顺序处理。此前 as_completed 乱序
        计数 empty_run, 空页先回来会直接 return, 丢掉同批还没回来的数据页。
        现在只有"批尾连续空页"才停, 批中空洞不再误杀后续页。
        """
        import concurrent.futures as _cf
        last_page = -1
        last_seq = -1
        p = start
        while p < max_pages:
            hi = min(p + batch, max_pages)
            with _cf.ThreadPoolExecutor(max_workers=5) as ex:
                futs = {ex.submit(_fetch_page, i): i for i in range(p, hi)}
                results = [fut.result() for fut in _cf.as_completed(futs)]
            trailing_empty = 0
            for pg, page_ticks in sorted(results, key=lambda r: r[0]):
                if page_ticks:
                    ticks.extend(page_ticks)
                    last_page = max(last_page, pg)
                    last_seq = max(last_seq, max((t.get("_seq", -1) for t in page_ticks), default=-1))
                    trailing_empty = 0
                else:
                    trailing_empty += 1
            if trailing_empty >= 2:
                return ticks, last_page, last_seq
            p = hi
        return ticks, last_page, last_seq

    # ---- 增量续拉: 缓存存在且过期(>30s) ----
    if cached:
        _, old_ticks, old_last_page, old_last_seq = cached[0], cached[1], cached[2], cached[3]
        # 从上次页码开始拉(最后一页盘中可能未满, 重新拉完整版) + 后续新页
        start = old_last_page
        if start >= max_pages:
            # 已拉满上限, 刷新缓存时间即可
            _cache_put(code, now, old_ticks, old_last_page, old_last_seq)
            _ticks_persist()  # 2026-08-12: 快照落盘
            return old_ticks
        new_ticks: list[dict] = []
        _new_ticks, last_page, last_seq = _drain_pages(start, max_pages, new_ticks)
        if _new_ticks:
            new_first_seq = min(t.get("_seq", -1) for t in _new_ticks)
            if new_first_seq <= 0 or old_last_seq <= 0 or new_first_seq >= old_last_seq:
                # 序号连续/推进(新 seq ≥ 旧末条 seq) → 合并去重(旧最后一页可能被新完整版覆盖)
                # 2026-09-04 P0-1: 合并前双方先丢未来 tick(旧快照残留洗白主入口)。
                merged = _dedup_ticks(_drop_future_ticks(old_ticks) + _drop_future_ticks(_new_ticks))
                # 总量守恒校验: 合并后总成交额不得超过旧数据+新增量的合理上界。
                # 若仍超(指纹也撞不出的极端重复), 放弃合并 → 全量重拉。
                old_amt = sum(x.get("amt", 0) for x in old_ticks)
                new_amt = sum(x.get("amt", 0) for x in _new_ticks)
                merged_amt = sum(x.get("amt", 0) for x in merged)
                if merged_amt > max(old_amt, new_amt) * 1.10 + 1e6:
                    logger.warning(
                        f"[dark_flow] {code} 增量合并后总额异常 "
                        f"(merged={merged_amt/1e8:.2f}亿 > max(old,new)="
                        f"{max(old_amt, new_amt)/1e8:.2f}亿×1.1), 弃增量全量重拉"
                    )
                    _TICKS_CACHE.pop(code, None)
                else:
                    for t in merged:
                        t.pop("_seq", None)
                    _cache_put(code, now, merged, last_page, last_seq)
                    _ticks_persist()  # 2026-08-12: 快照落盘
                    return merged
            # 序号断裂(新交易日/数据重置) 或 合并校验失败 → 全量重拉
            _TICKS_CACHE.pop(code, None)
        else:
            # 无新增(可能盘前/刚开盘): 刷新缓存时间, 保留旧数据
            # ⚠️ 2026-08-14 热修: 防跨日残留被"无新增"洗白 —— 昨天接口异常时可能只
            # 拉到少量残留(如 15:18 收盘后 2 笔)且 last_page 是昨天页码, 今天增量续拉
            # 从旧页码拉空页 → 走本分支 → 无条件刷新 day=今天 → 残留洗白成今天的缓存,
            # 之后 stale 判断永远放行, 主力意图永远拿到昨天残留(实测 tick=2)。
            # 残留特征: 最后一笔时间是"未来时间"(今天还没到, 如昨天 15:18)或
            # 当前已开盘(≥09:25)但最后一笔早于 09:25, 且笔数少(<30 不足以判断意图)。
            import datetime as _dt
            _stale_data = True
            if old_ticks:
                _last_t = old_ticks[-1].get("t", "")
                _now_t = _dt.datetime.now().strftime("%H:%M:%S")
                if _last_t:
                    if _now_t >= "09:25:00" and _last_t < "09:25:00":
                        _stale_data = True          # 已开盘但数据停在开盘前(残留)
                    elif _last_t > _now_t and _last_t > "15:00:00":
                        _stale_data = True          # 未来时间(昨天收盘后残留)
                    else:
                        _stale_data = False
            if _stale_data and len(old_ticks) < 30:
                _TICKS_CACHE.pop(code, None)        # 残留 → 全量重拉
            else:
                _cache_put(code, now, old_ticks, old_last_page, old_last_seq)
                _ticks_persist()  # 2026-08-12: 快照落盘
                return old_ticks

    # ---- 全量拉取(无缓存 / 序号断裂后) ----
    # 阶段1: 串行探前 6 页(确定页数 + 拿首批数据; 盘中数据少时 6 页内就到尾)
    probe_pages = 6
    ticks: list[dict] = []
    last_full = -1
    consecutive_empty = 0
    for p in range(min(probe_pages, max_pages)):
        _, page_ticks = _fetch_page(p)
        if page_ticks:
            ticks.extend(page_ticks)
            last_full = p
            consecutive_empty = 0
        else:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                ticks = _dedup_ticks(_drop_future_ticks(ticks))
                _cache_put(code, now, ticks, last_full, max((t.get("_seq", -1) for t in ticks), default=-1))
                _ticks_persist()  # 2026-08-12: 快照落盘
                return ticks

    # 阶段2: 并发拉剩余页(每页70条, 全天通常 10-200 页)
    if last_full >= 0 and consecutive_empty < 2:
        ticks, last_page, last_seq = _drain_pages(probe_pages, max_pages, ticks)
        # 2026-09-04: 全量路径同样指纹去重(并发翻页页内容漂移会拉重, 此前缺失
        # 导致主力买卖 10.54亿 vs 成交 6.05亿熔断)。注意 _dedup 前先留 seq 供排序。
        # 2026-09-04 P0-1: 先丢未来 tick 再去重(未来 tick 指纹正常, 去重洗不掉)。
        ticks = _dedup_ticks(_drop_future_ticks(ticks))
        for t in ticks:
            t.pop("_seq", None)
        _cache_put(code, now, ticks, last_page, last_seq)
        _ticks_persist()  # 2026-08-12: 快照落盘
        return ticks

    ticks = _dedup_ticks(_drop_future_ticks(ticks))
    for t in ticks:
        t.pop("_seq", None)
    _cache_put(code, now, ticks, last_full, max((t.get("_seq", -1) for t in ticks), default=-1))
    _ticks_persist()  # 2026-08-12: 快照落盘
    return ticks


def _fetch_all_ticks(code: str, max_pages: int = 200) -> list[dict]:
    """对外入口(签名不变): 拉取 + 回填观测 + 交易时段空拉取重试一次。

    2026-09-04 P0-2 sanity: 盘中拉空(0 笔)极可能是腾讯抖动而非真没成交,
    重试一次仍空才认(下游走 insufficient)。页数/笔数记 _LAST_FETCH 供 diag。
    """
    ticks = _fetch_all_ticks_inner(code, max_pages)
    if not ticks and _in_trading_hours():
        logger.warning(f"[dark_flow] {code} 盘中拉空, 重试一次")
        ticks = _fetch_all_ticks_inner(code, max_pages)
    try:
        cached = _TICKS_CACHE.get(code)
        _LAST_FETCH[code] = {
            "pages": (cached[2] + 1) if cached and len(cached) > 2 and cached[2] >= 0 else 0,
            "ticks": len(ticks),
        }
    except Exception:  # noqa: BLE001
        pass
    return ticks


# 2026-09-04 金额兜底: 簇总金额>=500万直接判主力(散户90秒内无此量级,
# 同花顺超大单单笔100万+, 簇总额500万即使拆10笔也有50万/笔)。
_MAIN_CLUSTER_AMT = 5_000_000.0


# v6 平盘误标修正阈值: 平盘笔中S占比超此线 → 方向不可信(涨停封板), 平盘S转M。
_FLAT_S_SKEW = 0.80


def _neutralize_flat_mislabel(ticks: list[dict]) -> list[dict]:
    """平盘误标修正(纯函数): chg==0笔中S占比>80% → 平盘S全转M。

    无chg字段的老tick(.get缺省非零)→原样返回，零 regression。
    M笔聚类时跳过不断簇(见循环内v6分支)，故转M=不参与方向统计。
    """
    flats = [t for t in ticks if t.get("chg", 1.0) == 0]
    if len(flats) < 3:
        return ticks
    s_ratio = sum(1 for t in flats if t.get("d") == "S") / len(flats)
    if s_ratio <= _FLAT_S_SKEW:
        return ticks
    return [{**t, "d": "M"} if (t.get("chg", 1.0) == 0 and t.get("d") == "S") else t for t in ticks]


def _detect_split_orders(ticks: list[dict], gap_sec: int = 10, window_sec: int = 90,
                         min_consec: int = 3, lo: float = 5e4, hi: float | None = None,
                         prev_close: float | None = None) -> dict:
    """拆单识别 v4: 时间间隔聚类识别"同方向密集成交簇"(拆单), 全簇累计暗盘流入/流出。

    2026-08-31 修复(用户反馈: 金健米业同花顺暗盘流入8亿 vs 我们净流出358万, 方向反+量级差200倍):
    - v3 只识别"金额5-100万且连续同方向不夹杂其他单"的簇, 涨停股成交密集、夹杂反向/超范围单
      → seq 频繁被打断, 漏检 90%+(金健米业中单买入6.5亿只识别出0.39亿)
    - v3 把"获利区买入"判为散户追涨排除在暗盘外, 但同花顺暗盘=所有拆单簇, 不分主力/散户
    修复:
    1. 拆单簇 = 相邻同方向笔间隔≤gap_sec(10s) 且簇总跨度≤window_sec(90s) 的连续同向成交;
       金额下限 lo=5万, 无上限(主力拆单每笔金额可大可小)
    2. 所有簇计入暗盘: buy_amt=买入簇总额(暗盘流入), sell_amt=卖出簇总额(暗盘流出)
    3. contrarian/reason 仅作意图属性(位置+方向), 不再用于区分是否计入暗盘
    2026-09-04 v5实验回滚教训(C计划5只对账: 非涨停2/2准, 涨停3/3方向反):
    曾试 hi=100万单笔上限"跳过不断簇", 结果涨停方向没回来, 反把准的两只砍塌
    (金螳螂1.01→0.26, 利欧0.76→0.09)——同花顺暗盘本就含均笔几百万的大簇。
    去开盘15分钟实验同样误杀真信号(金螳螂09:33买入簇是真拆单)。
    定论: L1逐笔无委托号, 涨停日散户获利了结潮与主力拆单不可区分, 属数据源天花板,
    非参数问题。涨停股由前端降权提示, 不再调参。

    Returns: {buy_amt(暗盘流入), sell_amt(暗盘流出), net, groups}
    """
    # v6 平盘误标修正(2026-09-04 P0d实锤: 涨停股98%成交价持平, 腾讯chg=0几乎全标S,
    # 龙版2147:3/亚盛3040:30, 卖出端爆仓致方向反; 非涨停股平盘B/S均衡则保留):
    # 平盘笔中S占比>80% → 平盘S全转M(方向不可信, 不参与聚类); 否则原样。
    ticks = _neutralize_flat_mislabel(ticks)
    def _t2s(t: str) -> int:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)

    clusters: list[dict] = []
    cur: dict | None = None

    def _flush() -> None:
        if cur is not None and cur["n"] >= min_consec:
            clusters.append(cur)

    for tk in sorted(ticks, key=lambda x: x["t"]):
        d = tk["d"]
        amt = tk["amt"]
        # v6: M(中性/平盘修正)跳过不断簇——涨停板满屏M时不断开B/S簇的时间连续性。
        if d == "M":
            continue
        if d not in ("B", "S") or amt < lo or (hi is not None and amt > hi):
            _flush()
            cur = None
            continue
        ts = _t2s(tk["t"])
        if (
            cur is not None
            and cur["d"] == d
            and (ts - cur["last_ts"]) <= gap_sec
            and (ts - cur["t0_ts"]) <= window_sec
        ):
            cur["amt"] += amt
            cur["n"] += 1
            cur["last_ts"] = ts
            cur["t1"] = tk["t"]
            cur["p1"] = tk["price"]
        else:
            _flush()
            cur = {
                "d": d, "amt": amt, "n": 1,
                "t0": tk["t"], "t1": tk["t"],
                "t0_ts": ts, "last_ts": ts,
                "p0": tk["price"], "p1": tk["price"],
            }
    _flush()

    buy_total = sell_total = 0.0
    main_buy = main_sell = herd_buy = herd_sell = 0.0
    groups: list[dict] = []
    for c in clusters:
        seq = [
            {"d": c["d"], "amt": c["amt"], "price": c["p0"], "t": c["t0"]},
            {"d": c["d"], "amt": 0.0, "price": c["p1"], "t": c["t1"]},
        ]
        g = _classify_split(seq, prev_close)
        g["amt"] = round(c["amt"])
        g["n"] = c["n"]
        g["t0"] = c["t0"]
        g["t1"] = c["t1"]
        g["p0"] = c["p0"]
        g["p1"] = c["p1"]
        groups.append(g)
        if c["d"] == "B":
            buy_total += c["amt"]
            if g["contrarian"]:
                main_buy += c["amt"]
            else:
                herd_buy += c["amt"]
        else:
            sell_total += c["amt"]
            if g["contrarian"]:
                main_sell += c["amt"]
            else:
                herd_sell += c["amt"]

    groups.sort(key=lambda g: -g["amt"])
    return {
        "buy_amt": round(buy_total),
        "sell_amt": round(sell_total),
        "net": round(buy_total - sell_total),
        # 2026-09-04: 主力/散户分项(全簇按 contrarian 归集, 暗盘净额口径不变)
        "main_buy": round(main_buy),
        "main_sell": round(main_sell),
        "herd_buy": round(herd_buy),
        "herd_sell": round(herd_sell),
        "groups": groups[:10],
    }


def _classify_split(seq: list[dict], prev_close: float | None) -> dict:
    """单组拆单簇分类, 返回组信息 + contrarian 标记(仅作意图展示, 不影响暗盘总额)。

    2026-08-31: _detect_split_orders 改为全簇累计暗盘流入/流出后, 本函数的 contrarian
    不再决定是否计入暗盘, 仅用于前端展示"逆势(疑似主力) vs 顺势(散户)"的标记。
    判据: 位置(套牢/获利)为主 + 价格方向辅助。
    2026-09-04: 加金额兜底(用户反馈: 5笔3629万买入簇被标"散户追涨",
    散户90秒内不可能同向连吃数百万)。组总金额>=500万直接判主力,
    位置/方向只决定是抄底/派发还是顺势买入/卖出。
    """
    direction = seq[0]["d"]
    amt_sum = sum(x["amt"] for x in seq)
    p0, p1 = seq[0]["price"], seq[-1]["price"]
    price_dir = "up" if p1 > p0 else ("down" if p1 < p0 else "flat")
    # 相对昨收位置(套牢区 vs 获利区)
    below_prev = prev_close is not None and p0 < prev_close
    # 金额兜底: 簇总额>=500万=主力(散户无此量级), 覆盖顺势/逆势
    is_whale = amt_sum >= _MAIN_CLUSTER_AMT
    contrarian = False
    reason = ""
    if direction == "B":
        if below_prev:
            contrarian = True
            reason = "主力抄底"          # 套牢区(价格<昨收)买入 = 主力抄底吸筹
        elif is_whale:
            contrarian = True
            reason = "主力买入"          # 获利区大金额买入 = 主力顺势吃货, 非散户追涨
        else:
            contrarian = False
            reason = "回落承接" if price_dir == "down" else "散户追涨"
    else:  # direction == "S"
        if not below_prev:
            contrarian = True
            reason = "主力派发"          # 获利区(价格>昨收)卖出 = 主力高位出货
        elif is_whale:
            contrarian = True
            reason = "主力卖出"          # 套牢区大金额卖出 = 主力砸盘, 非散户割肉
        else:
            contrarian = False
            reason = "散户解套" if price_dir == "up" else "散户割肉"

    return {
        "d": direction, "n": len(seq), "amt": round(amt_sum),
        "t0": seq[0]["t"], "t1": seq[-1]["t"],
        "p0": p0, "p1": p1,
        "contrarian": contrarian, "price_dir": price_dir, "reason": reason,
    }


# ── 内盘外盘口诀(2026-08-13)──────────────────────────────────────────────
# 7 条实战口诀(规则预判, 只提示不改结论)。数据基础: 腾讯 Quote 的
# volume_outer(外盘/主动买) + volume_inner(内盘/主动卖) + change_pct + volume_ratio。
_MNEMONIC_STRONG = 55.0       # ①~④ 单边占比阈值(外盘或内盘 >55%)
_MNEMONIC_BALANCE = 10.0      # ⑤ 内外盘相当: |买%-卖%| < 10%
_MNEMONIC_VOL_RATIO = 1.5     # ① ② 放量: 量比 > 1.5
_MNEMONIC_MOVE = 0.5          # 有效涨跌: |涨跌%| > 0.5 才算涨/跌(剔除噪音)
_MNEMONIC_FLAT = 1.0          # ⑦ 价格不动: |涨跌%| <= 1.0
_MNEMONIC_OSCILLATE = 3.0     # ⑥ 震荡: 0.5 < |涨跌%| <= 3%(有波动无单边)
# v0.4.79 口诀活代码化阈值(tck 主动率口径, 仅当 tck_active_ratio 入参有效时触发)
_MNEMONIC_TCK_DUAL_SMALL = 30.0   # ⑥ 双小单: 大单占比 < 30%(市场冷清, 主力未参与)
_MNEMONIC_TCK_DUAL_LARGE = 85.0   # ⑦ 双大单: 大单占比 > 85%(主力频繁进出, 疑似对倒)

# 2026-08-25 腾讯数据源适配(审计修复, 见 docs/audit_main_intent_20260825.md):
# 腾讯口径 volume≈外盘+内盘(active_ratio≈100%), 原 ⑥ active_ratio<30% 永不触发、
# ⑦ active_ratio>85% 恒真。改用量比/内外失衡替代, 保留用户七口诀语义。
_MNEMONIC_IMBALANCE = 15.0    # ⑦ 对倒: 内外盘失衡 |买%-卖%| < 15%(方向模糊)
_MNEMONIC_SHRINK_VOL = 0.8    # ⑥ 控盘洗盘: 缩量 量比 < 0.8
_MNEMONIC_NO_BIG_VOL = 1.2    # ⑥⑦③④ 共用: ③④ 缩量确认 量比 < 1.2; ⑦ 对倒放量 量比 > 1.2
_MNEMONIC_CHURN_VOL = 1.2     # ⑦ 对倒放量: 量比 > 1.2


def _num(v) -> float | None:
    """安全转 float, 失败/None 返回 None。"""
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _position_from_range(quote: dict | None) -> str:
    """今日振幅区间内估算位置(无K线时的简化口径): 分位 >=0.66 high / <=0.33 low / 其他 mid。"""
    try:
        if not quote:
            return "unknown"
        hi = _num(quote.get("high_price"))
        lo = _num(quote.get("low_price"))
        price = _num(quote.get("current_price"))
        if hi and lo and price and hi > lo:
            pct = (price - lo) / (hi - lo)
            if pct >= 0.66:
                return "high"
            if pct <= 0.33:
                return "low"
            return "mid"
    except Exception:
        pass
    return "unknown"


def _estimate_position(symbol: Symbol, quote: dict | None = None) -> str:
    """现价位置估算: 20日K线分位优先, 失败回退今日振幅区间, 再失败 unknown。"""
    try:
        from marketdata.vendors.kline import fetch_tencent_kline_raw
        code = _tencent_code(symbol)
        if code:
            bars = fetch_tencent_kline_raw(code, 20)
            if bars and len(bars) >= 5:
                highs = [b.high for b in bars if b.high]
                lows = [b.low for b in bars if b.low]
                if highs and lows:
                    hi, lo = max(highs), min(lows)
                    price = _num(bars[-1].close) or _num((quote or {}).get("current_price"))
                    if hi > lo and price:
                        pct = (price - lo) / (hi - lo)
                        if pct >= 0.66:
                            return "high"
                        if pct <= 0.33:
                            return "low"
                        return "mid"
    except Exception:
        pass
    return _position_from_range(quote)


def _signal_direction(signal: str) -> int:
    """从主力意图 signal 文本粗判方向: +1 偏多 / -1 偏空 / 0 中性。

    用于口诀 divergence(背离)判定: 信号文本同时含多空词(如"净流出…疑洗盘吸筹")
    视为中性, 不判背离, 避免把模糊信号误报成背离。
    """
    s = signal or ""
    bull = ("净流入" in s) or ("吸筹" in s)
    bear = ("净流出" in s) or ("派发" in s) or ("出货" in s) or ("抛压" in s)
    if bull and not bear:
        return 1
    if bear and not bull:
        return -1
    return 0


def _judge_mnemonic(
    dark: dict,
    quote: dict | None = None,
    tck_active_ratio: float | None = None,
) -> dict | None:
    """内盘外盘 7 口诀判定(规则预判, 只提示不改结论)。

    输入: compute_dark_flow 结果 dark + 腾讯 Quote 字段 dict(volume_outer/
    volume_inner/change_pct/volume_ratio/high_price/low_price/current_price/volume)。
    buy_pct = 外盘/(外盘+内盘)*100, sell_pct 反向; 涨跌/放量/位置按阈值判定。

    v0.4.79 口诀活代码化新增参数 tck_active_ratio(0-100):
      - 有 .tck 数据时(tck_active_ratio 非 None), ⑥⑦ 用原始「主动率」口诀
        (大单占比 < 30% = 双小单 / > 85% = 双大单, 主力意图最准)
      - 无 .tck 数据时(tck_active_ratio=None), 走腾讯兜底: ⑥「缩量+震荡」/ ⑦「内外失衡+不动+放量」
    优先级: tck 路径 > 兜底路径(因为 .tck 是交易所级委托, 比腾讯方向自解析更准)。

    命中返回 {mnemonic: 口诀名, direction: 看涨/看跌/观望/警惕/关注/中性,
    divergence: bool(口诀方向与主力意图 signal 方向是否背离), detail: 解析文本};
    无命中返回 None。

    判定优先级(避免多口诀同时命中时自相矛盾):
      ⑥ 控盘洗盘(结构)→ ⑤ 平衡(良性解读优先, 否则每个横盘日都被 ⑦ 误报对倒)→ ⑦ 对倒
      → ① ② 放量确认的强信号 → ③ ④ 位置信号
    2026-08-25 腾讯口径适配(审计修复): 腾讯 volume≈外盘+内盘(active_ratio≈100%),
    故 ⑥ 不再用 active_ratio<30%(永不触发), 改为「缩量+震荡」;
    ⑦ 不再用 active_ratio>85%(恒真), 改为「内外失衡+不动+放量」三条件。
    """
    if not dark or not quote:
        return None
    outer = _num(quote.get("volume_outer"))
    inner = _num(quote.get("volume_inner"))
    change_pct = _num(quote.get("change_pct"))
    volume_ratio = _num(quote.get("volume_ratio"))
    if outer is None or inner is None or (outer + inner) <= 0 or change_pct is None:
        return None

    total = outer + inner
    buy_pct = outer / total * 100
    sell_pct = inner / total * 100

    # 位置: 优先 compute_dark_flow 的 inner_outer.position(20日分位), 缺失回退今日振幅
    position = ((dark.get("inner_outer") or {}).get("position")) or _position_from_range(quote)

    # 成交量口径: 主动盘(外+内)占总成交量比例(腾讯 volume 总手)
    quote_vol = _num(quote.get("volume"))
    active_ratio = total / quote_vol * 100 if (quote_vol and quote_vol > 0) else 100.0

    # 涨跌/放量/形态
    up = change_pct > _MNEMONIC_MOVE
    down = change_pct < -_MNEMONIC_MOVE
    flat = abs(change_pct) <= _MNEMONIC_MOVE
    no_move = abs(change_pct) <= _MNEMONIC_FLAT       # ⑦ 价格不动
    oscillate = _MNEMONIC_MOVE < abs(change_pct) <= _MNEMONIC_OSCILLATE  # ⑥ 震荡
    volume_up = volume_ratio is not None and volume_ratio > _MNEMONIC_VOL_RATIO  # ① ② 放量
    volume_shrink = volume_ratio is not None and volume_ratio < _MNEMONIC_SHRINK_VOL  # ⑥ 缩量
    balance = abs(buy_pct - sell_pct) < _MNEMONIC_BALANCE  # ⑤ 内外盘相当
    # 2026-08-25 腾讯口径适配: 腾讯 volume≈外盘+内盘(active_ratio≈100%), 故不再用
    # active_ratio 判定 ⑥双小/⑦双大。⑥ 改用「缩量+震荡」, ⑦ 改用「内外失衡+不动+放量」。
    imbalance = abs(buy_pct - sell_pct) < _MNEMONIC_IMBALANCE   # ⑦ 内外盘失衡(方向模糊)
    quiet = volume_ratio is not None and volume_ratio < _MNEMONIC_NO_BIG_VOL  # ③④ 缩量确认
    churn = volume_ratio is not None and volume_ratio > _MNEMONIC_CHURN_VOL    # ⑦ 对倒放量

    head = (
        f"外盘{buy_pct:.1f}%/内盘{sell_pct:.1f}%, 涨跌{change_pct:+.2f}%, "
        f"量比{volume_ratio:.2f}, 位置:{position}, 主动盘占比{active_ratio:.0f}%"
    )

    # v0.4.79 口诀活代码化: 有 .tck 主动率时, ⑥⑦ 走原始「主动率」口诀(优先级最高, 因为 .tck
    # 是交易所级委托记录, 比腾讯方向自解析更准)。无效值(None/NaN/越界)走兜底, 不破坏现有行为。
    tck_active = (
        tck_active_ratio
        if isinstance(tck_active_ratio, (int, float)) and 0 <= tck_active_ratio <= 100
        else None
    )

    # (条件, 口诀名, 方向, 解析文本) —— 按优先级排列
    # 注意: tck 路径的 detail 用函数延迟格式化(避免 tck_active=None 时 f-string 立即炸)
    rules = [
        # ===== v0.4.79 tck 优先路径(主动率口径, 交易所级数据) =====
        # ⑦ 双大单(对倒): 大单占比 > 85%(主力频繁进出, 疑似对倒)
        (tck_active is not None and tck_active > _MNEMONIC_TCK_DUAL_LARGE, "双大单对倒", "警惕",
         lambda: f"大单主动率{tck_active:.0f}%>85%, 主力频繁进出, 疑似对倒(警惕)"),
        # ⑥ 双小单: 大单占比 < 30%(市场冷清, 主力未参与)
        (tck_active is not None and tck_active < _MNEMONIC_TCK_DUAL_SMALL, "双小单", "观望",
         lambda: f"大单主动率{tck_active:.0f}%<30%, 市场冷清主力未参与(观望)"),
        # ===== 兜底路径(腾讯口径) =====
        # ⑥ 控盘洗盘: 缩量(量比<0.8)+震荡(0.5<|涨跌|<=3%) → 关注
        (volume_shrink and oscillate, "控盘洗盘", "关注",
         "缩量(量比<0.8)且窄幅震荡 → 交投清淡, 疑似主力高度控盘后的洗盘(关注)"),
        # ⑤ 内外盘相当(|买-卖|<10%)+横盘 → 多空平衡(观望)
        (balance and flat, "多空平衡", "观望",
         "内外盘占比接近且横盘 → 多空力量平衡, 方向不明(观望)"),
        # ⑦ 对倒造假: 内外盘失衡(|买-卖|<15%)+价格不动(|涨跌|<=1%)+放量(量比>1.2) → 警惕
        (imbalance and no_move and churn, "对倒造假", "警惕",
         "内外盘方向模糊且价格几乎不动却放量 → 疑似对倒制造成交量, 警惕出货陷阱(警惕)"),
        # ① 外盘大(>55%)+涨+放量 → 真金进攻(看涨)
        (buy_pct > _MNEMONIC_STRONG and up and volume_up, "真金进攻", "看涨",
         "外盘占比高+上涨+放量 → 主动性买盘真金进攻(看涨)"),
        # ② 内盘大(>55%)+跌+放量 → 主力撤退(看跌)
        (sell_pct > _MNEMONIC_STRONG and down and volume_up, "主力撤退", "看跌",
         "内盘占比高+下跌+放量 → 主动性卖盘汹涌, 疑似主力撤退(看跌)"),
        # ③ 外盘大+跌+高位+缩量(量比<1.2) → 诱多出货(警惕)
        (buy_pct > _MNEMONIC_STRONG and down and position == "high" and quiet, "诱多出货", "警惕",
         "外盘占比高却下跌且处高位, 量能萎缩 → 疑似边拉边出诱多, 警惕高位派发(警惕)"),
        # ④ 内盘大+涨+低位+缩量(量比<1.2) → 压盘吸筹(看涨)
        (sell_pct > _MNEMONIC_STRONG and up and position == "low" and quiet, "压盘吸筹", "看涨",
         "内盘占比高但上涨且处低位, 量能萎缩 → 疑似压盘吸筹, 低位换手收集筹码(看涨)"),
        ]
    for cond, name, direction, text in rules:
        if not cond:
            continue
        # v0.4.79: tck 路径用 lambda 延迟格式化(避免 tck_active=None 时 f-string 炸)
        if callable(text):
            text = text()
        # 背离: 口诀方向与主力意图 signal 方向相反(仅数据充分且信号方向明确时判定)
        divergence = False
        if dark.get("data_status") == "ok":
            sig_dir = _signal_direction(dark.get("signal", ""))
            mn_dir = {"看涨": 1, "看跌": -1}.get(direction, 0)
            divergence = sig_dir != 0 and mn_dir != 0 and sig_dir != mn_dir
        if divergence:
            text += " ⚠️ 与主力资金意图方向背离, 优先以主力意图为准!"
        return {
            "mnemonic": name,
            "direction": direction,
            "divergence": divergence,
            "detail": f"{head} → {text}",
        }
    return None


def _detect_big_mid_divergence(big_net, mid_net, change_pct) -> dict | None:
    """超大单/大单背离检测(托盘出货 vs 压盘吸筹)。

    逻辑: 超大单(≥100万)与大单(20-100万)方向相反, 叠加价格异动缺失 → 主力对倒嫌疑。
    - 托盘出货(危险): 超大单大幅净买(拉抬) + 大单大幅净卖(出逃) + 价格滞涨 → 诱多
    - 压盘吸筹: 超大单大幅净卖(打压) + 大单大幅净买(吸筹) + 价格抗跌 → 洗盘
    参数可能为 None(数据缺失), 一律不触发(宁可漏报不误报)。纯函数, 无 IO。
    阈值常量: _DIV_BIG_NET(±800万) / _DIV_FLAT_PCT / _DIV_NO_DROP_PCT, 见文件顶部。

    Returns: 命中返回 {type, big_net, mid_net, change_pct, detail}; 否则 None。
    """
    big_net = _num(big_net)
    mid_net = _num(mid_net)
    change_pct = _num(change_pct)
    if big_net is None or mid_net is None or change_pct is None:
        return None
    # 托盘出货(危险): 超大单拉抬 + 大单出逃 + 价格滞涨
    if big_net > _DIV_BIG_NET and mid_net < -_DIV_BIG_NET and abs(change_pct) < _DIV_FLAT_PCT:
        return {
            "type": "托盘出货",
            "big_net": round(big_net),
            "mid_net": round(mid_net),
            "change_pct": change_pct,
            "detail": "超大单拉抬+大单出逃+价格滞涨, 警惕诱多",
        }
    # 压盘吸筹: 超大单打压 + 大单吸筹 + 价格抗跌
    if big_net < -_DIV_BIG_NET and mid_net > _DIV_BIG_NET and change_pct >= _DIV_NO_DROP_PCT:
        return {
            "type": "压盘吸筹",
            "big_net": round(big_net),
            "mid_net": round(mid_net),
            "change_pct": change_pct,
            "detail": "超大单打压+大单吸筹+价格抗跌, 疑洗盘",
        }
    return None


def _detect_price_divergence(main_net, change_pct) -> dict | None:
    """量价背离检测(净流入滞涨 vs 净流出抗跌)。

    主力净额方向与价格表现相悖 → 对倒/换手 或 压盘吸筹嫌疑。
    - 净流入滞涨: 主力大幅净买(≥500万)但价格不涨(涨跌幅% < +0.5) → 对倒/换手嫌疑
    - 净流出抗跌: 主力大幅净卖(≤-500万)但价格抗跌(涨跌幅% > -0.5) → 压盘吸筹嫌疑
    参数为 None 一律不触发。纯函数, 无 IO。
    阈值常量: _DIV_MAIN_NET(±500万) / _DIV_STALL_PCT / _DIV_HOLD_PCT, 见文件顶部。

    Returns: 命中返回 {type, main_net, change_pct, detail}; 否则 None。
    """
    main_net = _num(main_net)
    change_pct = _num(change_pct)
    if main_net is None or change_pct is None:
        return None
    # 净流入滞涨: 主力净流入但价格不涨
    if main_net > _DIV_MAIN_NET and change_pct < _DIV_STALL_PCT:
        return {
            "type": "净流入滞涨",
            "main_net": round(main_net),
            "change_pct": change_pct,
            "detail": "主力净流入但价格不涨, 对倒/换手嫌疑",
        }
    # 净流出抗跌: 主力净流出但价格抗跌
    if main_net < -_DIV_MAIN_NET and change_pct > _DIV_HOLD_PCT:
        return {
            "type": "净流出抗跌",
            "main_net": round(main_net),
            "change_pct": change_pct,
            "detail": "主力净流出但价格抗跌, 压盘吸筹嫌疑",
        }
    return None


def _detect_rhythm(segments: dict) -> dict | None:
    """时段节奏模式检测(早吸尾抛 / 早压尾拉 / 尾盘异动)。

    segments: {morning, mid, afternoon, tail} 四段净额(单位: 元)。
    值可能为 0 或 None(数据缺失), 统一容错为 0.0。
    - 早吸尾抛: 早盘净买(≥300万) + 尾盘净卖(≤-300万) → 拉高出货特征
    - 早压尾拉: 早盘净卖(≤-300万) + 尾盘净买(≥300万) → 洗盘特征
    - 尾盘异动: 全天四段合计 |净额| > 500万 且 (尾盘 |净额| > 40%*四段绝对值之和
      或 尾盘方向与全天净额相反且 |尾盘净额| > 300万) → 尾盘方向与全天背离
    纯函数, 无 IO。阈值常量: _RHYTHM_SEG_NET / _RHYTHM_DAY_NET / _RHYTHM_TAIL_RATIO。

    Returns: 命中返回 {pattern, detail, ...}; 否则 None。
    """
    keys = ("morning", "mid", "afternoon", "tail")
    vals: dict[str, float] = {}
    for k in keys:
        v = _num((segments or {}).get(k))
        vals[k] = 0.0 if v is None else v
    morning, tail = vals["morning"], vals["tail"]
    # 早吸尾抛: 早盘吸筹 + 尾盘抛压
    if morning > _RHYTHM_SEG_NET and tail < -_RHYTHM_SEG_NET:
        return {
            "pattern": "早吸尾抛",
            "morning": round(morning),
            "tail": round(tail),
            "detail": "早盘吸筹尾盘抛压, 拉高出货特征",
        }
    # 早压尾拉: 早盘打压 + 尾盘回补
    if morning < -_RHYTHM_SEG_NET and tail > _RHYTHM_SEG_NET:
        return {
            "pattern": "早压尾拉",
            "morning": round(morning),
            "tail": round(tail),
            "detail": "早盘打压尾盘回补, 洗盘特征",
        }
    # 尾盘异动: 全天净额显著 + 尾盘占比过高 或 尾盘方向与全天相反
    day_net = sum(vals.values())
    sum_abs = sum(abs(v) for v in vals.values())
    tail_heavy = sum_abs > 0 and abs(tail) > _RHYTHM_TAIL_RATIO * sum_abs
    tail_contrary = tail * day_net < 0 and abs(tail) > _RHYTHM_SEG_NET
    if abs(day_net) > _RHYTHM_DAY_NET and (tail_heavy or tail_contrary):
        return {
            "pattern": "尾盘异动",
            "day_net": round(day_net),
            "tail": round(tail),
            "detail": "尾盘方向与全天背离",
        }
    return None


def compute_dark_flow(symbol: Symbol) -> dict | None:
    """计算暗盘资金 v5: 三分类 + 大单/暗盘分层 + 价格维度 + 时段。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    ticks = _fetch_all_ticks(code)
    if not ticks:
        return None

    # 昨收(用于套牢区判断: 涨中卖+低于昨收=散户解套, 非主力派发)
    prev_close = None
    quote_dict = None
    try:
        from marketdata.vendors.tencent import TencentQuoteVendor
        q = TencentQuoteVendor().fetch([symbol], {})[0]
        prev_close = q.prev_close if q.prev_close else None
        quote_dict = {
            "current_price": q.current_price,
            "high_price": q.high_price,
            "low_price": q.low_price,
            "change_pct": q.change_pct,
            "volume_ratio": q.volume_ratio,
            "volume_outer": q.volume_outer,
            "volume_inner": q.volume_inner,
            "turnover": getattr(q, "turnover", None),
        }
    except Exception:
        pass

    # ---- 基础统计(三分类, 竞价单单独处理) ----
    # 关键(2026-08-11 三表破解): 9:25-9:30 集合竞价撮合不是"主动买入",
    # 腾讯网页大单把它算中性盘。方向标记 B 在竞价时段不可信。
    buy_amt = sell_amt = m_amt = 0.0
    buy_vol = sell_vol = m_vol = 0.0
    auction_amt = auction_vol = 0.0   # 竞价单(集合竞价撮合)
    # 大单分层
    big_buy_amt = big_sell_amt = 0.0
    small_buy_amt = small_sell_amt = 0.0
    # 时段
    seg = {"morning": 0.0, "mid": 0.0, "afternoon": 0.0, "tail": 0.0}

    for tk in ticks:
        d, amt, vol, t = tk["d"], tk["amt"], tk["vol"], tk["t"]
        is_big = amt >= BIG_AMOUNT or vol >= BIG_VOLUME
        hm = t[:5]
        # 竞价时段(9:25-9:30): 集合竞价撮合, 方向不可信, 单独统计
        if t < "09:30":
            auction_amt += amt
            auction_vol += vol
            continue
        if d == "B":
            buy_amt += amt; buy_vol += vol
            if is_big: big_buy_amt += amt
            else: small_buy_amt += amt
            sign = 1.0
        elif d == "S":
            sell_amt += amt; sell_vol += vol
            if is_big: big_sell_amt += amt
            else: small_sell_amt += amt
            sign = -1.0
        else:  # M 中性
            m_amt += amt; m_vol += vol
            continue
        # 时段净额
        if hm < "10:30":
            seg["morning"] += sign * amt
        elif hm < "11:30":
            seg["mid"] += sign * amt
        elif hm < "14:30":
            seg["afternoon"] += sign * amt
        else:
            seg["tail"] += sign * amt

    # ---- 结果 ----
    dark_net = buy_amt - sell_amt          # 全量主动净额(剔除竞价)
    big_net = big_buy_amt - big_sell_amt   # 超大单(≥100万)净额
    small_net = small_buy_amt - small_sell_amt  # 中小单(<100万)净额

    # 当日主力意图(2026-08-11 修正, 腾讯官方口径):
    # 主力 = 成交金额≥20万 或 股数≥6万股(600手); 超大单≥100万; 大单=主力-超大单
    # ⚠️ 必须剔除竞价单(9:25-9:30 撮合非主动买卖), 否则主力净额被竞价B污染
    non_auction = [t for t in ticks if t["t"] >= "09:30"]
    main_buy_amt = sum(t["amt"] for t in non_auction if (t["amt"] >= 20e4 or t["vol"] >= 600) and t["d"] == "B")
    main_sell_amt = sum(t["amt"] for t in non_auction if (t["amt"] >= 20e4 or t["vol"] >= 600) and t["d"] == "S")
    main_net = main_buy_amt - main_sell_amt           # 主力净额(≥20万, 剔除竞价)
    big_net = big_buy_amt - big_sell_amt               # 超大单净额(≥100万, 已剔除竞价)
    mid_net = main_net - big_net                        # 大单净额(20万-100万)
    retail_buy_amt = sum(t["amt"] for t in non_auction if not (t["amt"] >= 20e4 or t["vol"] >= 600) and t["d"] == "B")
    retail_sell_amt = sum(t["amt"] for t in non_auction if not (t["amt"] >= 20e4 or t["vol"] >= 600) and t["d"] == "S")
    retail_net = retail_buy_amt - retail_sell_amt      # 散户净额(<20万, 剔除竞价)
    main_intensity = (main_buy_amt + main_sell_amt) / (buy_amt + sell_amt) * 100 if (buy_amt + sell_amt) else None  # 主力参与度%
    main_buy_ratio = main_buy_amt / (main_buy_amt + main_sell_amt) * 100 if (main_buy_amt + main_sell_amt) else None  # 主力买占主力成交%

    # ---- 物理守卫(2026-08-23 P4): 盘中实时对账 ----
    # 主力成交额(买+卖)物理上不可能超过全日总成交额; 超过即数据异常
    # (典型故障: 逐笔缓存重复计数, 2026-08 两次净额翻倍事故均为此类, 当时只有
    # 盘后哨兵能发现)。对标 data_quality_sentinel 的 130% 阈值, 盘中即时拦截。
    _quote_turnover = _num((quote_dict or {}).get("turnover")) or 0.0
    data_suspect = bool(
        _quote_turnover > 0
        and (main_buy_amt + main_sell_amt) > _quote_turnover * 1.30
    )

    result = {
        "dark_net": round(dark_net),           # 全量主动净额
        "main_net": round(main_net),           # 主力净额(≥20万, 腾讯官方口径)
        "big_net": round(big_net),             # 超大单净额(≥100万)
        "mid_net": round(mid_net),             # 大单净额(20-100万)
        "small_net": round(retail_net),        # 散户净额(<20万)
        "buy_amt": round(buy_amt), "sell_amt": round(sell_amt),
        "m_amt": round(m_amt),
        "buy_vol": round(buy_vol), "sell_vol": round(sell_vol), "m_vol": round(m_vol),
        "buy_pct": round(buy_vol / (buy_vol + sell_vol + m_vol) * 100, 1) if (buy_vol + sell_vol + m_vol) else None,
        "sell_pct": round(sell_vol / (buy_vol + sell_vol + m_vol) * 100, 1) if (buy_vol + sell_vol + m_vol) else None,
        "auction_amt": round(auction_amt),   # 竞价撮合金额(元)
        "auction_vol": round(auction_vol),   # 竞价撮合手数
        "main_intensity": round(main_intensity, 1) if main_intensity is not None else None,  # 主力参与度%
        "main_buy_ratio": round(main_buy_ratio, 1) if main_buy_ratio is not None else None,  # 主力买占比%
        # 2026-08-13: 内盘外盘结构化字段(分时卡片用)。buy_amt/sell_amt = 主动买/卖金额(元),
        # buy_pct/sell_pct = 占比(基于 vol, 含中性盘分母); volume_ratio/change_pct 取自腾讯 Quote;
        # position = 现价位置(20日分位优先, 回退今日振幅, 再失败 unknown)
        "inner_outer": {
            "buy_amt": round(buy_amt),
            "sell_amt": round(sell_amt),
            "buy_pct": round(buy_vol / (buy_vol + sell_vol + m_vol) * 100, 1) if (buy_vol + sell_vol + m_vol) else None,
            "sell_pct": round(sell_vol / (buy_vol + sell_vol + m_vol) * 100, 1) if (buy_vol + sell_vol + m_vol) else None,
            "volume_ratio": (quote_dict or {}).get("volume_ratio"),
            "change_pct": (quote_dict or {}).get("change_pct"),
            "position": _estimate_position(symbol, quote_dict),
        },
        "segments": {k: round(v) for k, v in seg.items()},
        "tick_count": len(ticks),
        # 2026-09-04: 末笔时刻(运维可见性: tick 冻结时一眼看出, 配合 diag 暴露)。
        # 时刻为 "HH:MM:SS" 字符串, 字典序 max 即最晚。
        "last_tick_t": max((t.get("t", "") for t in ticks), default="") or None,
        # 2026-09-04 P0-2: 拉到页数(页码从0起, +1 为页数; 0=没拉到)。
        "tick_pages": _LAST_FETCH.get(code, {}).get("pages"),
        # 2026-08-12: 盘中数据量门槛 —— 竞价/开盘初期(非竞价成交<30笔)不算主力意图,
        # 直接给"数据不足"标记, 避免把竞价单/零星成交误判成吸筹派发
        # 2026-08-23 P4: suspect = 主力成交额超总成交额 130%(物理不可能, 疑重复计数),
        # 下游(insufficient 同款处理)不判吸筹/派发、不做 AI 意图解释
        "data_status": (
            "insufficient" if len(non_auction) < 30 else ("suspect" if data_suspect else "ok")
        ),
    }

    # ---- 价格维度(分价表) ----
    # 真实字段(2026-08-11 截图破解): 价~主动买量~总成交量~委托买量~委托卖量
    # 竞买率 = 主动买量/总成交量 → 每价位买盘强度
    try:
        from marketdata.vendors.tencent_panel import fetch_price_distribution
        prices = fetch_price_distribution(symbol, limit=70)
        if prices and len(prices) > 5:
            total_vol = sum(px["volume"] for px in prices)
            if total_vol > 0:
                vwap = sum(px["price"] * px["volume"] for px in prices) / total_vol
                result["vwap"] = round(vwap, 2)
                # 低价承接: 价格<VWAP 的成交量占比
                low_vol = sum(px["volume"] for px in prices if px["price"] < vwap)
                result["low_price_ratio"] = round(low_vol / total_vol, 3)

                # 吸筹价位: 主成交区(量>3万手)且竞买率>55% 的价位
                strong_buy_zones = []
                strong_sell_zones = []
                for px in prices:
                    vol = px.get("volume") or 0
                    buy_vol = px.get("buy_volume") or 0
                    if vol < 30000:
                        continue
                    ratio = buy_vol / vol * 100 if vol else 0
                    if ratio >= 55:
                        strong_buy_zones.append({"price": px["price"], "ratio": round(ratio, 1), "vol": round(vol)})
                    elif ratio <= 45:
                        strong_sell_zones.append({"price": px["price"], "ratio": round(ratio, 1), "vol": round(vol)})
                result["strong_buy_zones"] = strong_buy_zones[:6]   # 吸筹价位
                result["strong_sell_zones"] = strong_sell_zones[:6] # 抛压价位
    except Exception:
        pass

    if result.get("data_status") == "suspect":
        # P4: 数据异常时不给吸筹/派发结论, 防止翻倍净额被当成强吸筹推送
        result["signal"] = (
            f"⚠ 数据异常: 主力成交额超总成交额130%"
            f"(主力买+卖 {round((main_buy_amt + main_sell_amt) / 1e8, 2)}亿 vs "
            f"成交额 {round(_quote_turnover / 1e8, 2)}亿), 疑逐笔重复计数, 本轮不判意图"
        )
    else:
        result["signal"] = _judge_signal(
            dark_net, main_net, big_net, mid_net, retail_net, seg,
            result.get("low_price_ratio"),
            result.get("strong_buy_zones", []),
            result.get("strong_sell_zones", []),
            auction_amt, auction_vol,
            result.get("main_intensity"), result.get("main_buy_ratio"),
        )

    # 2026-09-04 P1-4: 结论翻转注记(同日上次结论 vs 本次)。变号或差超 5 千万
    # 则注记, 否则用户只看到数字变了会先怀疑系统坏了(如 -1.25亿→+3490万事故)。
    result["verdict_note"] = None
    try:
        _vd = _verdict_cache()
        _prev = _vd.get(code) or {}
        _cur = main_net or 0
        if _prev.get("day") == _cache_day() and isinstance(_prev.get("main_net"), (int, float)):
            _p = _prev["main_net"]
            if ((_p < 0) != (_cur < 0)) or abs(_cur - _p) > 5e7:
                result["verdict_note"] = (
                    f"结论更新: 上次净{('流出' if _p < 0 else '流入')}{abs(_p) / 1e8:.2f}亿 → "
                    f"本次净{('流出' if _cur < 0 else '流入')}{abs(_cur) / 1e8:.2f}亿(全量重拉/去重后)"
                )
        _vd.set(code, {"day": _cache_day(), "main_net": main_net})
    except Exception:  # noqa: BLE001
        pass

    # ---- 主力意图增强算法(2026-08-14): 超大单/大单背离 + 量价背离 + 时段节奏 ----
    # 2026-09-04 #3/#4: 收盘后快照存档(全天逐笔+结论)。白天不写; 同日一次;
    # 失败不影响主链路(函数内吞异常)。
    try:
        from src.core.tick_archive import maybe_archive_day
        maybe_archive_day(code, symbol.code, ticks, result)
    except Exception:  # noqa: BLE001
        pass
    # 三个独立纯函数(可单测), 注入 result 新字段, 不破坏现有字段。
    # change_pct 从腾讯 Quote 取(可能 None, 函数内部容错为不触发); seg 为已算出的四段净额。
    _change_pct = _num((quote_dict or {}).get("change_pct"))
    result["divergence"] = _detect_big_mid_divergence(big_net, mid_net, _change_pct)
    result["price_divergence"] = _detect_price_divergence(main_net, _change_pct)
    result["rhythm"] = _detect_rhythm(seg)

    # ---- 拆单识别(主力伪装的中小单) ----
    try:
        split = _detect_split_orders(ticks, prev_close=prev_close)
        result["split_order"] = split
    except Exception as e:
        logger.debug(f"拆单识别失败: {e}")

    # ---- 价位级承接分析(2026-08-11 用户洞察) ----
    # 找"大单卖+中小买"(主力砸散户接) 或 "大单买+中小卖"(主力吸筹) 的价位
    try:
        from collections import defaultdict
        by_price = defaultdict(lambda: {"big_buy": 0.0, "big_sell": 0.0, "small_buy": 0.0, "small_sell": 0.0})
        for tk in ticks:
            p = round(tk["price"], 2)
            is_big = tk["amt"] >= BIG_AMOUNT
            if is_big:
                if tk["d"] == "B": by_price[p]["big_buy"] += tk["amt"]
                elif tk["d"] == "S": by_price[p]["big_sell"] += tk["amt"]
            else:
                if tk["d"] == "B": by_price[p]["small_buy"] += tk["amt"]
                elif tk["d"] == "S": by_price[p]["small_sell"] += tk["amt"]
        # 主力吸筹位: 大单净买>800万 且 中小单净卖(散户割)
        absorb_zones, distribute_zones = [], []
        for p, d in by_price.items():
            total = d["big_buy"] + d["big_sell"] + d["small_buy"] + d["small_sell"]
            if total < 1000e4:  # 只留 1000万以上成交的价位
                continue
            big_net = d["big_buy"] - d["big_sell"]
            small_net = d["small_buy"] - d["small_sell"]
            if big_net > 800e4 and small_net < -300e4:
                absorb_zones.append({"price": p, "big_net": round(big_net), "small_net": round(small_net)})
            elif big_net < -800e4 and small_net > 300e4:
                distribute_zones.append({"price": p, "big_net": round(big_net), "small_net": round(small_net)})
        result["absorb_zones"] = sorted(absorb_zones, key=lambda x: -x["big_net"])[:6]
        result["distribute_zones"] = sorted(distribute_zones, key=lambda x: x["big_net"])[:6]
    except Exception as e:
        logger.debug(f"承接价位分析失败: {e}")

    # ---- 5日主力阶段(2026-08-11 用户洞察: 主力不可能一直买/散户不能一直卖) ----
    try:
        from marketdata.vendors.tencent_fundflow import TencentFundflowVendor
        cf = TencentFundflowVendor().fetch([symbol], {})[0]
        today_main = cf.main_net_inflow        # 今日主力净(腾讯口径, 元)
        main_5d = cf.main_net_5d               # 近5日主力净累计(元)
        if main_5d is not None:
            today_main = today_main or 0.0
            result["today_main_5d_net"] = round(today_main)
            result["main_5d_net"] = round(main_5d)
            if main_5d > 0 and today_main < 0:
                result["phase"] = "吸筹后转派发(5日净流入但今日流出, 主力开始获利了结)"
            elif main_5d > 0 and today_main > 0:
                result["phase"] = "持续吸筹(5日+今日均净流入)"
            elif main_5d < 0 and today_main > 0:
                result["phase"] = "派发后反弹(5日净流出但今日流入, 观察是否止跌)"
            elif main_5d < 0 and today_main < 0:
                result["phase"] = "持续派发(5日+今日均净流出)"
            else:
                result["phase"] = "阶段不明(数据不足)"
    except Exception as e:
        logger.debug(f"5日阶段判断失败: {e}")

    result["note"] = "v11: 拆单+套牢位+5日主力阶段(双向)"
    return result


def _judge_signal(dark_net: float, main_net: float, big_net: float, mid_net: float,
                  retail_net: float, seg: dict, low_ratio: float | None,
                  strong_buy: list | None = None, strong_sell: list | None = None,
                  auction_amt: float = 0.0, auction_vol: float = 0.0,
                  main_intensity: float | None = None, main_buy_ratio: float | None = None) -> str:
    """信号判定 v14: 主力买入强度(吸筹力度) + 净额方向。

    2026-08-11 二次修正(用户洞察: 同花顺暗盘流入多 = 主力在吸筹):
    - 同花顺"暗盘" ≈ 主力主动买入强度(占成交额 40-80%), 不是净额
    - 神剑: 主力买8.6亿(占40.6%)净额仅-2.9% → 判吸筹(同花顺一致), 不再判"托盘出货"
    - 主力买入强度 = 主力参与度%(占全市场成交) + 主力买占比%(买占主力成交)
    """
    threshold = MAIN_NET_LIMIT  # 500万
    tail = seg.get("tail", 0)
    low_boost = "低位承接" if (low_ratio or 0) > 0.4 else ""
    auction_note = f"竞价{auction_amt/1e4:.0f}万" if auction_amt > 0 else ""
    # 吸筹力度: 主力参与度>35% 且 主力买占比>48% = 强吸筹(阈值见模块顶部共享常量)
    strong_absorb = (main_intensity or 0) >= ABSORB_INTENSITY and (main_buy_ratio or 0) >= ABSORB_BUY_RATIO
    intensity_note = f"主力买占比{main_buy_ratio:.0f}%" if main_buy_ratio else ""

    # 主力净方向(≥20万)
    if main_net > threshold:
        if tail > 0:
            return f"主力净流入+尾盘加仓(吸筹){low_boost}|{auction_note}|{intensity_note}"
        return f"主力净流入(主动买占优){low_boost}|{auction_note}|{intensity_note}"
    if main_net < -threshold:
        # 净流出但主力参与度高(买入强度大) = 对倒换手/洗盘吸筹
        if strong_absorb:
            return f"主力净流出但参与度高({main_buy_ratio:.0f}%买占)疑洗盘吸筹|{auction_note}"
        if tail < 0:
            return f"主力净流出+尾盘抛压(出货)|{auction_note}"
        return f"主力净流出(主动卖占优)|{auction_note}"
    # 平衡: 看买入强度定吸筹/派发
    if strong_absorb:
        return f"主力平衡但参与度高({main_buy_ratio:.0f}%买占)疑吸筹|{auction_note}"
    return f"主力平衡(买卖接近)|{auction_note}"


# v0.4.79 口诀活代码化: .tck 主动率计算 helper
def compute_tck_active_ratio(symbol: str, tck_dir: str | None = None) -> float | None:
    """从 .tck 文件计算「主动率」(大单笔数占比)。

    路径: PANWATCH_TCK_DIR/{sh|sz}{code}_{yyyymmdd}.tck(盘后超盘回放落盘)。
    算法: orders(主动委托, tag=00 申报)中 amt>=30万 的笔数 / orders 总笔数 × 100。
    注: 30 万 = 1 手 = 100 股 × 3000 元(对齐神剑股份 002361 主力 1 手单位)。

    返回:
      - float 0-100: 主动率(可用)
      - None: 无 .tck 文件 / orders 为空 / 文件解析失败(调用方走兜底)

    集成路径: chat_tools.dark_review_from_tck 已用 parse_tck, 此函数复用其结果。
    性能: parse_tck 是 9ms 级(36字节定长 + zlib, 见 tdx_tick_parser 注释), 每次调用
    解析一次 OK, 不需要缓存(盘后场景调用频次低)。
    """
    import os
    from datetime import datetime
    try:
        from src.core.tdx_tick_parser import parse_tck
    except ImportError:
        return None
    if not tck_dir:
        tck_dir = os.environ.get("PANWATCH_TCK_DIR", "/app/data/tck")
    # 文件名规则: {sh|sz}{code}_{yyyymmdd}.tck
    today = datetime.now().strftime("%Y%m%d")
    # 判断市场: 6 开头 sh, 其他 sz
    market_prefix = "sh" if str(symbol).startswith("6") else "sz"
    fname = f"{market_prefix}{symbol}_{today}.tck"
    path = os.path.join(tck_dir, fname)
    if not os.path.exists(path):
        return None
    try:
        _, orders, _cancels = parse_tck(path)
    except Exception:
        return None
    if not orders:
        return None
    BIG_AMT = 30e4  # 30 万元 = 1 手大单阈值(神剑 002361 经验值)
    big = sum(1 for o in orders if (o.get("amt") or 0) >= BIG_AMT)
    return big / len(orders) * 100
