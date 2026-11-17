"""同一张图的「追问」只认**封闭语法**（2026-11-18 六轮审计重构）。

## 为什么把词表换掉

前几轮修法是往一张「纯追问词表」里补词（`_FOLLOWUP_VOCAB` + 单字白名单
`_FOLLOWUP_CHARS`），再用「剥完词表还剩什么」来推断「正文有没有点别的票」。这条路
**天然不可能证明不存在**：词表外的任何简称都会被当成噪音项吃掉，而且露出的破口是
组合性的——实测 `万科怎么样` 里「万」在单字表里被当虚词吞掉、「科」只剩一个字被丢弃，
于是整句被判成"纯追问"，页面停在 600519 时就把贵州茅台的结构答给了万科；`万科A怎么样`
则因为本地全名索引只装 `万科A` 而"没认出名字"。再补一个别名只能把已知的那只补上，
下一只新的简称照样漏。**多轮补丁已证明这条路不可收敛。**

## 现在的协议（正向、有限、可完整匹配）

带图表快照的轮次里，正文只有三种形状可以用这张图（其余一律拒绝同图）：

1. **泛问模板**（不点任何标的，只指当前这张图）::

       句子 ::= [客套] [时间] [泛指主语] 核心 [语气]
       泛指主语 ::= 「泛指指代 + (量词) + 类别词」的**封闭形状**（`is_generic_reference`，
                    不超过 6 字），或 `_PRONOUN_SUBJECTS` 里那几条明确的当前指代；
                    可带一个「的」；**与任一已验证全名同字的一律不算主语**
       核心 ::= `_CORES` 里的**整串字面量**（缠论结构/分型笔/买点/操作类追问）
       客套 ::= `_POLITE` 里的整串字面量    时间 ::= `_TIME` 里的整串字面量
       语气 ::= `_TAIL_CHARS` 里的**单个**字

   另有一条撞名保护：整句若**恰好等于**某条已验证全名，即便它长得像模板也不按泛问算。

2. **唯一代码 / @提及 + 同一条追问语法**：正文里的 6 位代码必须只有一个；把它剥掉之后，
   剩下的部分仍必须**整串**满足上面那条语法（可以为空）。

3. **已验证全名 + 同一条追问语法**：名字必须**写在句首**、必须是本地可信名单里
   **一字不差的全名**、且该名字只对应**唯一一个**代码；剥掉名字（可带一个「的」）之后
   剩下的部分仍必须整串满足上面这条语法。

判定只有两种结果：**整串匹配**（可用同图）或**不匹配**（不可用同图）。这里没有
"按字符集剥词"、"按相似度猜"、"按首字认主"这类步骤 —— 因此它可审计、可复现，
也不存在「词表外简称被吞掉」的破口。

## 这份协议能证明什么、不能证明什么

- **能证明**：符合上述字面量的句子，结构上不含除当前标的/本句唯一标的以外的成分。
- **不能证明**：自然语言"绝无歧义"。协议之外的说法（简称、别名、句中名字、口语化
  长句、多标的、错别字）一律**判不匹配**，即**允许误拒、不许误答**。名字这一维的
  识别范围只有「句首 + 已验证全名 + 唯一命中」；名单不可用就干脆不支持名字形式。
  判定过程**零网络**（不为归属打 TQ 检索），代价是名单冷缓存那一轮不支持名字形式。
- 拒绝时调用方必须明确告知"本轮不可读同图"，并给出改用 6 位代码/@提及 + 简短问题
  的回退；**普通不带快照的聊天不受本协议约束**（本模块只在快照归属判定里被调用）。
  该说明由调用方注入 system（与既有 note 机制一致），服务端不生成替代正文。

## 与旧词表的取舍

`_POLITE` / `_TIME` / `_CORES` 里的每一条都是**整串字面量**，只能整句精确匹配，
不做任意拼接猜测；加词只影响"能不能用同图"的覆盖，不会引入"把别的票当成当前票"的
风险（名字不可能等于一条追问核心）。`tests/test_chan_followup_protocol.py` 里有一条
钉子：这些字面量里不许出现任何已验证证券全名。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "SnapshotText",
    "normalize",
    "parse",
    "GENERIC_TEMPLATE_EXAMPLES",
    "CORES",
    "POLITE",
    "TIME_WORDS",
]

# ── 归一化：只有一套，且是**封闭**的 ────────────────────────────────────────
# NFKC 负责全角→半角（数字/问号/字母），下面那张标点表负责去掉句读。除此之外
# 不改动任何字符：不删汉字、不删字母、不做同义词替换。
_PUNCT = frozenset(
    "？?！!。．.,，、；;：:～~…·\"'“”‘’（）()【】[]《》<>〈〉「」『』〔〕/\\-_—"
)
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """严格归一：NFKC + 去空白 + 去掉封闭标点表 + ASCII 小写。其余字符原样保留。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = "".join(ch for ch in s if ch not in _PUNCT)
    s = _WS_RE.sub("", s)
    return s.lower()


