"""代码 → 股票名称的统一解析(2026-10-21)。

## 为什么单开一层
`GET /api/decisions/log` 这类账本只落 `symbol`, 页面于是只能显示 6 位代码 —— 用户读不出
"这是哪只票"。中文名的**唯一权威来源**是全仓已有的
`src.collectors.stock_list.get_stock_list()`(东财全市场列表, 落 `data/stock_list_cache.json`,
TTL 7 天)。本模块把"代码 → 名字"收敛到一处: 免得每个 API 各拼一份索引, 也免得有人在取不到
名字时**拿代码冒充名字**或返回空串。

## 口径(与全仓"取不到就 NULL"一致)
- 取不到名字的 symbol **不出现在返回 dict 里**(调用方自己显示 `--`), 绝不编造、不返回空串。
- 入参允许带市场前后缀(`sz002050` / `002050.SZ` / `USZA300033`), 内部统一剥成 6 位码查;
  **输出 key 保留调用方原写法**, 前端可直接 `name = names[symbol]`。
- 进程内索引缓存 TTL 10 分钟; 抓取失败只缓存 60 秒(别让每个请求都去撞一次网络, 也别把
  一次网络抖动钉死 10 分钟)。
- 首次建索引在**后台线程**里做, 最多等 `FIRST_BUILD_WAIT_S` 秒: 名字只是展示增强, 不能让一次
  全市场抓取(实测 ~17s, 5 个市场多点分页)把账本页请求拖住。抓完自然进缓存, 之后毫秒级
  (温缓存下首次 ~0.16s、命中 0.02ms)。
- **不许 import src.web**(core→web 反向依赖棘轮门禁 `tests/test_w41_core_web_dependency.py`)。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: 索引缓存 TTL(秒): 10 分钟。列表本身 7 天一刷, 这里只挡"每个请求重建 3 万条索引"的开销。
CACHE_TTL_S = 600.0

#: 抓取失败后的重试间隔(秒): 短一些, 网络恢复后尽快可用, 又不至于把请求全打在下游。
FAILURE_TTL_S = 60.0

#: 首次建索引的最大阻塞时间(秒)。实测: 温缓存(读 JSON + 建 3 万条索引) ~0.16s, 冷启动走网络
#: 全市场抓取 ~17s —— 超过本值就先把已有(空)索引给出去, 后台线程继续抓, 下一次调用即命中。
#: 名字只是展示增强, 绝不能让一次抓取把账本页/测试进程卡住。
FIRST_BUILD_WAIT_S = 3.0

#: 代码里的数字段(用于剥 `sz002050` / `002050.SZ` / `USZA300033` 这类写法)
_DIGITS_RE = re.compile(r"\d+")

_lock = threading.RLock()
#: market(大写) → {6 位码: 名字}; 键 "" 是全市场合并索引
_indexes: dict[str, dict[str, str]] = {}
#: 上次建索引(成功或失败)的时刻; 0 = 从未建过
_built_at: float = 0.0
#: 上次建索引是否成功(决定用 CACHE_TTL_S 还是 FAILURE_TTL_S)
_ok: bool = False
#: 是否已有线程在建(避免并发重建/N 个请求各抓一次全市场)
_building: bool = False


def get_stock_list() -> list[dict]:
    """默认取数口: 运行时 import 采集器(单测可 monkeypatch **本名** 换掉数据源)。

    写成函数而不是模块级 `from ... import get_stock_list`, 是为了让
    `monkeypatch.setattr(src.core.stock_names, "get_stock_list", fake)` 生效,
    同时保持默认路径仍是既有采集器(不新建采集)。
    """
    from src.collectors.stock_list import get_stock_list as _get

    return _get()


def normalize_symbol(raw: Any, market: str = "CN") -> str:
    """任意写法 → 查询用的 6 位码; 认不出返回空串(**不猜**)。

    - `sz002050` / `002050.SZ` / `USZA300033` → `002050` / `002050` / `300033`;
    - A 股代码恒为 6 位: `market=CN` 时 5 位及以下的数字段**不当 6 位码补零猜**
      (否则 `1` 会被猜成 `000001`、港股 `00700` 会被猜成 A 股 `000700`);
    - 纯字母(美股 ticker)在 A 股索引里无解 → 空串。
    """
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    runs = _DIGITS_RE.findall(s)
    if not runs:
        return ""
    digits = max(runs, key=len)
    if len(digits) == 6:
        return digits
    if (market or "CN").strip().upper() == "CN":
        return ""
    # 非 A 股(港股 5 位等): 补齐/截断到 6 位, 与建索引时同一规则 → 两侧可对上
    return digits[-6:].zfill(6)


def _index_one(stocks: list[dict]) -> dict[str, dict[str, str]]:
    """列表 → {market: {码: 名}} + 全市场合并索引(键 "")。取不到名字的行直接跳过。"""
    out: dict[str, dict[str, str]] = {"": {}}
    for row in stocks or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        sym = str(row.get("symbol") or "").strip()
        if not name or not sym:
            continue  # 没名字/没代码的行不进索引(绝不编造)
        mkt = str(row.get("market") or "CN").strip().upper() or "CN"
        code = normalize_symbol(sym, mkt)
        if not code:
            continue
        out.setdefault(mkt, {}).setdefault(code, name)
        out[""].setdefault(code, name)  # 先到先得(采集器 CN 在前), 合并索引只做兜底
    return out


def _build() -> tuple[dict[str, dict[str, str]], bool]:
    """抓 + 建索引。**永不抛** —— 采集器失败就当"没有名字"(ok=False)。"""
    try:
        stocks = get_stock_list()
    except Exception as e:  # noqa: BLE001 —— 网络/DNS/JSON 任何异常都不该冒到 API 层
        logger.warning("股票名称索引构建失败(取股票列表异常): %s", e)
        return {}, False
    try:
        indexes = _index_one(list(stocks or []))
    except Exception as e:  # noqa: BLE001
        logger.warning("股票名称索引构建失败(数据形态异常): %s", e)
        return {}, False
    if not indexes.get(""):
        logger.warning("股票名称索引为空(采集器返回 0 条可用数据)")
        return indexes, False
    return indexes, True


def _commit(indexes: dict[str, dict[str, str]], ok: bool) -> None:
    global _indexes, _built_at, _ok, _building
    with _lock:
        if not ok and _indexes:
            # 刷新失败但手里有旧索引: 继续用旧的(公司改名极少), 只把重试间隔缩到 FAILURE_TTL_S ——
            # 一次网络抖动不该让线上页面突然全变回代码。
            _built_at = time.time()
            _ok = False
            _building = False
            return
        _indexes = indexes
        _built_at = time.time()
        _ok = ok
        _building = False


def _worker() -> None:
    try:
        indexes, ok = _build()
    except Exception:  # noqa: BLE001 —— 兜底: 后台线程绝不能把 _building 永久挂住
        indexes, ok = {}, False
    _commit(indexes, ok)


def _ensure_indexes() -> dict[str, dict[str, str]]:
    """拿当前索引(必要时建一次)。TTL 内直接返回, 毫秒级。"""
    global _building
    with _lock:
        ttl = CACHE_TTL_S if _ok else FAILURE_TTL_S
        if _built_at and (time.time() - _built_at) < ttl:
            return _indexes
        if _building:
            # 已有线程在抓: 不再起第二个, 先用现有索引(可能为空/略旧)
            return _indexes
        _building = True

    th = threading.Thread(target=_worker, name="stock-names-build", daemon=True)
    th.start()
    th.join(FIRST_BUILD_WAIT_S)  # 有界等待: 冷启动别拖住请求
    with _lock:
        return _indexes


def names_for(symbols: list[str], market: str = "CN") -> dict[str, str]:
    """symbols → {symbol: name}。取不到的 symbol **不出现在结果里**(不编造、不返回空串)。

    输出 key 就是调用方传进来的原写法(`sz002050` 进 → `sz002050` 出)。
    """
    wanted: dict[str, str] = {}
    for raw in symbols or []:
        if raw is None:
            continue
        key = str(raw).strip()
        if not key:
            continue
        code = normalize_symbol(key, market)
        if code:
            wanted[key] = code
    if not wanted:
        return {}

    indexes = _ensure_indexes()
    key_mkt = (market or "CN").strip().upper() or "CN"
    idx = indexes.get(key_mkt)
    if idx is None:
        idx = indexes.get("")  # 该市场没有单独索引(或整表只抓到一个市场)时用合并索引兜底
    if not idx:
        return {}
    out: dict[str, str] = {}
    for raw, code in wanted.items():
        name = idx.get(code)
        if name:  # 只有真拿到名字才写; 缺失 → 键不存在
            out[raw] = name
    return out


def reset_cache() -> None:
    """清空进程内索引(测试/运维强制重读用)。"""
    global _indexes, _built_at, _ok, _building
    with _lock:
        _indexes = {}
        _built_at = 0.0
        _ok = False
        _building = False
