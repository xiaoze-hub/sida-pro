"""依据行数字对账：把「哪一条依据里的数在本轮取数里找不到」落到**行**粒度。

为什么要到行：现有 `number_audit.annotate` 只给整段回答加一句脚注（"N 个数字未在工具返回中
找到出处"），用户看到脚注还得自己回正文找是哪一行。依据常写成 markdown 表格，一行一个
维度，落到行才好核对。

**它不说的话（与 evidence-provenance.ts 的纪律一致）**：
- 不说"某行来自某个工具"——本轮链路没有「依据行 → step」的关联，任何按关键词/顺序的
  归属都是伪造；
- 不说"某数是编造的"——只陈述"本轮工具返回与注入上下文里没有这个读数"这一可复核事实；
- 没有依据段就没有行级结论（`rows=[]` + `reason`），不拿整段回答凑一行。

口径全部复用 `number_audit.audit` 的容差（四舍五入、万元/亿折算、价差×股数、派生百分比），
避免行级比整段更严而误报。
"""
from __future__ import annotations

import re

from src.core.number_audit import audit as _audit_numbers
from src.core.number_audit import extract_claims

# 回答模板的段落标记（真实输出带 markdown：`## 【结论】`、`【口径汇总】`）。
_MARKER_RE = re.compile(r"【\s*(结论|依据|预案|口径|风险|操作)[^】]*】")
_EVIDENCE_LABEL_RE = re.compile(r"【\s*依据[^】]*】")
_TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")
_DECORATION_RE = re.compile(r"^\s*(?:#{1,6}\s*|>\s?|[-*+]\s+|\d+[.)]\s+)")


def _strip(text: str) -> str:
    out = text.replace("**", "").replace("__", "").replace("`", "")
    return _DECORATION_RE.sub("", out).strip()


def evidence_section(answer: str) -> str | None:
    """取出【依据】段落原文；没有该段返回 None（不拿全文凑）。"""
    if not answer:
        return None
    marks = list(_MARKER_RE.finditer(answer))
    start = None
    for i, m in enumerate(marks):
        if m.group(1) == "依据":
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(answer)
            return answer[start:end]
    if start is None and _EVIDENCE_LABEL_RE.search(answer):
        return answer[_EVIDENCE_LABEL_RE.search(answer).end():]  # type: ignore[union-attr]
    return None


def split_rows(section: str) -> list[str]:
    """依据段落 → 行（表格行、列表项、普通段落行都算一行）。"""
    rows: list[str] = []
    for raw in (section or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("---") or set(line) <= {"|", "-", ":", " "}:
            continue
        if _TABLE_SEP_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")] if line.startswith("|") else []
        if cells and all(c in ("维度", "关键读数", "口径", "项", "读数", "来源", "依据") for c in cells if c):
            continue  # 表头
        text = _strip(" ".join(cells) if cells else line)
        if text:
            rows.append(text)
    return rows


def audit_rows(answer: str, evidence: str, question: str = "") -> dict:
    """逐条依据行核对数字出处。

    返回:
      {
        "basis": "number_containment",   # 只做"数字出现在本轮取数里"的核对
        "rows": [{"index", "text", "claims", "unverified"}],   # 仅含有硬数字的行
        "checked": 参与核对的数字个数,
        "unverified_total": 查不到出处的数字个数,
        "reason": 没有依据段/没有数字时的原因（否则为空串）,
      }
    """
    section = evidence_section(answer or "")
    if section is None:
        return {
            "basis": "number_containment", "rows": [], "checked": 0,
            "unverified_total": 0, "reason": "no_evidence_section",
        }
    rows_out: list[dict] = []
    checked = unverified_total = 0
    for index, text in enumerate(split_rows(section)):
        claims = extract_claims(text)
        if not claims:
            continue
        result = _audit_numbers(text, evidence or "", question or "")
        checked += int(result.get("checked") or 0)
        bad = list(result.get("unverified") or [])
        unverified_total += len(bad)
        rows_out.append(
            {"index": index, "text": text, "claims": claims, "unverified": bad}
        )
    reason = "" if rows_out else "no_numeric_rows"
    return {
        "basis": "number_containment",
        "rows": rows_out,
        "checked": checked,
        "unverified_total": unverified_total,
        "reason": reason,
    }
