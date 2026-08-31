"""
通达信 L2AMO 分档资金公式封装
================================
通过 TQ 公式引擎的 ZJBY（资金搏弈）公式跑出超/大/中/小 4 档主力净额。

背景：
  通达信 L2 的 L2_AMO(N, M) 函数返回逐笔分档金额：
    N=0~3: 超大/大/中/小  M=0:买  M=1:卖
  官方 ZLJE 公式将其拆为 8 个中间变量 + 主力净额。
  但该公式未在系统中注册，因此使用系统内置的 ZJBY 公式，它计算的是
  相同的 4 档净额（买入-卖出），结果等效。

  ZJBY 输出线：
    1 主力净额 = 超大单净额 + 大单净额
    2 超大单净额 = L2_AMO(0,0) - L2_AMO(0,1)
    3 大单净额   = L2_AMO(1,0) - L2_AMO(1,1)
    4 中单净额   = L2_AMO(2,0) - L2_AMO(2,1)
    5 小单净额   = L2_AMO(3,0) - L2_AMO(3,1)

  单位：万元（与通达信一致）

  对照验证：ZJBY 主力净额 == get_more_info.Zjl_HB（完全一致）

用法：
  python l2_amo_formula.py                          # 跑默认三只股票
  python l2_amo_formula.py 002361 000001 600519     # 指定股票代码
"""

import json
import sys
import urllib.request
from typing import Optional

# ── TQ 服务地址 ──────────────────────────────────────────────
# 优先使用环境变量 TQ_URL，否则用默认本地地址
# 通过 frp 到云端映射时：export TQ_URL="http://101.35.244.238:5100/"
import os
TQ_URL = os.environ.get("TQ_URL", "http://127.0.0.1:17709/")


def _tq_call(method: str, params: dict) -> dict:
    """向 TQ HTTP 服务发送 JSON-RPC 请求并返回 result 部分。"""
    payload = json.dumps({"id": 1, "method": method, "params": params}, ensure_ascii=False)
    req = urllib.request.Request(
        TQ_URL,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"HTTP 请求失败: {e}"}

    if "error" in raw:
        return {"error": f"TQ 接口错误: {raw['error']}"}

    result = raw.get("result", {})
    err_id = result.get("ErrorId", "0")
    if err_id != "0" and err_id is not None:
        return {"error": f"TQ ErrorId={err_id}, Value={result.get('Value', result)}"}
    return result


def _normalize_code(code: str) -> str:
    """补齐股票代码后缀（深交所 .SZ, 上交所 .SH, 北交所 .BJ）。"""
    code = code.strip().upper()
    if "." in code:
        return code
    # 深交所: 000~002, 300, 301, 001
    # 上交所: 600, 601, 603, 605, 688, 689
    # 北交所: 4, 8 开头
    if code.startswith(("6", "9")):
        return code + ".SH"
    elif code.startswith(("0", "3", "2")):
        return code + ".SZ"
    elif code.startswith(("4", "8")):
        return code + ".BJ"
    return code


