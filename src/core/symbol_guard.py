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

2026-11-17 四轮审计再补一条：名字**紧贴提问词**（「贵州茅台缠论怎么样」，中间没有
空格/标点）时按分隔符切段必然漏检，于是页面停在 600000 就把浦发的快照答成茅台。
现在用**本地全市场名单**（东财/通达信列表缓存，非快照自述）建 name→code 反向索引，
在整段汉字上做窗口确认（`glued_name_codes`）；本地索引不可用时退回有界的 TQ 段首
探测，两边都不可用则**明确 fail-closed**（`whole_text_name_codes` 的 coverage="none"）
——「找不到」绝不等于「用户没提别票」。

**覆盖边界（2026-11-17 五轮审计，必须按这个口径使用本模块）**：上面两条路都只覆盖
**一部分写法** —— 本地名单只有 4/3 字全名（外加句首两字简称），TQ 兜底只探每段开头
3~4 字，所以
  * 句中简称（「我想看看贵州茅台缠论怎么样」里的名字在段中）、
  * 本地名单过期/只装了一部分（例如只有别名、只有老票）时的全名、
  * 新上市/改名后的名称
都可能**漏检**。因此 `coverage`（"local"/"tq"/"none"）**只说明结论是哪个通道给的，
不是"全集已确认"**；任何调用方都不许把「hits 为空」当成「正文没点名」——
唯一能当作**正向**证明的，是 `unverified_name_words()` 返回空（整句都由泛指/追问
用语构成，见 `_FOLLOWUP_VOCAB`）。
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

# 「泛指当前这只」的判定：抓到类别词，但说话人指的是**眼前这张图**，不是在点名。
# 为什么必须分出来（2026-11-17 三轮审计）：快照归属把"名字解析失败"当 fail-closed，
# 如果连「这家银行」「整个医药」也算"点了名却解析失败"，用户对着浦发银行图表问
# 「这家银行后面怎么走」就会被整份丢弃 —— 那是把泛指误杀成歧义。
#
# 2026-11-17 四轮审计修：过去只看**首字**（本/其/各/另/…），于是真名「本钢板材」被
# 判成泛指 → 用户问本钢板材、页面停在 600000 时，浦发的快照被当成本轮那张图答出去。
# 现在必须是「指代 + (量词) + **类别词**」的整体形态才算泛指：「本钢板材」的
# 「钢板材」不是类别词，所以它是真名。
_GENERIC_HEADS = ("这", "那", "该", "本", "此", "其", "哪", "每", "各", "另")
_GENERIC_MEASURES = ("", "只", "支", "家", "个", "些", "类", "种")
# 「整个/其他/所有…」这类本身已含指代义的短语（只认整句开头）
_GENERIC_PHRASES = (
    "整个", "所有", "全部", "部分", "一些", "各种", "多家", "几家", "几只",
    "其他", "其它", "别的", "上述", "以上", "国内", "海外", "两市", "主板",
)
# 类别词：指代 + (量词) + 类别词 = 泛指（「这家银行」「这只股票」「该板块」）
_GENERIC_NOUNS = frozenset(_NAME_SUFFIX) | {
    "股票", "股", "票", "个股", "标的", "品种", "公司", "板块", "行业", "指数",
    "市场", "大盘", "品", "块",
}
# 裸类别词本身（「医药」「银行」）也不是某个具体标的
_GENERIC_WORDS = frozenset(_NAME_SUFFIX)


def _clean(word: str) -> str:
    for noise in _LEAD_NOISE:
        if word.startswith(noise) and len(word) > len(noise):
            word = word[len(noise):]
            break
    word = word.lstrip(_LEAD_CHARS)
    return word


