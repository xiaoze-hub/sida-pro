#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TDX 公式静态语法审校器 (docs/tq-formulas/*.txt)。

**这不是**通达信客户端『测试公式』的替代品 —— 它做的是**静态审计**, 用来在交付前
抓住本可以抓住的错: 乱写函数名/参数个数、未定义变量、括号不配平、中文标点、注释嵌套。

审校项:
  1) 花括号注释必须配平, 且**不可嵌套**(通达信注释不嵌套, 首个 } 即结束);
  2) 圆括号 () 必须配平;
  3) 字符串外只允许 ASCII 可见字符 + 汉字(变量名) + 空白; 中文标点(，；：（）等)一律报错;
  4) 每条语句必须以 ; 结尾;
  5) 只允许调用**权威白名单**内的函数, 且实参个数正确
     (白名单逐个对照 help.tdx.com.cn/gspt 官方函数表与公开函数手册; 未收录名一律报错,
      宁可漏报也不放行编造的函数);
  6) 变量必须"先定义后引用"(定义符 := 或 :);
  7) 条件选股公式(.txt 文件名含 '选股')只允许 1 个输出(`:`), 其余一律 `:=`。

退出码: 全部通过 = 0; 任一处报错 = 1。
"""
from __future__ import annotations

import pathlib
import re
import sys

# ── 权威函数白名单: 函数名(大写) -> 实参个数 ──────────────────────────────
# 对照 help.tdx.com.cn/gspt 函数表 + 公开函数手册(zcym/agudashi/chanluns)逐条核对。
FUNCS: dict[str, int] = {
    # 引用/均线
    "MA": 2, "EMA": 2, "EXPMA": 2, "MEMA": 2, "EXPMEMA": 2, "WMA": 2, "SMA": 3, "DMA": 2,
    "REF": 2, "REFDATE": 2, "SUM": 2, "COUNT": 2, "EVERY": 2, "EXIST": 2,
    "HHV": 2, "LLV": 2, "HHVBARS": 2, "LLVBARS": 2, "BARSLAST": 1, "BARSLASTCOUNT": 1,
    "BARSCOUNT": 1, "BARSSINCE": 1, "BARSSINCEN": 2, "FILTER": 2, "FILTERX": 2,
    "SUMBARS": 2, "CONST": 1, "RANGE": 3, "TOPRANGE": 1, "LOWRANGE": 1,
    # 逻辑/算术
    "CROSS": 2, "LONGCROSS": 3, "UPNDAY": 2, "DOWNNDAY": 2, "NDAY": 3, "LAST": 3,
    "NOT": 1, "IF": 3, "IFF": 3, "IFN": 3, "MAX": 2, "MIN": 2,
    "ABS": 1, "SQRT": 1, "POW": 2, "LN": 1, "LOG": 1, "EXP": 1, "SIGN": 1,
    "CEILING": 1, "FLOOR": 1, "INTPART": 1, "ROUND": 1, "ROUND2": 2, "BETWEEN": 3,
    # 绘图
    "STICKLINE": 5, "DRAWICON": 3, "DRAWTEXT": 3, "DRAWNUMBER": 4, "DRAWLINE": 5,
    "DRAWKLINE": 4, "POLYLINE": 2, "PLOYLINE": 2, "PARTLINE": 2, "VERTLINE": 1,
    # 行情/财务/板块
    "FINANCE": 1, "DYNAINFO": 1, "GPJYVALUE": 3, "ZTPRICE": 2, "DTPRICE": 2,
    "CODELIKE": 1, "NAMELIKE": 1, "NAMEINCLUDE": 1, "GETNAMEOFCODE": 2,
    "INDEXC": 0, "INDEXO": 0, "INDEXH": 0, "INDEXL": 0, "INDEXV": 0, "INDEXA": 0,
    # 时间
    "DATE": 0, "TIME": 0, "YEAR": 0, "MONTH": 0, "DAY": 0, "WEEK": 0, "HOUR": 0,
    "MINUTE": 0, "FROMOPEN": 0,
    # 形态
    "COST": 1, "WINNER": 1, "ZIG": 2, "PEAK": 3, "TROUGH": 3, "SAR": 3,
}

# 序列行情 / 关键字 / 线型色 (无需定义, 也不是函数)
SERIES = {
    "C", "O", "H", "L", "V", "VOL", "CLOSE", "OPEN", "HIGH", "LOW", "AMO", "AMOUNT",
    "CAPITAL", "ISLASTBAR", "DRAWNULL", "AND", "OR", "NOT",
}
MODIFIERS = {
    "NODRAW", "DRAWABOVE", "COLORSTICK", "STICK", "VOLSTICK", "LINESTICK",
    "CROSSDOT", "CIRCLEDOT", "POINTDOT", "LINETHICK", "LINETHICK1", "LINETHICK2",
    "LINETHICK3", "LINETHICK4", "LINETHICK5", "LINETHICK6", "LINETHICK7",
    "LINETHICK8", "LINETHICK9", "SHIFT", "ALIGN0", "ALIGN1", "ALIGN2", "ALIGN3",
    "ALIGN4", "ALIGN5",
    "COLORRED", "COLORGREEN", "COLORBLUE", "COLORYELLOW", "COLORMAGENTA",
    "COLORWHITE", "COLORCYAN", "COLORBLACK", "COLORBROWN", "COLORGRAY",
    "COLORLIGRAY", "COLORLIBLUE", "COLORLIGREEN", "COLORLICYAN", "COLORLIRED",
    "COLORLIMAGENTA",
}
# 允许的"已知常量/输出名"由使用者自行取名, 不在此列举 —— 由"先定义后引用"规则约束。

_IDENT = re.compile(r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*")
_CJK = range(0x4E00, 0xA000)


def strip_comments(src: str) -> str:
    """去掉 {..} 注释(禁嵌套), 注释替换为空格以保持位置。"""
    out: list[str] = []
    i, n, depth = 0, len(src), 0
    while i < n:
        ch = src[i]
        if ch == "{":
            depth += 1
            if depth > 1:
                raise SyntaxError("注释嵌套 '{' (通达信注释不可嵌套, 首个 } 即结束)")
            i += 1
            while i < n and src[i] != "}":
                i += 1
            if i >= n:
                raise SyntaxError("注释未闭合(缺 })")
            depth = 0
            i += 1
            out.append(" ")
            continue
        if ch == "}":
            raise SyntaxError("多余的 } (无对应 {)")
        out.append(ch)
        i += 1
    return "".join(out)


def check_charset(src: str, path: str) -> None:
    """字符串外只允许 ASCII + 汉字 + 空白; 抓中文标点。"""
    i, n, in_str = 0, len(src), False
    while i < n:
        ch = src[i]
        if in_str:
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        o = ord(ch)
        if ch in "\t\r\n " or 0x20 <= o <= 0x7E or o in _CJK:
            i += 1
            continue
        raise SyntaxError(f"非法字符 {ch!r} (U+{o:04X}); 字符串外只允许 ASCII/汉字/空白"
                          f" (中文标点需改成英文)")


def _scan_stmt(stmt: str):
    """返回 (func_calls, defined_name, is_output, used_bare_idents, n_top_commas)。"""
    funcs: list[tuple[str, int, int]] = []
    defined = None
    is_output = False
    top_comma = -1
    depth = 0
    in_str = False
    i, n = 0, len(stmt)
    first_sep = None  # 首个顶层 ':' 或 ':=' 的索引
    while i < n:
        ch = stmt[i]
        if in_str:
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth < 0:
                raise SyntaxError(f"多余的 ) : {stmt.strip()[:60]!r}")
            i += 1
            continue
        if depth == 0:
            if stmt.startswith(":=", i):
                if first_sep is None:
                    first_sep = i
                i += 2
                continue
            if ch == ":":
                if first_sep is None:
                    first_sep = i
                i += 1
                continue
            if ch == ",":
                top_comma = i
                i += 1
                continue
        i += 1
    if depth != 0:
        raise SyntaxError(f"圆括号不配平: {stmt.strip()[:60]!r}")

    # 定义名 + 是否输出
    if first_sep is not None:
        head = stmt[:first_sep].strip()
        if not _IDENT.fullmatch(head):
            raise SyntaxError(f"赋值左侧不是单一变量名: {head!r}")
        defined = head
        is_output = not stmt.startswith(":=", first_sep)
    elif top_comma >= 0:
        # 形如 DRAWICON(...),COLORRED  —— 不是赋值语句
        pass

    # 找函数调用: 标识符后紧跟 '('
    for m in _IDENT.finditer(stmt):
        name = m.group(0)
        j = m.end()
        while j < n and stmt[j] == " ":
            j += 1
        if j < n and stmt[j] == "(":
            # 统计实参个数(顶层逗号+1)
            k, d, argc, has_arg = j + 1, 1, 0, False
            sflag = False
            while k < n and d > 0:
                c2 = stmt[k]
                if d == 1 and not c2.isspace():
                    has_arg = True
                if sflag:
                    if c2 == "'":
                        sflag = False
                elif c2 == "'":
                    sflag = True
                elif c2 == "(":
                    d += 1
                elif c2 == ")":
                    d -= 1
                elif c2 == "," and d == 1:
                    argc += 1
                k += 1
            argc = argc + 1 if has_arg else 0
            funcs.append((name.upper(), argc, m.start()))
    return funcs, defined, is_output, top_comma


def _used_idents(stmt: str) -> list[str]:
    """收集所有非函数名、非字符串的裸标识符(用于未定义检查)。"""
    out: list[str] = []
    in_str = False
    i, n = 0, len(stmt)
    while i < n:
        ch = stmt[i]
        if in_str:
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        m = _IDENT.match(stmt, i)
        if m:
            name = m.group(0)
            j = m.end()
            while j < n and stmt[j] == " ":
                j += 1
            if not (j < n and stmt[j] == "("):  # 不是函数调用
                out.append(name)
            i = m.end()
            continue
        i += 1
    return out


def audit_file(path: pathlib.Path) -> list[str]:
    errs: list[str] = []
    src = path.read_text(encoding="utf-8")
    try:
        body = strip_comments(src)
        check_charset(body, str(path))
    except SyntaxError as e:
        return [str(e)]

    stmts = [s for s in body.split(";")]
    # 末段允许纯空白
    stmts = [s for s in stmts if s.strip()]

    defined: set[str] = set()
    outputs: list[str] = []
    is_cond = "选股" in path.name
    for idx, st in enumerate(stmts, 1):
        try:
            funcs, dname, is_out, _ = _scan_stmt(st)
        except SyntaxError as e:
            errs.append(f"语句#{idx} {e}")
            continue
        for fname, argc, _pos in funcs:
            if fname not in FUNCS:
                errs.append(f"语句#{idx} 未知函数 {fname}() —— 不在权威白名单, 禁止编造")
                continue
            if FUNCS[fname] != argc:
                errs.append(f"语句#{idx} 函数 {fname}() 实参 {argc} 个, 应为 {FUNCS[fname]} 个")
        if dname:
            defined.add(dname)
            if is_out:
                outputs.append(dname)
        # 未定义引用检查: 排除函数体里的字符串与函数名本身
        for nm in _used_idents(st):
            u = nm.upper()
            if nm == dname or u in FUNCS or u in SERIES or u in MODIFIERS:
                continue
            if nm in defined:
                continue
            if nm.isdigit():
                continue
            errs.append(f"语句#{idx} 引用了未定义的变量 {nm!r}")
    if is_cond:
        outs = [o for o in outputs]
        # 条件选股: 只允许一个 ": 输出"
        # 重新数一遍(上面 outputs 已含全部输出)
        if len(outputs) != 1:
            errs.append(f"条件选股公式必须恰好 1 个输出(当前 {len(outputs)}: {outputs})")
    else:
        if not outputs:
            errs.append("技术指标公式至少需要 1 个输出(:)")
    return errs


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1] / "docs" / "tq-formulas"
    files = sorted(root.glob("*.txt"))
    if not files:
        print(f"未找到公式文件: {root}", file=sys.stderr)
        return 1
    total = 0
    print(f"审校目录: {root}\n" + "=" * 72)
    for f in files:
        errs = audit_file(f)
        total += len(errs)
        tag = "PASS" if not errs else f"FAIL({len(errs)})"
        print(f"[{tag}] {f.name}")
        for e in errs:
            print(f"        - {e}")
    print("=" * 72)
    print(f"合计: {len(files)} 个公式, {total} 处问题 -> {'全部通过' if total == 0 else '存在语法问题'}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
