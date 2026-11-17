"""标的与取数护栏（2026-10-21 线上幻觉事故后加）。

事故：用户问「全景数智深度诊断强锐技术和三花智控」，模型**一次工具都没调**，
直接编出「北方稀土 + 688013」两只完全无关的票，还给了现价与支撑位。
根因不是提示词不够温柔，而是"允许模型不取数就作答"这条缝。

本模块只做两件确定性的事（不猜、不兜底生成）：
1. `is_stock_question()`：判断这句话是不是在问具体标的（6 位代码或股票名后缀词）。
2. `resolve_names()`：把用户写的名字拿去 TQ `get_match_stkinfo` 核对；
   写错/简称不一致时返回可回填的更正说明（如 强锐技术 → 301128 强瑞技术）。

另有两个**只用于归属判定**的旁路（2026-11-17 三轮审计补齐，不改变上面的口径）：
`is_generic_reference()` 分出"泛指眼前这只"（这家银行）而不是点名；
`bare_name_codes()` 只做**正向确认**：没有后缀词的名字（贵州茅台）在整段命中行情库时
才算用户点了名，查不到一律不算 —— 增广覆盖，不猜标的。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 6 位 A 股代码（300/301/688/6/0/8 开头，前后不能紧贴数字）
_CODE_RE = re.compile(r"(?<!\d)((?:00|30|60|68|83|87|43)\d{4})(?!\d)")

# 股票名常见后缀：命中即视为"在问某只票"
_NAME_SUFFIX = (
    "技术", "科技", "股份", "集团", "电子", "医药", "生物", "银行", "证券",
    "能源", "材料", "智控", "精密", "控股", "实业", "传媒", "环保", "重工",
    "汽车", "电器", "光电", "通信", "软件", "食品", "药业", "新材", "国际",
    "机械", "化工", "钢铁", "水泥", "航空", "船舶", "核电", "锂业", "稀土",
)

# 抓取候选名：后缀前带 2~4 个汉字
_NAME_RE = re.compile(r"([\u4e00-\u9fa5]{2,4}(?:" + "|".join(_NAME_SUFFIX) + r"))")

# 这些词本身不是标的，别拿去查
_STOPWORDS = {"我的持仓", "自选股", "大盘", "两市", "板块", "行业", "指数", "概念"}

# 抓名字时会把前面的连接词/动词带进来（"诊断强锐技术" / "和三花智控"），先剥掉
_LEAD_NOISE = ("诊断", "分析", "看看", "看一下", "查一下", "点评", "研究", "跟踪")
_LEAD_CHARS = "和与及跟、，,。 的了对在把"

# 「泛指当前这只」的前缀：抓到后缀词，但说话人指的是**眼前这张图**，不是在点名。
# 为什么必须分出来（2026-11-17 三轮审计）：快照归属把"名字解析失败"当 fail-closed，
# 如果连「这家银行」「整个医药」也算"点了名却解析失败"，用户对着浦发银行图表问
# 「这家银行后面怎么走」就会被整份丢弃 —— 那是把泛指误杀成歧义。
_GENERIC_PREFIXES = (
    "这", "那", "该", "本", "此", "其", "哪", "每", "各", "另",
    "整个", "所有", "全部", "部分", "一些", "各种", "多家", "几家", "几只",
    "其他", "其它", "别的", "上述", "以上", "国内", "海外", "两市", "主板",
)
# 裸后缀词本身（「医药」「银行」）也不是某个具体标的
_GENERIC_WORDS = frozenset(_NAME_SUFFIX)


def _clean(word: str) -> str:
    for noise in _LEAD_NOISE:
        if word.startswith(noise) and len(word) > len(noise):
            word = word[len(noise):]
            break
    word = word.lstrip(_LEAD_CHARS)
    return word


def is_generic_reference(word: str) -> bool:
    """这个词是不是**泛指**（"这家银行"/"整个医药"/"银行"），而不是点了一只具体的票。

    只用于"要不要把它当成用户点了名"的判定：泛指 → 交给上层按页面锚点/会话绑定处理；
    具体名（"北方稀土"/"某某科技"）→ 必须解析成唯一代码，解析不出来就 fail-closed。
    """
    w = _clean(str(word or "").strip())
    if not w:
        return True
    if w in _GENERIC_WORDS or w in _STOPWORDS:
        return True
    return w.startswith(_GENERIC_PREFIXES)


def is_stock_question(text: str) -> bool:
    """这句话是不是在问具体标的（决定是否强制先取数）。"""
    if not text:
        return False
    if _CODE_RE.search(text):
        return True
    for m in _NAME_RE.finditer(text):
        if _clean(m.group(1)) not in _STOPWORDS:
            return True
    return False


def mention_candidates(text: str) -> list[str]:
    """抽出待核对的名字（去重保序，最多 4 个）。"""
    out: list[str] = []
    for m in _NAME_RE.finditer(text or ""):
        word = _clean(m.group(1))
        if len(word) < 2 or word in _STOPWORDS or word in out:
            continue
        out.append(word)
        if len(out) >= 4:
            break
    return out


# 名称检索结果缓存: 路由/绑定阶段每轮都要把「北方稀土」解析成 600111,
# 不能每句都打一次 TQ(单线程 17709)。10 分钟过期, 失败不写缓存(下次重试)。
_TQ_MATCH_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}
_TQ_MATCH_TTL_S = 600.0


def _tq_match(word: str) -> list[tuple[str, str]]:
    """TQ 名称检索 → [(code, name)]；失败返回空（护栏是旁路，绝不抛）。"""
    try:
        try:
            from packages.marketdata.src.marketdata.vendors.tq import match_stkinfo
        except Exception:  # noqa: BLE001
            from marketdata.vendors.tq import match_stkinfo  # type: ignore
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = match_stkinfo(word) or []
    except Exception as exc:  # noqa: BLE001 - TQ 挂了不能影响主流程
        logger.info("match_stkinfo(%s) 失败: %s", word, exc)
        return []
    out = []
    for row in rows:
        code = str(row.get("Code") or row.get("code") or "")
        name = str(row.get("Name") or row.get("name") or "")
        if code and name:
            out.append((code, name))
    return out


def _match_cached(word: str) -> list[tuple[str, str]]:
    """带 10 分钟缓存的名称检索。

    为什么不在 `_tq_match` 里缓存：那是纯检索函数, 缓存放外层才既省 TQ 调用
    又能被单测替换掉网络层（patch `_tq_match` 时缓存仍然有效）。
    """
    import time as _time

    hit = _TQ_MATCH_CACHE.get(word)
    if hit and (_time.time() - hit[0]) < _TQ_MATCH_TTL_S:
        return hit[1]
    out = _tq_match(word)
    if out:
        _TQ_MATCH_CACHE[word] = (_time.time(), out)
    return out


def _suggest(word: str) -> tuple[str, str] | None:
    """查不到时给最像的一只（用首字扩大召回 + 字面相似度）。

    「强锐技术」直接查是空的，但查「强」能拿到 23 只，其中就有 301128 强瑞技术。
    相似度 < 0.6 一律不出建议 —— 宁可说查不到，也不把别的票塞给用户。
    """
    import difflib

    cands = _match_cached(word)
    if not cands and word:
        cands = _match_cached(word[0])
    best, best_score = None, 0.0
    for code, name in cands:
        if not name.endswith(("转债", "ETF", "指")):
            score = difflib.SequenceMatcher(None, word, name).ratio()
            if score > best_score:
                best, best_score = (code, name), score
    return best if best and best_score >= 0.6 else None


def resolve_mentions(text: str) -> dict:
    """名字 → {resolved: [(词,code,name)], suggestions: [(词,code,name)], unknown: [词]}。

    已给 6 位代码时直接返回空（不许自作聪明改标的）。
    """
    empty = {"resolved": [], "suggestions": [], "unknown": []}
    if not text or _CODE_RE.search(text):
        return empty
    for word in mention_candidates(text):
        hits = _match_cached(word)
        if hits:
            code, name = hits[0]
            empty["resolved"].append((word, code, name))
            continue
        sug = _suggest(word)
        if sug:
            empty["suggestions"].append((word, sug[0], sug[1]))
        else:
            empty["unknown"].append(word)
    return empty


def first_code_from_names(text: str) -> str | None:
    """问句里只写了股票名（「北方稀土还能拿吗」）→ 解析出第一只的 6 位代码。

    2026-10-22 实测事故：用户问「北方稀土…还能持有吗」，因为正文没有 6 位代码，
        `enter_request_ctx` 的 bound 为空 → 画像不判定 → 关键词也没命中配方 →
        模型拿到全量 64 个工具却一个都没调 → 触发零工具拒绝，答「没取到数据」。
        名字解析本来就是好的（resolve_mentions 命中 600111.SH），只是**没在这条路上用**。
    """
    if not text or _CODE_RE.search(text):
        return None
    try:
        info = resolve_mentions(text)
    except Exception:  # noqa: BLE001
        return None
    for _, code, _ in (info.get("resolved") or [])[:1]:
        return str(code).split(".")[0] or None
    return None


#: 整段名字切分：只在非汉字处断开（空格/标点/数字/字母）。
_SEGMENT_RE = re.compile(r"[^\u4e00-\u9fa5]+")


def bare_name_codes(text: str, limit: int = 2) -> list[tuple[str, str, str]]:
    """整段汉字（2~4 字、被分隔符隔开的）里能被行情库**直接命中**的名字。

    为什么需要（2026-11-17 三轮审计）：`mention_candidates` 只认「后缀词」
    （科技/医药/股份…），所以「贵州茅台 缠论怎么样」这类**没有后缀**的名字整句都
    检测不到，页面停在 600000 时就把 600000 的快照当成本轮结构答出去。

    边界（不猜）：只做**正向确认** —— 行情库查不到的整段一律不返回，绝不返回
    「可能是在说 XX」。因此这里增广的是"能确认的覆盖"，不是"猜一个标的"；查不到的
    泛问（「缠论结构怎么样」）仍然一个名字都不产生。

    已知覆盖边界（诚实说明）：紧贴问题词、又没有分隔符的长串（「贵州茅台缠论怎么样」）
    整段超过 4 字，仍然检测不到 —— 这类句子不会被误判成"解析失败"（不误杀），但也
    因此发现不了"名字与锚点不是同一只"的冲突。要覆盖这一种，得先有本地名字词典，
    而不是在这里按形状猜。
    """
    out: list[tuple[str, str, str]] = []
    if not text or _CODE_RE.search(text):
        return out
    attempts = 0
    for raw in _SEGMENT_RE.split(text):
        # 一次最多查 `limit` 段: TQ 是单线程(17709), 这是旁路, 不能因为一句长问题
        # 就打好几次检索(未命中也不进缓存, 每轮都会重复发生)。
        if attempts >= limit:
            break
        seg = _clean(raw.strip())
        if not (2 <= len(seg) <= 4) or seg in _STOPWORDS or is_generic_reference(seg):
            continue
        if any(seg == word for word, _c, _n in out):
            continue
        attempts += 1
        hits = _match_cached(seg)
        if not hits:
            continue
        code, name = hits[0]
        if not re.fullmatch(r"\d{6}", str(code or "")):
            continue
        out.append((seg, code, name))
        if len(out) >= limit:
            break
    return out


def correction_note(text: str) -> str | None:
    """把"名字对不上"的事实塞给模型，要求先确认再回答。"""
    info = resolve_mentions(text)
    lines: list[str] = []
    for word, code, name in info["resolved"]:
        if name != word and word not in name:
            lines.append(f"「{word}」= {code} {name}")
    for word, code, name in info["suggestions"]:
        lines.append(f"「{word}」行情库查不到，最接近的是 {code} {name}（**疑似**，需用户确认）")
    for word in info["unknown"]:
        lines.append(f"「{word}」行情库查不到，也没有相近标的")
    if not lines:
        return None
    return (
        "【标的核对（系统自动，务必遵守）】" + "；".join(lines) +
        "。回答前先用一句话跟用户确认标的，然后**只分析确认过的代码**；"
        "查不到的标的就直说查不到，禁止换成任何其他股票、禁止凭记忆给价格。"
    )


def zero_tool_refusal() -> str:
    """一次工具都没调就作答时的统一话术（不给结论，杜绝编数）。"""
    return (
        "这次**没有取到任何行情数据**（工具一次都没调用），所以我不给结论——凭记忆报价格和支撑位那是编的。\n\n"
        "请把标的写成代码再问一次（例：`301128 强瑞技术和 002050 三花智控今天怎么样`），"
        "或直接说「取数看看 XXX」。"
    )
