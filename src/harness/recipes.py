"""Harness 配方（内置智能体）加载器。

一个"内置智能体"不是新运行时，而是一份 YAML 配方：
    工具白名单 + 提示词片段 + 输出段落 + 预算

为什么用白名单而不是让模型自由选 55 个工具：模型会绕路（多点探测、重复取数），
既慢又贵；白名单把「这类问题该看哪几眼」固化下来，可审计、可回归。
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RECIPE_DIR = Path(__file__).resolve().parent / "recipes"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Recipe:
    id: str
    label: str
    icon: str = "🤖"
    desc: str = ""
    when: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    output_sections: tuple[str, ...] = ()
    max_tool_calls: int = 10
    prompt_fragment: str = field(default="")
    # 关键词打平时的优先级：风控/全景这类"更该赢"的意图显式抬高，不靠文件名顺序
    priority: int = 0
    # 确定性管线：true = 命中后由 harness 按 `tools` 顺序自己调工具，模型只写结论。
    # 实测同一问题工具集合 Jaccard 只有 0.57，让模型"选哪几个"就是方差源；
    # 管线化把这一步拿走，Jaccard 天然 = 1.0。
    pipeline: bool = False
    # 配方的天然口径（2026-10-23）：
    #   "symbol" = 问题是"某只票怎么样"，管线需要能解析出代码（symbols_of）；
    #   "market" = 问题是"今天市场怎么样"，没有标的 —— 交给 pipeline_market 跑全市场取数，
    #              要 symbol 的工具按用户自选/持仓展开，展不开就如实写「未取到」。
    # 只是**声明**，不改变路由；真正决定走哪条管线的仍是 `pipeline` + 这个字段的组合。
    scope: str = "symbol"
    # 本配方能取的**数据域**（`query_data_domain` 的 domain id）。用途只有一个：
    # 当关键词得分被一份 pipeline 配方压过时，告诉路由「这份配方能真取到这些域」——
    # pipeline 按 {} 调工具，必填 domain 的域工具会被判 unsatisfiable 静默跳过。
    covers_domains: tuple[str, ...] = ()
    # 本配方获准**接管**哪些 pipeline 配方（按配方 id 点名）。空 = 不接管任何管线：
    # 「能取到更多域」本身不是抢路由的理由，否则「持仓要不要减，看看席位」这种附带一句
    # 「席位」的问题会被换到龙虎榜配方上，持仓/盘中工具全丢。只有明确列出的管线允许被接管。
    rescues_pipelines: tuple[str, ...] = ()

    def public(self) -> dict:
        """给前端 /api/copilot/agents 的元信息（不含提示词正文，避免泄露与大包体）。"""
        return {
            "id": self.id,
            "label": self.label,
            "icon": self.icon,
            "desc": self.desc,
            "when": list(self.when),
            "tool_count": len(self.tools),
            "tools": list(self.tools),
            "output_sections": list(self.output_sections),
            "scope": self.scope,
            "pipeline": self.pipeline,
            # 2026-10-25 (P2-2.1): 内容指纹 = 版本。改一行 YAML 指纹就变，
            # 不靠人工维护 version 号（人工编号必然忘记改）。
            "version": recipe_fingerprint(self.id),
            "updated_at": recipe_updated_at(self.id),
        }


def _parse_scope(raw) -> str:
    """`scope` 只认 symbol / market；写错就当默认（symbol = 旧行为），并留一行日志。

    宁可退回旧行为，也不许把一份单标的配方误判成全市场配方 —— 后者会在
    没有自选时静默少跑一半工具，用户只看到一堆「未取到」。
    """
    val = str(raw or "symbol").strip().lower()
    if val in ("symbol", "market"):
        return val
    logger.warning("配方 scope=%r 非法，按 symbol 处理", raw)
    return "symbol"


def _parse(path: Path) -> Recipe:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    budget = raw.get("budget") or {}
    return Recipe(
        id=str(raw.get("id") or path.stem),
        label=str(raw.get("label") or path.stem),
        icon=str(raw.get("icon") or "🤖"),
        desc=str(raw.get("desc") or ""),
        # YAML 1.1 会把 `9:25` 解析成六十进制整数 565 —— 所有标签一律强制转字符串，
        # 否则 ' '.join(when) 直接 TypeError（`/mentions` 线上 500 就是这么来的）。
        when=tuple(str(x) for x in (raw.get("when") or ())),
        tools=tuple(str(x) for x in (raw.get("tools") or ())),
        skills=tuple(str(x) for x in (raw.get("skills") or ())),
        priority=int(raw.get("priority") or 0),
        pipeline=bool(raw.get("pipeline") or False),
        scope=_parse_scope(raw.get("scope")),
        output_sections=tuple(str(x) for x in (raw.get("output_sections") or ())),
        max_tool_calls=int(budget.get("max_tool_calls") or 10),
        prompt_fragment=str(raw.get("prompt_fragment") or "").strip(),
        covers_domains=tuple(str(x) for x in (raw.get("covers_domains") or ())),
        rescues_pipelines=tuple(str(x) for x in (raw.get("rescues_pipelines") or ())),
    )


@functools.lru_cache(maxsize=1)
def load_recipes() -> dict[str, Recipe]:
    """读目录下全部配方（进程内缓存；改 YAML 后重启生效）。"""
    out: dict[str, Recipe] = {}
    if not RECIPE_DIR.exists():
        return out
    
    # 技能锁校验：加载时校验 recipe.skills ⊂ lock，缺失即 raise（异常信息带配方 id 与缺失技能名）
    from src.harness.skills import verify_lock
    import json
    lock_data = {}
    try:
        from src.harness.skills import _LOCK_PATH
        if _LOCK_PATH.exists():
            lock_data = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass

    for p in sorted(RECIPE_DIR.glob("*.yaml")):
        try:
            r = _parse(p)
        except Exception as exc:
            logger.warning("配方解析失败, 已跳过: %s (%s)", p.name, exc)
            continue
            
        # 校验 skills 是否在 lock 中存在
        for sk in r.skills:
            if lock_data and sk not in lock_data:
                raise ValueError(f"配方 '{r.id}' 引用了不存在或未锁定的技能 '{sk}' (skills.lock.json 中缺失)")
                
        out[r.id] = r
    return out


# ── 配方版本（P2-2.1, 2026-10-25）──────────────────────────────────────────
# 为什么用内容指纹当版本号：手写 `version: 3` 必然忘记改，而且改了也会和实际内容不同步。
# 指纹 = 文件内容 sha256 前 8 位，改一个字就变，可复核（拿文件自己算得出同一个值）。


def _recipe_path(recipe_id: str) -> Path | None:
    p = RECIPE_DIR / f"{recipe_id}.yaml"
    return p if p.is_file() else None


def recipe_fingerprint(recipe_id: str) -> str | None:
    """配方内容指纹（sha256 前 8 位）。文件不存在返回 None，不编造。"""
    import hashlib

    p = _recipe_path(recipe_id)
    if p is None:
        return None
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:8]
    except OSError:
        return None


def recipe_updated_at(recipe_id: str) -> str | None:
    """配方文件最后修改时间（ISO，本地时区）。取不到返回 None。"""
    import datetime as _dt

    p = _recipe_path(recipe_id)
    if p is None:
        return None
    try:
        ts = p.stat().st_mtime
        return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except OSError:
        return None


def catalog() -> list[dict]:
    """全部配方的目录（给 `/copilot/agents`）。含版本指纹与工具明细。"""
    return [r.public() for r in list_recipes()]


def get_recipe(recipe_id: str | None) -> Recipe | None:
    if not recipe_id:
        return None
    return load_recipes().get(recipe_id.strip())


def list_recipes() -> list[Recipe]:
    return sorted(load_recipes().values(), key=lambda r: r.id)


def match_recipe(text: str) -> Recipe | None:
    """按 when 关键词把问题**确定性地**路由到一份配方（2026-10-21）。

    为什么要确定性路由：实测同一问题重跑三次，工具集合 Jaccard 只有 0.52，
        「全景深度诊断002050」三次分别调 4 / 0 / 3 个工具 —— 让模型自由选工具
        就是最大的方差源。命中关键词就固定工具清单，模型只能在清单内挑。

    打分：命中关键词的**字符总长度**（越长越具体，避免"盘口"这种短词抢走"撤单趋势"）。
    例外：胜出的是**全市场** pipeline 配方时，若另有非 pipeline 配方的
    `rescues_pipelines` **点名了这条管线**（且它声明了 covers_domains、关键词也命中），
    才改走后者 —— 管线按 `{}` 调工具，必填 `domain` 的 `query_data_domain` 会被判
    unsatisfiable 静默跳过，「收盘复盘+龙虎榜和涨停梯队」里的龙虎榜就永远取不到。

    为什么必须按管线 id 点名、不能「只要赢家是市场级管线就抢」：关键词打平时谁先命中
    谁赢，「持仓要不要减，看看席位」（持仓 4 分 / 席位 4 分）会落到 intraday_watcher，
    再被无条件改跳到龙虎榜配方，`get_portfolio` 与盘中工具全被丢掉 —— 用户问的是减仓，
    拿到的却是一份龙虎榜复盘。这不是「补一个域」，而是换了问题。

    为什么限定 `scope == "market"`：单标的管线（深度诊断/空头司令）有自己的固定证据链，
    「深度诊断 002361 龙虎榜」里的龙虎榜只是其中一个维度，改走市场级数据域配方会把
    其余 13 个维度全丢掉 —— 那是另一种失真，不能拿一个缺口换另一个。
    """
    if not text:
        return None
    best: tuple[int, int, int, Recipe] | None = None
    domain_alts: list[tuple[int, int, int, Recipe]] = []
    for r in list_recipes():
        hits = [k for k in r.when if k and k in text]
        if not hits:
            continue
        # 长关键词的权重按平方放大：'深度诊断'(4) 应当压过 '盘口'(2) 这类泛词
        score = sum(len(k) ** 2 for k in hits)
        key = (score, max(len(k) for k in hits), r.priority, r)
        if best is None or (key[0], key[1], key[2]) > (best[0], best[1], best[2]):
            best = key
        if not r.pipeline and r.covers_domains:
            domain_alts.append(key)
    if best is None:
        return None
    winner = best[3]
    if winner.pipeline and winner.scope == "market" and domain_alts:
        # 只有**点名**了这条管线的配方才许接管；没点名的（持仓/盘中/竞价）保持原路由。
        allowed = [k for k in domain_alts if winner.id in k[3].rescues_pipelines]
        if allowed:
            alt = max(allowed, key=lambda k: (k[0], k[1], k[2]))
            logger.info(
                "配方改走数据域配方: %s(score=%s) 是 pipeline 取不到域, 改 %s(covers=%s)",
                winner.id, best[0], alt[3].id, ",".join(alt[3].covers_domains),
            )
            return alt[3]
    return winner


def filter_schemas(schemas: list[dict], allowed: frozenset[str] | set[str] | None) -> list[dict]:
    """把全量工具 schema 过滤成配方白名单（None = 不过滤）。

    ⚠️ 白名单必须**fail-closed**（失效即收紧, 不失效即全放开）：
    早先的实现是 `return out or schemas` —— 白名单里一个工具都对不上时
    回落到全量, 也就是「配方写错了 → 把全部工具交给模型」。
    这正是白名单机制要防的事, 语义完全反了。

    现在: 白名单非空但一个都没命中 → 返回空列表(该配方无工具可用),
    并记 warning 指名是哪些工具名没对上。宁可少给, 不可乱给。
    """
    if not allowed:
        # allowed 为空/None = 显式不过滤(非配方路径, 如 forced 模式)
        return schemas
    out = []
    missing = []
    known = set()
    for sc in schemas:
        name = ((sc or {}).get("function") or {}).get("name")
        if not name:
            continue
        known.add(name)
        if name in allowed:
            out.append(sc)
    if not out:
        unknown = sorted(set(allowed) - known)
        logger.warning(
            "配方白名单一个工具都没命中, 已收紧为 0 工具(不再回退全量): 未知工具名=%s",
            unknown or "(白名单为空)",
        )
    return out
