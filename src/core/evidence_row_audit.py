"""依据行数字对账：把「哪一条依据里的数在本轮取数里找不到」落到**行**粒度。

为什么要到行：现有 `number_audit.annotate` 只给整段回答加一句脚注（"N 个数字未在工具返回中
找到出处"），用户看到脚注还得自己回正文找是哪一行。依据常写成 markdown 表格，一行一个
维度，落到行才好核对。

**它不说的话（与 evidence-provenance.ts 的纪律一致）**：
- 不说"某行来自某个工具"——本轮链路没有「依据行 → step」的关联，任何按关键词/顺序的
  归属都是伪造；
- 不说"某数是编造的"——只陈述"本轮工具返回与注入上下文里没有这个读数"这一可复核事实；
- 没有依据段就没有行级结论（`rows=[]` + `reason`），不拿整段回答凑一行。

实现口径（两条硬约束）：
1. **不重算口径**：`number_audit` 是冻结模块，容差（四舍五入、万元/亿折算、价差×股数、
   派生百分比）只许它算。本模块复用**同一份** audit 结论，再按行取"已判定查不到出处"的
   token 交集，绝不自己发明第二套比较逻辑。
2. **只算一次**：`audit` 的派生集合是 O(池²)（池上限 400 ⇒ 十几万次配对）。逐行各调一次
   会让同一次回答付 N 倍代价（实测 12 行 / 300 读数 = 3.8s，直接卡住事件循环）。所以行级
   结论从一份 audit 结果派生；调用方已有结果时直接传入（`rows_from_audit`）。
"""
from __future__ import annotations

import re

from src.core.number_audit import audit as _audit_numbers
from src.core.number_audit import extract_claims

# 段落标记：与前端 `answer-sections.ts` 的边界词对齐 —— 依据段后面常跟【数据缺口】【免责】
# 这类段而不带【预案】/【口径】，边界认窄了会把它们折进依据、当"依据行"报给用户。
_BOUNDARY_WORDS = (
    "结论", "依据", "预案", "口径", "风险", "操作",
    "数据缺口", "缺口", "缺失数据", "免责", "置信度", "来源",
    "统计", "说明", "备注", "局限", "下一步",
)
_MARKER_RE = re.compile(r"【\s*(" + "|".join(_BOUNDARY_WORDS) + r")[^】]*】")
# annotate 追加的对账脚注：正文之外的附言，不能当成依据行再核一遍（否则把脚注自己报回来）。
_FOOTER_RE = re.compile(r"^(?:-{3,}|⚠️|数字对账)")
_TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")
_DECORATION_RE = re.compile(r"^\s*(?:#{1,6}\s*|>\s?|[-*+]\s+|\d+[.)]\s+)")
_HEADER_CELLS = {"维度", "关键读数", "口径", "项", "读数", "来源", "依据"}
# 行数上限：正常依据段不超过 10 行；异常长的表格不逐行核（返回里如实标 truncated）。
MAX_ROWS = 24


def _strip(text: str) -> str:
    out = text.replace("**", "").replace("__", "").replace("`", "")
    return _DECORATION_RE.sub("", out).strip()


def evidence_section(answer: str) -> str | None:
    """取出【依据】段落原文；没有该段返回 None（不拿全文凑）。"""
    if not answer:
        return None
    marks = list(_MARKER_RE.finditer(answer))
    for i, m in enumerate(marks):
        if m.group(1) == "依据":
            end = marks[i + 1].start() if i + 1 < len(marks) else len(answer)
            return answer[m.end():end]
    return None


def split_rows(section: str) -> list[str]:
    """依据段落 → 行（表格行、列表项、普通段落行都算一行）；脚注行剔除。"""
    rows: list[str] = []
    for raw in (section or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("---") or set(line) <= {"|", "-", ":", " "}:
            continue
        if _TABLE_SEP_RE.match(line) or _FOOTER_RE.match(_strip(line)):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")] if line.startswith("|") else []
        if cells and all(c in _HEADER_CELLS for c in cells if c):
            continue  # 表头
        text = _strip(" ".join(cells) if cells else line)
        if text:
            rows.append(text)
    return rows


def rows_from_audit(audit_result: dict, section: str) -> tuple[list[dict], bool]:
    """把**一份** audit 结论按行切开：行内 token ∩ 全局 unverified。

    容差判定完全由 `number_audit.audit` 完成，这里只做归属（同一 token 出现在两行就两行都标）。
    返回 `(rows, truncated)`。
    """
    bad = {str(t) for t in (audit_result.get("unverified") or [])}
    rows: list[dict] = []
    truncated = False
    for index, text in enumerate(split_rows(section)):
        claims = extract_claims(text)
        if not claims:
            continue
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        rows.append({
            "index": index,
            "text": text,
            "claims": claims,
            "unverified": [c for c in claims if c in bad],
        })
    return rows, truncated


def audit_rows(answer: str, evidence: str, question: str = "") -> dict:
    """逐条依据行核对数字出处（便利入口：内部**只调一次** `number_audit.audit`）。

    返回:
      {
        "basis": "number_containment",   # 只做"数字出现在本轮取数里"的核对
        "rows": [{"index", "text", "claims", "unverified"}],   # 仅含有硬数字的行
        "checked": 参与核对的数字个数,
        "unverified_total": 查不到出处的数字个数,
        "reason": 没有依据段/没有数字时的原因（内部诊断用，正常为空串）,
        "truncated": 依据行超过 MAX_ROWS 时为 True,
      }
    """
    section = evidence_section(answer or "")
    if section is None:
        return {
            "basis": "number_containment", "rows": [], "checked": 0,
            "unverified_total": 0, "reason": "no_evidence_section", "truncated": False,
        }
    result = _audit_numbers(section, evidence or "", question or "") if section.strip() else {}
    rows, truncated = rows_from_audit(result, section)
    return {
        "basis": "number_containment",
        "rows": rows,
        "checked": len({c for r in rows for c in r["claims"]}),
        "unverified_total": sum(len(r["unverified"]) for r in rows),
        "reason": "" if rows else "no_numeric_rows",
        "truncated": truncated,
    }
