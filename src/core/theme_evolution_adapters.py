"""题材演化观察层: **真实数据源 payload 适配器**(纯映射, 依赖全部注入, 默认不联网)。

## 这个模块干什么

真实采集源返回的是它们自己的形状, `theme_evolution_capture.plan_capture` 只接受窄契约。
本模块只做**格式映射** —— 把下面四种真实 payload 转成采集契约, 自己**不取数**:

| 真实源(源码位置) | 真实形状 | 映射结果 | 本层能否证明有效日 |
|---|---|---|---|
| `tdx_boards.sector_items()` | `[{code,name,board_type}]`, code 带 .SH/.SZ | 目录条目 | — |
| `tdx_boards.constituents(code)` | `['600519.SH', ...]` / `None` | 成员列表 | **不能**: 接口只返回"当前"成分 |
| `MarketSentimentCollector.get_limit_up_pool(day)` | 每行 `as_of`/`as_of_basis`/`pool_coverage` | 逐行原样保留 | 只保留**源自报** |
| `tdx_boards.latest_trade_date()` | `'YYYY-MM-DD'` | `'YYYYMMDD'` | 只说明"源更新到哪天", **不是**成员有效日 |

## 硬约束(违反即视为 bug)

1. **依赖注入, 缺即拒**: 所有 raw 取数函数必须由调用方显式传入。`build_capture_deps`
   没收到某个源就**不产出**对应 key, 上游因此结构化拒绝(`deps_not_injected`); 传了
   非 callable 立刻抛 `AdapterDependencyMissing`。模块 import 期不拉行情/采集器/HTTP/DB。
2. **无时钟/无网络/无库/无文件**: 模块内没有任何取时调用, 不发请求, 不建连接, 不落盘。
   时间与新鲜度只能由调用方显式给。
3. **函数签名即证据**: 四个 `adapt_*` 只接受一个 `raw` 参数 —— **拿不到目标日与行情日**,
   所以结构上不可能用请求日/行情日去补 `as_of` / `data_day`。
4. **日期只来自源自报**: 涨停池逐行保留 `as_of`/`as_of_basis`(不统一、不推公共日期、
   不补缺行); 板块成员只认源**显式**给出的 `data_day`+`date_basis`, 其余一律留空,
   由上游记 unknown。板块 payload 里的 `as_of`(池/行情口径)不作为成员有效日。
5. **保留原码与覆盖声明**: 证券/板块代码原样透传(不去后缀、不改大小写、不裁空格 ——
   非法值交给上游拒绝); `pool_coverage` 原样透传(dict 含 `None` 不补 0, 字符串
   `'complete'` 不改写为 dict)。
6. **unknown 不伪造**: `None` / `[]` / 缺字段 / 非映射行原样透传, 由上游判 unknown。
   空池 ≠ 0 只涨停; 取不到 ≠ 板块没有成员。
7. **错误不泄露**: 异常只带结构化 code, 不含 payload、URL、token 或底层异常消息
   (底层异常的类型名已足够定位, 消息交给原异常自己抛)。
8. **不声明新鲜度、不宣称 PIT**: 本模块**不产出** `source_fresh`(缺失即未知, 绝不
   默认 True), 也不做「时点还原」。`as_of` 只是源自报的日期, 既**不能证明当日收盘后
   已定稿**, 也不是可回放的 point-in-time 成分。真实限制集中写在 `KNOWN_GAPS`。

## 边界(未验证)

- 未做**真实网络**联跑: 本文件与测试都用参照源码手工转录的 payload, 没有连接 TQ 网关,
  也没有连真实库。
- 未验证供应商是否撒谎: `as_of` / `pool_coverage` 都只是源自报。
- 未处理并发与重试: 那属于 store 与调用方。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Mapping

__all__ = [
    "ADAPTER_RULE_VERSION",
    "AdapterDependencyMissing",
    "AdapterPayloadInvalid",
    "CODE_CATALOG_NOT_A_SEQUENCE",
    "CODE_MEMBERS_MISSING",
    "CODE_MEMBERS_UNSUPPORTED",
    "CODE_POOL_NOT_A_SEQUENCE",
    "CODE_RAW_SOURCE_NOT_CALLABLE",
    "CODE_RAW_SOURCE_NOT_INJECTED",
    "KNOWN_GAPS",
    "ThemeAdapterError",
    "adapt_board_catalog",
    "adapt_limit_up_pool",
    "adapt_members",
    "adapt_trade_date",
    "board_catalog_fetcher",
    "build_capture_deps",
    "limit_up_pool_fetcher",
    "member_fetcher",
    "trade_date_fetcher",
]

#: 适配层规则版本(与 capture / store / agent 各自的版本并列, 不互相顶替)。
ADAPTER_RULE_VERSION = "theme_evo_adapter_v1"

#: 结构化错误码(不含任何取值/凭据)。异常消息**就是**这些 code。
CODE_RAW_SOURCE_NOT_INJECTED = "raw_source_not_injected"
CODE_RAW_SOURCE_NOT_CALLABLE = "raw_source_not_callable"
CODE_CATALOG_NOT_A_SEQUENCE = "board_catalog_not_a_sequence"
CODE_POOL_NOT_A_SEQUENCE = "pool_payload_not_a_sequence"
CODE_MEMBERS_MISSING = "members_missing_in_result"
CODE_MEMBERS_UNSUPPORTED = "members_payload_unsupported"

#: 本层已知且**未验证**的缺口(随代码一起交付, 不只写在文档里)。
KNOWN_GAPS = (
    "member_effective_day_unknown: tq 板块接口只返回当前成分, 无成员生效日期 ⇒ 成员日期恒 unknown",
    "as_of_not_proof_of_close: 池 as_of 来自日K末根/请求日, 不能证明当日收盘后已定稿",
    "pool_coverage_self_declared: 覆盖完整度由源自报, 未经独立核验",
    "no_live_network_verification: 本层未对真实网关/真实库联跑, payload 形状为源码转录",
)

#: 证据契约字段白名单。**只映射这些** —— 价格/连板数等未核验的派生字段不得混进证据批次。
_POOL_KEYS = (
    "code",
    "stock_code",
    "name",
    "source",
    "as_of",
    "as_of_basis",
    "pool_coverage",
    # 题材归属(同源同行, 采集时刻的当前成员关系): 只搬字段, 不判断含义。
    "sector",
    "concepts",
    "block_as_of",
    "block_source",
)
_BOARD_KEYS = ("code", "name", "board_type")
#: 成员日期只认这两个显式字段名; 其它任何"日期"字样(如 as_of/trade_date)一律不读。
_MEMBER_DATE_KEYS = ("data_day", "date_basis")

_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YMD_RE = re.compile(r"^\d{8}$")


# ── 错误: 只带 code ─────────────────────────────────────────────────────
class ThemeAdapterError(ValueError):
    """适配层错误。`str(exc)` / `args` **只有**结构化 code, 绝不带 payload。"""

    code: str = "adapter_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(self.code)


class AdapterDependencyMissing(ThemeAdapterError):
    """raw 取数函数没注入 / 不是 callable ⇒ 直接拒, 不偷偷回退到模块内的实现。"""

    code = CODE_RAW_SOURCE_NOT_INJECTED


class AdapterPayloadInvalid(ThemeAdapterError):
    """payload 形状无法映射成契约(类型层面, 不做语义修补)。"""

    code = "adapter_payload_invalid"


# ── 纯映射 ──────────────────────────────────────────────────────────────
def adapt_board_catalog(raw: Any) -> list[Any]:
    """`sector_items()` → 采集契约目录条目。

    code/name/board_type 原样复制(**不规范化**); 无法识别的条目原样透传, 由上游记
    `catalog_anomaly` —— 这里不静默丢数据, 也不伪造 code。
    """
    if not isinstance(raw, (list, tuple)):
        raise AdapterPayloadInvalid(CODE_CATALOG_NOT_A_SEQUENCE)
    out: list[Any] = []
    for item in raw:
        if not isinstance(item, Mapping):
            out.append(item)
            continue
        out.append({key: item[key] for key in _BOARD_KEYS if key in item})
    return out


def adapt_members(raw: Any) -> list[Any] | dict[str, Any] | None:
    """`constituents()` → 采集契约成员结果。

    - `None`(取不到) → `None`(不是空板块);
    - 代码序列 → 原样 `list`(**不带任何日期** ⇒ 上游 `data_day`/`date_basis` 为 None);
    - Mapping → 必须有 `members`; 日期只认 `data_day`/`date_basis` 且**照抄**,
      缺一半就让它缺着(上游会记 incomplete), 本层不补、不猜、不解析。
    """
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        if "members" not in raw:
            raise AdapterPayloadInvalid(CODE_MEMBERS_MISSING)
        members = raw["members"]
        if not isinstance(members, (list, tuple)):
            raise AdapterPayloadInvalid(CODE_MEMBERS_UNSUPPORTED)
        out: dict[str, Any] = {"members": list(members)}
        for key in _MEMBER_DATE_KEYS:
            if key in raw:
                out[key] = raw[key]
        return out
    if isinstance(raw, (list, tuple, set, frozenset)):
        return list(raw)
    raise AdapterPayloadInvalid(CODE_MEMBERS_UNSUPPORTED)


def adapt_limit_up_pool(raw: Any) -> list[Any] | None:
    """涨停池 payload → 采集契约, **逐行**保留 `as_of` / `as_of_basis` / `pool_coverage`。

    - `None` → `None`(取不到; 上游记 unknown, 不是 0 只涨停);
    - `[]` → `[]`(空池 ⇒ 上游 unknown);
    - 非映射行原样透传(上游记 `pool_row_not_mapping`);
    - 不做 `_common_as_of` 式的统一日期推断: 行间不一致就让它不一致, 由上游判 unknown。
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise AdapterPayloadInvalid(CODE_POOL_NOT_A_SEQUENCE)
    out: list[Any] = []
    for row in raw:
        if not isinstance(row, Mapping):
            out.append(row)
            continue
        out.append({key: row[key] for key in _POOL_KEYS if key in row})
    return out