# ── 封闭词表（全部是**整串字面量**，只做整句精确匹配）──────────────────────

#: 客套前缀
POLITE = (
    "", "请问", "请问下", "请问一下", "麻烦问下", "麻烦问一下",
    "帮我看看", "帮我看一下", "帮我看下", "帮忙看看", "帮忙看下",
    "看一下", "看看", "我想看看", "我想看下", "想看看",
)

#: 时间前缀（只说"什么时候看"，不指向任何标的）
TIME_WORDS = ("", "现在", "目前", "当前", "今天", "如今", "眼下")

#: 只作用于「眼前这张图」的指代（`is_generic_reference` 之外的一字/二字指代）
_PRONOUN_SUBJECTS = frozenset({
    "它", "这只", "那只", "这个", "那个",
    # 「图」类指代：`is_generic_reference` 的"量词+类别词"表里没有"张"，这里显式列出
    # （只放整串字面量，不做"张/幅/根 + 图"的任意拼接）。
    "这张图", "那张图", "这幅图", "那张图", "这图", "该图", "本图", "此图",
    "这张图表", "那张图表", "这个图", "那个图",
})

#: 语气词（**单个**字，只允许出现在整句末尾）
_TAIL_CHARS = frozenset("呢啊吧呀哈哦嘛")

#: 追问核心：缠论结构观测 + 常见的"眼前这只"研判/操作追问。
#: 只许加**不可能构成证券名**的整串短语；不许放单个类别词（"银行"/"医药"）。
CORES = (
    # 缠论结构（本工具的直接对象）
    "缠论结构怎么样", "缠论结构怎么看", "缠论结构如何", "缠论结构如何看",
    "缠论结构分析", "缠论结构咋样", "缠论结构",
    "缠论怎么样", "缠论怎么看", "缠论如何", "缠论分析", "缠论",
    "结构怎么样", "结构怎么看", "结构如何", "结构分析", "结构",
    "分型怎么样", "分型怎么看", "分型与笔怎么样", "分型和笔怎么样",
    "笔怎么样", "笔怎么看", "笔和线段怎么样", "线段怎么样", "中枢怎么样",
    "背驰了吗", "有背驰吗", "是什么级别", "什么级别", "级别怎么样",
    "三买点确认了吗", "三买点还有效吗", "三卖点确认了吗", "有买点吗", "买点在哪", "卖点在哪",
    # 泛指研判 / 操作
    "怎么样", "怎么看", "如何", "咋样", "分析一下", "分析", "点评一下", "点评",
    "后面怎么走", "后面怎么样", "后面怎么看", "后面怎么操作", "后面如何",
    "接下来怎么走", "后续怎么走", "怎么操作", "如何操作", "操作建议", "操作上怎么处理",
    "能买吗", "可以买吗", "能上车", "能上车吗", "要不要减仓", "要不要加仓",
    "该不该买", "该不该卖", "风险大吗", "风险怎么样",
    "支撑位在哪", "压力位在哪", "止损位在哪",
    "趋势怎么样", "趋势如何", "走势怎么样", "走势如何", "强弱怎么样",
)

_CORE_SET = frozenset(CORES)

#: 文档/测试用的"合格泛问"示例（必须是协议内、且不含任何标的）
GENERIC_TEMPLATE_EXAMPLES = (
    "缠论结构怎么样",
    "这张图的缠论结构怎么样",
    "这只股票的缠论结构怎么样",
    "这只票的缠论结构怎么样",
    "600519 缠论结构怎么样",
)

#: 名字最长取到几个字：A 股全名以 2~4 字为主，留到 6 字只为兼容少数长名。
_MAX_NAME_LEN = 6
#: 泛指主语的长度上限（代词 + 量词 + 类别词 + 的）
_MAX_SUBJECT_LEN = 6

#: 合法 A 股代码前缀（与 `symbol_guard._CODE_RE` 同口径：00/30/60/68 + 北交所 83/87/43）
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_VALID_CODE_RE = re.compile(r"^(?:00|30|60|68|83|87|43)\d{4}$")