def is_generic_reference(word: str) -> bool:
    """这个词是不是**泛指**（"这家银行"/"整个医药"/"这只股票"），而不是点了具体的票。

    只用于"要不要把它当成用户点了名"的判定：泛指 → 交给上层按页面锚点/会话绑定处理；
    具体名（"北方稀土"/"本钢板材"/"某某科技"）→ 必须解析成唯一代码，解析不出来就 fail-closed。

    口径（2026-11-17 四轮审计）：**不许用首字判**。「本钢板材」「北方稀土」「中国平安」
    都是真名；只有「指代 + (量词) + 类别词」的完整形态（本股票/该板块/这家银行）与
    明确泛指短语（整个医药/其他银行/所有股票）才算泛指。
    """
    w = _clean(str(word or "").strip())
    if not w:
        return True
    if w in _GENERIC_WORDS or w in _STOPWORDS:
        return True
    if w.startswith(_GENERIC_PHRASES):
        return True
    for head in _GENERIC_HEADS:
        if not w.startswith(head):
            continue
        rest = w[len(head):]
        for num in ("", "一", "两", "几"):
            if num and not rest.startswith(num):
                continue
            body = rest[len(num):]
            if body in _GENERIC_NOUNS:
                return True
            for measure in _GENERIC_MEASURES:
                if not measure or not body.startswith(measure):
                    continue
                tail = body[len(measure):]
                # 「另一只」「这几只」「这些都」：只有量词、没有类别词，也仍是泛指
                if tail in _GENERIC_NOUNS or tail in ("", "一", "两", "几", "些") or len(tail) <= 1:
                    return True
    return False


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


def _tq_lookup(word: str) -> list[tuple[str, str]] | None:
    """TQ 名称检索 → [(code, name)]。

    **None = 查不了**（导入失败 / 调用异常），``[]`` = 查得到但没有这个名字。
    这个区分是 2026-11-17 四轮审计的要求：把"通道挂了"当成"没有这只票"，就会把
    「找不到」误当成「用户没提别票」，然后信任页面上的旧快照。
    """
    try:
        try:
            from packages.marketdata.src.marketdata.vendors.tq import match_stkinfo
        except Exception:  # noqa: BLE001
            from marketdata.vendors.tq import match_stkinfo  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        rows = match_stkinfo(word) or []
    except Exception as exc:  # noqa: BLE001 - TQ 挂了不能影响主流程
        logger.info("match_stkinfo(%s) 失败: %s", word, exc)
        return None
    out = []
    for row in rows:
        code = str(row.get("Code") or row.get("code") or "")
        name = str(row.get("Name") or row.get("name") or "")
        if code and name:
            out.append((code, name))
    return out


def _tq_match(word: str) -> list[tuple[str, str]]:
    """TQ 名称检索 → [(code, name)]；失败返回空（护栏是旁路，绝不抛）。"""
    return _tq_lookup(word) or []


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


# ── 本地全名索引（名字 → 代码）────────────────────────────────────────────
# 2026-11-17 四轮审计: 「贵州茅台缠论怎么样」中间**没有分隔符**, 按非汉字切段会拿到一整段
# 10 字, 旧实现直接跳过 → 页面停在 600000 时仍信任浦发快照, 把浦发的结构答成茅台。
#
# 为什么不能靠 TQ 补: 一句长问题按 2~4 字窗口去问 TQ 是几十次网络(单线程 17709),
# 每条消息都这样打不可接受。所以复用全仓已有的**本地全市场名单**(东财/通达信列表,
# 落 `data/stock_list_cache.json`, 7 天 TTL) 建一份 name→code 反向索引: 整段窗口扫描
# 是 O(字数) 次字典查找, 零网络。
#
# 口径: 只读本地, **绝不联网**; ``None`` 表示"索引不可用", 不是"没有这个名字" ——
# 调用方必须区别对待(见 `whole_text_name_codes` 的 coverage)。

_LOCAL_INDEX_TTL_S = 600.0
_LOCAL_INDEX_FAIL_TTL_S = 60.0
_LOCAL_INDEX: dict = {"stamp": 0.0, "ok": False, "names": None, "aliases": None}

#: 整段窗口确认的长度: 先 4 字再 3 字（A 股全名以 4 字为主, 2 字全名极少且歧义大）
_GLUED_NAME_LENS = (4, 3)