def adapt_trade_date(raw: Any) -> str | None:
    """`latest_trade_date()` 的 `'YYYY-MM-DD'` → `'YYYYMMDD'`; 解析不了 → `None`(不猜)。

    只用于回答\"源更新到哪一天\"(`source_trade_date`), **永远不是**成员有效日。
    """
    text = str(raw or "").strip()
    if _ISO_DAY_RE.match(text):
        text = text.replace("-", "")
    if not _YMD_RE.match(text):
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


# ── 注入包装: 生成 plan_capture 需要的取数函数 ──────────────────────────
def _require_source(fetch_raw: Any) -> Callable[..., Any]:
    if not callable(fetch_raw):
        raise AdapterDependencyMissing(CODE_RAW_SOURCE_NOT_CALLABLE)
    return fetch_raw


def board_catalog_fetcher(fetch_raw: Any) -> Callable[[], list[Any]]:
    """把 raw 目录取数函数包成 `fetch_boards`。异常原样抛出(上游只记类型名)。"""
    source = _require_source(fetch_raw)

    def _fetch() -> list[Any]:
        return adapt_board_catalog(source())

    return _fetch


def member_fetcher(fetch_raw: Any) -> Callable[[str], Any]:
    """把 raw 成分取数函数包成 `fetch_members`。板块代码原样透传给源。"""
    source = _require_source(fetch_raw)

    def _fetch(block_code: str) -> Any:
        return adapt_members(source(block_code))

    return _fetch


