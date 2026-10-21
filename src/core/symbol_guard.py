"""标的与取数护栏（2026-10-21 线上幻觉事故后加）。

事故：用户问「全景数智深度诊断强锐技术和三花智控」，模型**一次工具都没调**，
直接编出「北方稀土 + 688013」两只完全无关的票，还给了现价与支撑位。
根因不是提示词不够温柔，而是"允许模型不取数就作答"这条缝。

本模块只做两件确定性的事（不猜、不兜底生成）：
1. `is_stock_question()`：判断这句话是不是在问具体标的（6 位代码或股票名后缀词）。
2. `resolve_names()`：把用户写的名字拿去 TQ `get_match_stkinfo` 核对；
   写错/简称不一致时返回可回填的更正说明（如 强锐技术 → 301128 强瑞技术）。
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


def _clean(word: str) -> str:
    for noise in _LEAD_NOISE:
        if word.startswith(noise) and len(word) > len(noise):
            word = word[len(noise):]
            break
    word = word.lstrip(_LEAD_CHARS)
    return word


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


def _suggest(word: str) -> tuple[str, str] | None:
    """查不到时给最像的一只（用首字扩大召回 + 字面相似度）。

    「强锐技术」直接查是空的，但查「强」能拿到 23 只，其中就有 301128 强瑞技术。
    相似度 < 0.6 一律不出建议 —— 宁可说查不到，也不把别的票塞给用户。
    """
    import difflib

    cands = _tq_match(word)
    if not cands and word:
        cands = _tq_match(word[0])
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
        hits = _tq_match(word)
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