@dataclass(frozen=True)
class SnapshotText:
    """正文对"本轮同图"的判定结果。

    ``kind``:
      * ``"generic"`` —— 整句只指眼前这张图（不点标的）；
      * ``"code"``    —— 唯一 6 位代码（或 @提及）+ 合规追问；
      * ``"name"``    —— 已验证全名（唯一命中）+ 合规追问；
      * ``"reject"``  —— 无法按本协议证明唯一目标；``problem`` 给出原因码：
        ``"ambiguous"``（正向确认到多只不同标的）、``"name_conflict"``（句首名字指向
        另一只）、``"unresolved_name"``（有带后缀的名字候选却解析不出）、
        ``"not_in_protocol"``（其余：简称/别名/句中名字/长句/口语）。
    """

    kind: str
    symbol: str | None = None
    name: str | None = None
    problem: str | None = None
    #: 正向确认到的名字（仅用于拒绝文案；空 = 没确认到，不代表"没有别的票"）
    others: tuple[tuple[str, str], ...] = ()


# ── 语法判定（全部是整串比较，没有字符级剥离）────────────────────────────────


def _is_core(s: str) -> bool:
    """整串是否是一条追问核心（允许末尾一个语气词）。"""
    if not s:
        return False
    if s in _CORE_SET:
        return True
    return len(s) > 1 and s[-1] in _TAIL_CHARS and s[:-1] in _CORE_SET


def _is_subject(s: str, names: dict | None) -> bool:
    """整串是否是一个"只指眼前这张图"的主语。"""
    if not s:
        return False
    if names and s in names:                # 与已验证全名撞字 → 它是名字，不是泛指
        return False
    if s in _PRONOUN_SUBJECTS:
        return True
    try:
        from src.core.symbol_guard import is_generic_reference
    except Exception:  # noqa: BLE001 - 护栏缺失时按不匹配处理（方向安全）
        return False
    return bool(is_generic_reference(s))


def _match_qpart(s: str, names: dict | None) -> bool:
    """`s` 是否**整串**匹配「[客套][时间][泛指主语]核心」。"""
    if not s:
        return False
    for polite in POLITE:
        if polite and not s.startswith(polite):
            continue
        rest = s[len(polite):]
        for tm in TIME_WORDS:
            if tm and not rest.startswith(tm):
                continue
            body = rest[len(tm):]
            if _is_core(body):
                return True
            for i in range(1, min(len(body), _MAX_SUBJECT_LEN) + 1):
                subj, core = body[:i], body[i:]
                if not core or not _is_core(core):
                    continue
                head = subj[:-1] if subj.endswith("的") else subj
                if _is_subject(head, names):
                    return True
    return False


# ── 正向命中器（只用来"确认存在"，绝不用"没命中"推"不存在"）─────────────────


def _name_index() -> dict | None:
    """本地可信全名索引：``{全名: [(code, 全名), ...]}``；``None`` = 不可用。"""
    try:
        from src.core.symbol_guard import local_name_index

        raw = local_name_index()
    except Exception:  # noqa: BLE001 - 索引不可用 = 不支持名字形式（不是"没有名字"）
        return None
    if not raw:
        return None
    # 归一化后的视图（NFKC + 小写），与 `normalize()` 的口径一致 —— 否则「万科A」
    # 会在正文里变成「万科a」而永远匹配不上。按 raw 的身份缓存，名单刷新即失效。
    cached = _NAME_VIEW
    if cached["raw"] is raw and cached["view"] is not None:
        return cached["view"]
    view: dict[str, list[tuple[str, str]]] = {}
    for name, hits in raw.items():
        view.setdefault(normalize(name), []).extend(hits)
    _NAME_VIEW.update(raw=raw, view=view)
    return view


#: 归一化名字索引的缓存（只按 raw 对象身份失效，避免每轮重建成千条键）
_NAME_VIEW: dict = {"raw": None, "view": None}


def _codes_of(hits) -> set[str]:
    return {str(code).split(".")[0] for code, _n in hits or [] if str(code or "").strip()}


def _verified_names(s: str, names: dict | None) -> list[tuple[str, str]]:
    """整段里能**正向确认**的全名（最长优先、互不重叠）；只用于拒绝文案。"""
    if not names or not s:
        return []
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(s):
        hit = None
        for size in range(min(_MAX_NAME_LEN, len(s) - i), 1, -1):
            cand = s[i:i + size]
            hits = names.get(cand)
            if hits and len(_codes_of(hits)) == 1:
                hit = (cand, next(iter(_codes_of(hits))), size)
                break
        if hit is None:
            i += 1
            continue
        cand, code, size = hit
        if (cand, code) not in out:
            out.append((cand, code))
        i += size
    return out