def limit_up_pool_fetcher(fetch_raw: Any) -> Callable[[str], Any]:
    """把 raw 涨停池取数函数包成 `fetch_pool`。

    目标日**只**作为请求参数透传给源(真实实现按日回溯取数), 绝不用来回填 `as_of`。
    """
    source = _require_source(fetch_raw)

    def _fetch(day: str) -> Any:
        return adapt_limit_up_pool(source(day))

    return _fetch


def trade_date_fetcher(fetch_raw: Any) -> Callable[[], str | None]:
    """把 raw 交易日取数函数包成 `fetch_trade_date`(结果归一成 YYYYMMDD 或 None)。"""
    source = _require_source(fetch_raw)

    def _fetch() -> str | None:
        return adapt_trade_date(source())

    return _fetch


def build_capture_deps(
    *,
    board_catalog: Any = None,
    members: Any = None,
    limit_up_pool: Any = None,
    trade_date: Any = None,
) -> dict[str, Callable[..., Any]]:
    """把可得的真实源组装成 `plan_capture` / `run_once` 的注入依赖。

    - 没传(或传 None)的源**不产出 key** ⇒ 上游 `deps_not_injected` 结构化拒绝;
    - 传了非 callable ⇒ 立刻抛 `AdapterDependencyMissing`(fail-closed);
    - **不产出** `source_fresh`(本层不声称新鲜) 与 `source_trade_date`(那是值, 由调用方显式给);
    - 不产出 `now` —— 采集时刻同样只能由调用方显式传入。

    用法: `run_once(now=..., bind=..., source_fresh=..., **build_capture_deps(...))`。
    """
    deps: dict[str, Callable[..., Any]] = {}
    if board_catalog is not None:
        deps["fetch_boards"] = board_catalog_fetcher(board_catalog)
    if members is not None:
        deps["fetch_members"] = member_fetcher(members)
    if limit_up_pool is not None:
        deps["fetch_pool"] = limit_up_pool_fetcher(limit_up_pool)
    if trade_date is not None:
        deps["fetch_trade_date"] = trade_date_fetcher(trade_date)
    return deps