def _local_name_pairs() -> list[tuple[str, str]] | None:
    """(6 位码, 全名) 列表; ``None`` = 本地索引不可用（不联网、不猜、不编）。"""
    rows = None
    try:
        from src.collectors.stock_list import cached_stock_list

        rows = cached_stock_list()          # 只读磁盘缓存, 过期返回 None
    except Exception:  # noqa: BLE001 - 取名单是旁路, 失败等于不可用
        logger.info("本地全市场名单读取失败(按不可用处理)", exc_info=True)
        rows = None
    pairs: list[tuple[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("symbol") or "").strip()
        name = str(row.get("name") or "").strip()
        mkt = str(row.get("market") or "CN").strip().upper() or "CN"
        if mkt != "CN" or not name or not re.fullmatch(r"\d{6}", code):
            continue
        pairs.append((code, name))
    if pairs:
        return pairs
    try:
        from src.core.stock_names import cached_pairs

        got = cached_pairs()
    except Exception:  # noqa: BLE001
        got = None
    return got or None


def _build_alias_index(names: dict[str, list[tuple[str, str]]]) -> dict[str, list[tuple[str, str]]]:
    """4 字全名的 2 字尾 → 那只票（「贵州茅台」→「茅台」）。

    只用于**句首**简称（「茅台缠论怎么样」）；句中出现「平安」「时代」这类词太容易
    误伤普通表达，宁可漏检也不猜。
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for name, hits in names.items():
        if len(name) != 4:
            continue
        tail = name[-2:]
        if tail in _STOPWORDS or is_generic_reference(tail):
            continue
        bucket = out.setdefault(tail, [])
        for code, full in hits:
            if (code, full) not in bucket:
                bucket.append((code, full))
    return out


def local_name_index() -> dict[str, list[tuple[str, str]]] | None:
    """名字 → [(code, name)]；``None`` = 索引不可用（**不能**当成"没有这个名字"）。"""
    import time as _time

    now = _time.time()
    ttl = _LOCAL_INDEX_TTL_S if _LOCAL_INDEX["ok"] else _LOCAL_INDEX_FAIL_TTL_S
    if _LOCAL_INDEX["stamp"] and (now - _LOCAL_INDEX["stamp"]) < ttl:
        return _LOCAL_INDEX["names"]
    pairs = _local_name_pairs()
    names: dict[str, list[tuple[str, str]]] | None = None
    aliases: dict[str, list[tuple[str, str]]] | None = None
    if pairs:
        built: dict[str, list[tuple[str, str]]] = {}
        for code, name in pairs:
            bucket = built.setdefault(name, [])
            if (code, name) not in bucket:
                bucket.append((code, name))
        names = built or None
        aliases = _build_alias_index(names) if names else None
    _LOCAL_INDEX.update(stamp=now, ok=bool(names), names=names, aliases=aliases)
    return names


def reset_local_index() -> None:
    """清空本地名称索引缓存（测试/名单更新后强制重读）。"""
    _LOCAL_INDEX.update(stamp=0.0, ok=False, names=None, aliases=None)


def _scan_local_names(seg: str, names: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str, str]]:
    """整段汉字的**全偏移、最长优先**全名命中；只认索引里真实存在的名字。"""
    out: list[tuple[str, str, str]] = []
    i, n = 0, len(seg)
    while i < n:
        hit = None
        for size in _GLUED_NAME_LENS:
            cand = seg[i:i + size]
            if len(cand) != size:
                continue
            hits = names.get(cand)
            if not hits or cand in _STOPWORDS or is_generic_reference(cand):
                continue
            hit = (cand, hits, size)
            break
        if hit is None:
            i += 1
            continue
        word, hits, size = hit
        out.extend((word, code, name) for code, name in hits)
        i += size
    return out


def glued_name_codes(text: str, limit: int = 2) -> list[tuple[str, str, str]]:
    """无分隔符整句里能被**本地全名索引**确认的票：[(用户写的词, code, name)]。

    贴紧提问词也认得出（「贵州茅台缠论怎么样」→ 贵州茅台），因为不再依赖分隔符，
    而是在整段汉字上做窗口确认。本地索引不可用时返回 []（**空 ≠ 没点名**，
    覆盖状态见 `whole_text_name_codes`）。
    """
    out: list[tuple[str, str, str]] = []
    if not text or _CODE_RE.search(text):
        return out
    names = local_name_index()
    if not names:
        return out
    aliases = _LOCAL_INDEX.get("aliases") or {}
    for raw in _SEGMENT_RE.split(text):
        seg = _clean(raw.strip())
        if len(seg) < 2 or seg in _STOPWORDS:
            continue
        found = _scan_local_names(seg, names)
        if not found and len(seg) >= 3:
            head = seg[:2]
            hits = aliases.get(head) or []
            if len(hits) == 1 and not is_generic_reference(head):
                code, name = hits[0]
                found = [(head, code, name)]
        for word, code, name in found:
            if any(word == w for w, _c, _n in out):
                continue
            out.append((word, code, name))
            if len(out) >= limit:
                return out
    return out


def _tq_prefix_probe(text: str, limit: int = 2) -> list[tuple[str, str, str]] | None:
    """本地索引不可用时的**有界**兜底：只把长汉字段的头 4/3 字拿去 TQ 精确核对。

    返回 None = TQ 也查不了 ⇒ 调用方必须按"无法确认正文没点别的票"保守处理；
    ``[]`` = 问到了，这些段首位置上没有全名 —— **只是"段首没有"，不是"正文没点名"**。

    为什么只探段首: 全偏移探测会变成几十次网络调用; 段首能覆盖「贵州茅台缠论怎么样」
    这类最常见写法。**段中名字照漏**（「我想看看贵州茅台缠论怎么样」），2026-11-17
    五轮审计的误答就出在这条：把 `[]`（外加 coverage="tq"）读成"没点别的票"，于是
    页面停在 600000 时信任了浦发的快照。所以本函数的空结果**永远不许**被当作
    "正文没有其他标的"的证据（见 `whole_text_name_codes` 的 coverage 口径）。
    """
    out: list[tuple[str, str, str]] = []
    calls = 0
    for raw in _SEGMENT_RE.split(text or ""):
        seg = _clean(raw.strip())
        if len(seg) < 4 or seg in _STOPWORDS:
            continue
        for size in _GLUED_NAME_LENS:
            cand = seg[:size]
            if len(cand) != size or cand in _STOPWORDS or is_generic_reference(cand):
                continue
            if calls >= limit:
                return out
            calls += 1
            hits = _tq_lookup(cand)
            if hits is None:
                return None
            if any(name == cand for _c, name in hits):
                code, name = next((c, n) for c, n in hits if n == cand)
                out.append((cand, code, name))
                break
    return out


# 「纯追问」词汇表：只在**本地索引与 TQ 都不可用**时用来回答"这句里还有没有我核对
# 不了的词"。不是用来猜名字，而是区分「整句都是追问用语」和「有无法核对的新词」。
_FOLLOWUP_VOCAB = tuple(sorted({
    "缠论", "结构", "分型", "笔", "线段", "中枢", "走势", "趋势", "背驰", "买卖点",
    "三买", "三卖", "一买", "一卖", "二买", "二卖", "级别", "日线", "周线", "月线",
    "季线", "年线", "分钟", "均线", "成交量", "量能", "换手", "支撑", "压力", "阻力",
    "止损", "止盈", "仓位", "加仓", "减仓", "持仓", "成本", "风险", "行情", "大盘",
    "板块", "行业", "指数", "市场", "个股", "股票", "标的", "品种", "这只", "那只",
    "该股", "这家", "那家", "这个", "那个", "这些", "那些", "现在", "目前",
    "请问", "帮我", "帮忙", "麻烦", "谢谢", "看看", "看一下", "看下", "说说", "讲讲",
    "分析", "诊断", "点评", "复盘", "预测", "判断", "怎么样", "怎么", "如何", "什么",
    "多少", "为什么", "能否", "可以", "是否", "还有", "还能", "后面", "接下来",
    "今天", "明天", "后天", "最近", "近期", "这周", "下周", "继续", "详细", "简单",
    "解释", "讲解", "一下", "一些", "哪个", "哪些", "两个", "三个", "多空", "方向",
    "企业", "公司", "仓位", "买点", "卖点", "价格", "股价", "开盘", "收盘", "新高",
    "新低", "反弹", "回调", "拉升", "跳水", "涨停", "跌停", "放量", "缩量", "主力",
    "资金", "北向", "业绩", "估值", "消息", "利好", "利空", "事件", "题材", "龙头",
    "券商", "银行", "医药", "科技", "白酒", "半导体", "新能源", "军工", "地产", "煤炭",
    "有色", "钢铁", "化工", "汽车", "机械", "传媒", "农业", "电力", "港口", "银行股",
    # 问句脚手架 / 操作与观察类用语：**不是**标的线索，加它们的唯一作用是别把
    # 「这只票后面怎么操作」「三买点确认了吗」这类纯追问误判成"有认不出的名字"。
    # 只许往这里加**不可能出现在股票名里**的词（「平安」「时代」这种一律不许加）。
    "操作", "确认", "建议", "看法", "情况", "表现", "空间", "幅度", "区间", "变化",
    "介入", "处理", "观察", "应对", "复盘", "预期", "节奏", "强弱", "方向",
    "突破", "跌破", "冲高", "回落", "震荡", "横盘", "整理", "加速", "反抽", "补涨",
    "上车", "下车", "进场", "出场", "建仓", "补仓", "清仓", "买入", "卖出", "观望",
    "追高", "抄底", "做多", "做空", "空仓", "满仓", "半仓", "止损位", "支撑位",
    # 研判用语（与 context._JUDGMENT_WORDS 同源）：这些词出现在正文里是"在问判断"，
    # 不是"点了另一只票"。同样只放不可能构成股票名的词。
    "洗盘", "出货", "吸筹", "派发", "见顶", "见底", "割肉", "恐慌", "企稳",
    "反转", "缺口", "量价", "阴跌", "逼空", "诱多", "诱空", "补跌", "上移", "下移",
}, key=len, reverse=True))

#: 单字（虚词/语气词/数字）：算不上"一个具体的词"，出现在残留里不算可疑
_FOLLOWUP_CHARS = frozenset(
    "的了着过吗呢吧啊呀哦嗯和与及或对在把是有没不会要就都也还很最再又才"
    "我你他她它我们你们他们这那哪个些只支家种类多几"
    "一二三四五六七八九十两半0123456789亿万元年月日点位倍成"
)


def _unexplained_words(text: str) -> list[str]:
    """正文里"说不清"的汉字块（长度 ≥ 2）；纯追问用语一律不算。

    之所以要这个：本地索引与 TQ 都查不了时，**没有**任何办法证明「贵州茅台」不是标的，
    所以不能把"没检测到名字"当成"用户没点别的票"。此时只要句子里有核对不了的词，
    调用方就必须 fail-closed（丢快照），而不是拿页面上的旧图硬答。
    """
    out: list[str] = []
    for raw in _SEGMENT_RE.split(text or ""):
        seg = _clean(raw.strip())
        if len(seg) < 2:
            continue
        buf = ""
        i = 0
        while i < len(seg):
            tok = next((t for t in _FOLLOWUP_VOCAB if seg.startswith(t, i)), None)
            if tok:
                if len(buf) >= 2:
                    out.append(buf)
                buf = ""
                i += len(tok)
                continue
            ch = seg[i]
            if ch in _FOLLOWUP_CHARS:
                if len(buf) >= 2:
                    out.append(buf)
                buf = ""
            else:
                buf += ch
            i += 1
        if len(buf) >= 2:
            out.append(buf)
    return out


def unverified_name_words(text: str, limit: int = 4) -> list[str]:
    """正文里**没法核对**的汉字块 —— 本模块唯一能当"正文没点别的票"证据的东西。

    用法（2026-11-17 五轮审计）：名称识别（本地名单 / TQ 兜底）都只覆盖**一部分写法**，
    所以「一次都没确认出名字」既可能是"用户没点名"，也可能是"名字漏检了"。把这两种
    情形分开的**唯一**办法是看整句能不能被 `_FOLLOWUP_VOCAB`（泛指 + 追问 + 操作类
    用语）**完全覆盖**：
      * 返回 ``[]`` ⇒ 整句都是"只指当前页面"的说法，可以继续用当前页面的快照;
      * 返回非空  ⇒ 里面有服务端认不出的词（可能是简称/错别字/新名称）⇒ 调用方必须
        fail-closed，不能拿页面上的旧图硬答。

    这不是"猜名字"，方向是**正向**的（能证明"全是泛指/追问"才放行），与 coverage 无关。
    """
    if not text or _CODE_RE.search(text):
        return []
    out: list[str] = []
    for word in _unexplained_words(text):
        if word in out:
            continue
        out.append(word)
        if len(out) >= limit:
            break
    return out


def whole_text_name_codes(text: str, limit: int = 2) -> tuple[list[tuple[str, str, str]], str]:
    """整段汉字里能确认的名字 + **覆盖状态**（区分"没名字"与"没法查"）。

    coverage:
      * ``"local"``: 本地全名索引可用，已做全偏移窗口确认（贴紧提问词也认得出）;
      * ``"tq"``:    本地索引不可用，退回有界的 TQ 段首探测（只覆盖段首 3~4 字）;
      * ``"none"``:  两边都不可用 ⇒ hits 必为空，**调用方不能把它当成"没点名"**。

    **coverage 不是"全集已确认"**（2026-11-17 五轮审计）：本地名单只有全名 + 句首
    两字简称、TQ 只探段首，所以 ``"local"`` / ``"tq"`` 下 hits 为空同样可能漏掉句中
    名字。它只回答"这个结论是哪个通道给的"；要判断"能不能用当前页面的快照"，只能用
    `unverified_name_words()` 的**正向**结论（整句都由泛指/追问用语构成）。
    """
    if not text or _CODE_RE.search(text):
        return [], "local"
    if local_name_index() is not None:
        return glued_name_codes(text, limit=limit), "local"
    probe = _tq_prefix_probe(text, limit=limit)
    if probe is None:
        return [], "none"
    return probe, "tq"


def _name_hits(word: str) -> list[tuple[str, str]]:
    """名字 → [(code, name)]：**本地全名索引优先**（零网络），查不到再问 TQ。

    这个顺序是 2026-11-17 四轮审计的要求：本地索引里已有的全名（「浦发银行」）不该
    因为 TQ 一时不可达就被判成"解析不出"，否则对着这张图问「浦发银行缠论怎么样」
    也会被 fail-closed 误杀；反过来本地索引不可用时仍按老口径问 TQ（不改变既有行为）。
    """
    local = (local_name_index() or {}).get(word)
    if local:
        return local
    return _match_cached(word)


def _suggest(word: str) -> tuple[str, str] | None:
    """查不到时给最像的一只（用首字扩大召回 + 字面相似度）。

    「强锐技术」直接查是空的，但查「强」能拿到 23 只，其中就有 301128 强瑞技术。
    相似度 < 0.6 一律不出建议 —— 宁可说查不到，也不把别的票塞给用户。
    """
    import difflib

    cands = _name_hits(word)
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
        hits = _name_hits(word)
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

    覆盖边界（2026-11-17 四轮审计更新）：本函数只处理**有分隔符**的段；紧贴提问词的
    长串（「贵州茅台缠论怎么样」）交给 `glued_name_codes`（本地全名索引做整段窗口），
    两条路合起来就是 `whole_text_name_codes`。仍不覆盖的：本地索引不可用时的**段中**
    名字、以及 2 字简称（句首除外）—— 这类漏检如实记录，绝不按形状猜。
    """
    out: list[tuple[str, str, str]] = []
    if not text or _CODE_RE.search(text):
        return out
    attempts = 0
    local = local_name_index() or {}
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
        hits = local.get(seg) or _match_cached(seg)
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