def _name_prefix(s: str, names: dict | None) -> tuple[str, str | None] | None:
    """句首是否为**一字不差的已验证全名** → (名字, 唯一代码或 None=不唯一)。"""
    if not names or not s:
        return None
    for size in range(min(_MAX_NAME_LEN, len(s)), 1, -1):
        cand = s[:size]
        hits = names.get(cand)
        if not hits:
            continue
        codes = _codes_of(hits)
        return cand, (next(iter(codes)) if len(codes) == 1 else None)
    return None


def _unverified_name_words(s: str, names: dict | None) -> list[str]:
    """正文里"带股票名后缀、但不在已验证全名索引里"的词 —— **只用于拒绝原因**。

    纯正则（零网络）：命中只说明"这里像被人写了个名字而服务端核不出来"，不是标的证据，
    也不影响判定（判定已经是"拒绝"）。已验证全名会被排除，免得把"另一个全名"误标成
    "解析不出"。
    """
    try:
        from src.core.symbol_guard import mention_candidates

        words = mention_candidates(s)
    except Exception:  # noqa: BLE001
        return []
    return [w for w in words if not (names and normalize(w) in names)]


def _reject(s: str, names: dict | None, *, primary: tuple[str, str] | None = None
            ) -> SnapshotText:
    """不匹配 → 给原因码（原因只影响文案，判定已经是"拒绝"）。"""
    found = list(_verified_names(s, names))
    if primary and primary[0] and primary[1] and not any(n == primary[0] for n, _c in found):
        found.insert(0, (primary[0], primary[1]))
    if primary and primary[1] is None:
        # 句首名字存在但不是唯一命中（同名多码）—— 只能算"解析不出唯一代码"
        return SnapshotText("reject", name=primary[0], problem="unresolved_name",
                            others=tuple(found))
    if len({c for _n, c in found}) > 1:
        # 正向确认到不止一只票
        return SnapshotText("reject", problem="ambiguous", others=tuple(found))
    if _unverified_name_words(s, names):
        # 写了像名字的东西却核不出来 —— 只能算"解析不出唯一代码"
        return SnapshotText("reject", problem="unresolved_name", others=tuple(found))
    if len(found) == 1:
        name, code = found[0]
        if primary and primary[1] and code == primary[1]:
            return SnapshotText("reject", name=name, symbol=code, problem="not_in_protocol",
                                others=tuple(found))
        return SnapshotText("reject", name=name, symbol=code, problem="name_conflict",
                            others=tuple(found))
    return SnapshotText("reject", problem="not_in_protocol")


def parse(text: str) -> SnapshotText:
    """正文 → 本轮同图判定（唯一入口）。见模块头部的协议定义。"""
    s = normalize(text)
    if not s:
        return SnapshotText("generic")
    names = _name_index()

    codes: list[str] = []
    for m in _CODE_RE.finditer(s):
        code = m.group(1)
        if code not in codes:
            codes.append(code)
    if len(codes) > 1:
        return SnapshotText("reject", problem="ambiguous")
    if codes:
        code = codes[0]
        if not _VALID_CODE_RE.match(code):
            return SnapshotText("reject", problem="not_in_protocol")
        residual = _CODE_RE.sub("", s)
        if not residual or _match_qpart(residual, names):
            return SnapshotText("code", symbol=code)
        verdict = _reject(residual, names)
        if verdict.problem == "name_conflict" and verdict.symbol == code:
            # 残句里的全名**就是**这个代码（「600000 浦发银行」）：不是"另一只票"，
            # 只是没构成合规追问 —— 别把文案说成"指向另一只"。
            return SnapshotText("reject", symbol=code, name=verdict.name,
                                problem="not_in_protocol", others=verdict.others)
        return verdict

    # 泛问模板 + 整句本身**不是**一条已验证全名 才算"只指眼前这张图"。
    # 只判模板形状是不够的：理论上某个证券全名可能长得就像「泛指主语 + 核心」的
    # 组合（例如名字就叫「这只股票」），此时模板也会匹配，所以这里再正向排除一次。
    # 只看"整句 == 某个全名"（不做子串扫描）：既不放过这种撞名，也不会把正常模板误杀。
    if _match_qpart(s, names) and s not in (names or {}):
        return SnapshotText("generic")

    pref = _name_prefix(s, names)
    if pref is not None:
        name, code = pref
        if code is None:
            return SnapshotText("reject", name=name, problem="unresolved_name")
        residual = s[len(name):]
        if residual.startswith("的"):
            residual = residual[1:]
        if not residual or _match_qpart(residual, names):
            return SnapshotText("name", symbol=code, name=name)
        return _reject(residual, names, primary=(name, code))

    return _reject(s, names)