def get_l2_fund_flow(code: str, verbose: bool = False) -> dict:
    """
    通过 TQ 公式引擎获取单只股票的 L2 分档资金净额。

    参数
    ----
    code : str
        股票代码，如 "002361" 或 "002361.SZ"
    verbose : bool
        是否打印原始返回值用于调试

    返回
    ----
    dict
        {
            "code":           "002361.SZ",
            "name":           "神剑股份",       # 从 get_more_info 补全
            "hq_date":        "20260826",
            "超大净额":       84.20,             # 万元
            "大净额":         -1296.11,
            "中净额":         -2629.12,
            "小净额":         3841.98,
            "主力净额":       -1211.91,           # 超大净额 + 大净额
            # ---- 对照字段 ----
            "zjl_hb":         -1211.91,           # get_more_info.Zjl_HB
            "zjl":            -4773.86,           # get_more_info.Zjl (总主买净额)
            # ---- 状态 ----
            "success":        True,
            "error":          None,
        }

    如果 ZJBY 公式不可用（如 L2 未开通），返回 {"success": False, "error": "..."}。
    """
    code = _normalize_code(code)

    # ── 1. 通过公式引擎获取 4 档净额 ──
    result = _tq_call("formula_process_mul_zb", {
        "formula_name": "ZJBY",
        "formula_arg": "",
        "return_count": 1,
        "return_date": False,
        "xsflag": 2,                     # 2 位小数
        "stock_list": [code],
        "stock_period": "1d",
        "count": 30,
        "dividend_type": 0,
    })

    if "error" in result:
        return {"success": False, "error": result["error"], "code": code}

    if verbose:
        print(f"[RAW] formula_process_mul_zb ZJBY: {json.dumps(result, ensure_ascii=False)}")

    # 提取股票数据
    stock_data = result.get(code, {})
    if not stock_data:
        return {"success": False, "error": f"股票 {code} 无返回数据", "code": code}

    # 解析各档净额
    try:
        flow = {
            "超大净额": float(stock_data.get("超大单", [0])[0]),
            "大净额":   float(stock_data.get("大单", [0])[0]),
            "中净额":   float(stock_data.get("中单", [0])[0]),
            "小净额":   float(stock_data.get("小单", [0])[0]),
        }
    except (ValueError, IndexError, TypeError) as e:
        return {"success": False, "error": f"解析 ZJBY 数据失败: {e}", "raw": stock_data, "code": code}

    flow["主力净额"] = flow["超大净额"] + flow["大净额"]

    # ── 2. 获取对照数据（get_more_info）─ ─
    mi = _tq_call("get_more_info", {"stock_code": code})
    if "error" not in mi:
        v = mi.get("Value", {})
        flow["zjl_hb"] = _safe_float(v.get("Zjl_HB"))
        flow["zjl"] = _safe_float(v.get("Zjl"))
        flow["hq_date"] = v.get("HqDate", "")
        stock_name = v.get("Name", "")
        if stock_name:
            flow["name"] = stock_name

    # 再从 get_stock_info 补名称
    if "name" not in flow:
        si = _tq_call("get_stock_info", {"stock_code": code, "field_list": ["Name"]})
        if "error" not in si:
            flow["name"] = si.get("Value", {}).get("Name", "")

    flow["code"] = code
    flow["success"] = True
    flow["error"] = None
    return flow


def _safe_float(v) -> Optional[float]:
    """安全转换字符串为 float，None/空返回 None。"""
    if v is None or v == "" or v == "N/A":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ── 主入口：命令行测试 ──────────────────────────────────────
def main():
    default_codes = ["002361", "000001", "600519"]
    codes = sys.argv[1:] if len(sys.argv) > 1 else default_codes

    print("=" * 70)
    print("L2 分档资金净额测试（ZJBY 公式引擎）")
    print("=" * 70)

    success_count = 0
    for code in codes:
        print(f"\n▶ {code}")
        print("-" * 50)
        result = get_l2_fund_flow(code, verbose=True)
        if result["success"]:
            name = result.get("name", "")
            hq_date = result.get("hq_date", "")
            print(f"  名称: {name}  |  行情日期: {hq_date}")
            print(f"  超大净额: {result['超大净额']:>10.2f} 万元")
            print(f"  大净额:   {result['大净额']:>10.2f} 万元")
            print(f"  中净额:   {result['中净额']:>10.2f} 万元")
            print(f"  小净额:   {result['小净额']:>10.2f} 万元")
            print(f"  主力净额: {result['主力净额']:>10.2f} 万元  (= 超大+大)")
            if result.get("zjl_hb") is not None:
                diff = abs(result["主力净额"] - result["zjl_hb"])
                ok = "✓" if diff < 0.1 else "✗"
                print(f"  Zjl_HB:   {result['zjl_hb']:>10.2f} 万元  {ok}")
            if result.get("zjl") is not None:
                print(f"  Zjl(总主买): {result['zjl']:>10.2f} 万元")
            success_count += 1
        else:
            print(f"  ❌ 失败: {result['error']}")

    print(f"\n{'=' * 70}")
    print(f"完成: {success_count}/{len(codes)} 成功")
    print("=" * 70)


if __name__ == "__main__":
    main()