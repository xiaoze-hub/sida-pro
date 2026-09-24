#!/usr/bin/env python3
"""SIDA Skill Gateway 客户端（仅用 Python 标准库，无需 pip 安装）。

用法:
  python sida_client.py guest                            # 游客自动领 free 档 key（100次/天）
  python sida_client.py register "你的手机或微信标识"   # 领 AppKey（trial 档）
  python sida_client.py skills                          # 列可用 skill 与档位
  python sida_client.py run get_stock_quote '{"symbol":"600519","market":"CN"}'
  python sida_client.py run get_stock_quote '{"symbol":"600519"}' json   # 第4参指定输出格式
  python sida_client.py usage                           # 查当日用量与剩余额度

输出格式(format): html(默认, 美化模板内嵌 JSON) / json(纯结构化)。
业务接口返回信封 {code, success, data:{skill, result, caliber, risk, duration_ms, format}}。

环境变量:
  SIDA_BASE   API 基址，默认 https://www.sida.hengsheng-elec.com
  SIDA_KEY    AppKey（sk_ 开头），业务接口必需的凭据

凭据纪律: SIDA_KEY 请只放在环境变量或权限 600 的配置文件中，
          不要写进代码、不要提交进仓库、不要输出到日志。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "https://www.sida.hengsheng-elec.com"
BASE = os.environ.get("SIDA_BASE", DEFAULT_BASE).rstrip("/")
KEY = os.environ.get("SIDA_KEY", "")


def _req(method: str, path: str, body: dict | None = None, timeout: float = 30.0,
         need_key: bool = True) -> dict:
    """发一次请求并返回解包后的 JSON 对象。

    need_key=True 时要求 SIDA_KEY 已设置；/api/keys 与 /api/skills/catalog 无需凭据。
    """
    if not BASE:
        sys.exit("请设置环境变量 SIDA_BASE，例如: export SIDA_BASE=" + DEFAULT_BASE)
    if need_key and not KEY:
        sys.exit("缺少 SIDA_KEY。先领 key:\n"
                 '  python3 sida_client.py register "你的手机或微信标识"\n'
                 "再: export SIDA_KEY=sk_...")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if KEY:
        headers["X-API-Key"] = KEY

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:500]
        if e.code == 429:
            retry = e.headers.get("Retry-After", "?")
            sys.exit(f"[HTTP 429] 触发限流，Retry-After={retry} 秒。请退避后重试。\n{raw}")
        if e.code == 403:
            sys.exit(f"[HTTP 403] 档位不足或 key 被禁用。\n{raw}")
        if e.code == 401:
            sys.exit(f"[HTTP 401] API Key 缺失或无效。\n{raw}")
        try:
            payload = json.loads(raw)
        except Exception:
            sys.exit(f"[HTTP {e.code}] {raw}")
    except urllib.error.URLError as e:
        hint = "（若运行环境配置了 http_proxy，可尝试 unset http_proxy https_proxy）"
        sys.exit(f"连接失败: {e.reason} {hint}")

    # 裸 HTTP 返回信封是 {code, success, data:{...}}；这里统一解包出 data
    if isinstance(payload, dict) and "data" in payload and "success" in payload:
        return payload.get("data") or {}
    return payload


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]

    if cmd == "register":
        label = sys.argv[2] if len(sys.argv) > 2 else ""
        data = _req("POST", "/api/keys", {"owner_label": label, "trial": True}, need_key=False)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if isinstance(data, dict) and data.get("api_key"):
            print("\n请立即保存（明文只返回一次）。设置方式:")
            print(f"  export SIDA_KEY={data['api_key']}")

    elif cmd == "guest":
        # 游客自动领 free 档 key（无需任何信息，100 次/天）
        data = _req("POST", "/api/guest-key", need_key=False)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if isinstance(data, dict) and data.get("api_key"):
            print("\n请立即保存（明文只返回一次）。设置方式:")
            print(f"  export SIDA_KEY={data['api_key']}")

    elif cmd == "skills":
        data = _req("GET", "/api/skills")
        print(json.dumps(data, ensure_ascii=False, indent=2))

    elif cmd == "catalog":
        # 公开端点，无需凭据：列全部开放 skill 与档位/限流配置
        data = _req("GET", "/api/skills/catalog", need_key=False)
        print(json.dumps(data, ensure_ascii=False, indent=2))

    elif cmd == "run":
        if len(sys.argv) < 3:
            sys.exit('用法: python sida_client.py run <skill_name> \'{"symbol":"600519"}\' [format]')
        name = sys.argv[2]
        payload = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        # 参数需包一层 args；这里允许用户省略，自动补上
        if "args" not in payload:
            payload = {"args": payload}
        # 第 4 参可选指定输出格式 html/json（默认 html）
        if len(sys.argv) > 4:
            payload["format"] = sys.argv[4]
        print(json.dumps(_req("POST", f"/api/skills/{name}/run", payload, timeout=60),
                         ensure_ascii=False, indent=2))

    elif cmd == "usage":
        print(json.dumps(_req("GET", "/api/usage"), ensure_ascii=False, indent=2))

    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
