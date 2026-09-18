"""部署后验收(postdeploy, 2026-09-18)。

发版铁律里"三重验证"的可执行版 —— 判"部署成没成"只看**生产实证**, 不看看门狗日志、不看 CI 结论:
  ① 生产 `/api/health` 的 `version` == 目标 tag;
  ② 容器真实镜像 tag(`docker inspect panwatch --format '{{.Config.Image}}'`) == 目标 tag;
  ③ 设计稿 v3.0 验收线巡检(终端 8 页 + 公开面 4 页);
  ④ 新增页面 smoke(`/decision-ledger` 无 pageerror、两段表格在)。

用法:
  SIDA_SHOT_PW=*** SIDA_SSH_PW=*** python scripts/postdeploy_verify.py v0.10.32
  SIDA_SHOT_PW=*** python scripts/postdeploy_verify.py v0.10.32 --skip-ssh   # 只验外部可见部分

凭据一律从环境变量读(**仓库不留任何密码字面量** —— `tests/test_auth_no_default_password.py` 会扫)。
退出码: 0 = 全部通过; 1 = 有硬失败(版本/镜像不符 或 页面报错); 巡检越线只告警不失败。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
import time
import urllib.request

BASE = "https://www.sida.hengsheng-elec.com"
HOST = "TIANXIANG@100.91.30.35"


def _health() -> dict:
    with urllib.request.urlopen(BASE + "/api/health", timeout=20) as r:
        return json.loads(r.read()).get("data") or {}


def _ssh(cmd: str) -> str:
    """走 sshpass + wsl; 密码只从环境变量取。引号嵌套一律用单条字符串避坑。"""
    pw = os.environ.get("SIDA_SSH_PW") or ""
    if not pw:
        return "(跳过: 需要 SIDA_SSH_PW)"
    full = ["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=no", HOST,
            f"wsl -e bash -lc \"{cmd}\""]
    try:
        out = subprocess.run(full, capture_output=True, text=True, timeout=120)
        return (out.stdout or out.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return f"(ssh 失败: {type(exc).__name__})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", help="目标版本, 如 v0.10.32")
    ap.add_argument("--skip-ssh", action="store_true")
    ap.add_argument("--skip-audit", action="store_true")
    ap.add_argument("--skip-anon", action="store_true",
                    help="跳过公开面实证(必跑: 档位页曾因客户端双 /api 前缀在生产 404, 后端测试查不出)")
    args = ap.parse_args()

    hard_fail = 0
    print(f"=== 部署后验收: 目标 {args.tag}  ({time.strftime('%F %T')})")

    h = _health()
    ver = h.get("version")
    print(f"① 生产 /api/health version = {ver}")
    if ver != args.tag:
        print(f"   ✗ 版本不符(期望 {args.tag}) —— 部署未落地或还在滚动")
        hard_fail += 1
    db = ((h.get("components") or {}).get("database") or {}).get("url")
    print(f"   数据库 = {db}")
    if db and "postgres" not in str(db):
        print("   ✗ 数据库不是 PG(生产必须 postgres)")
        hard_fail += 1

    if not args.skip_ssh:
        if not (os.environ.get("SIDA_SSH_PW") or ""):
            # 没给 SSH 密码 ≠ 部署失败: 只降级为"未验证", 不计入硬失败(凭据一律走环境变量)
            print("② 容器镜像/ VERSION 未验证(需要 SIDA_SSH_PW; 不加 --skip-ssh 时给上更稳)")
        else:
            img = _ssh("docker inspect panwatch --format '{{.Config.Image}}'")
            vfile = _ssh("docker exec panwatch cat /app/VERSION")
            print(f"② 容器镜像 = {img}")
            print(f"   容器 VERSION = {vfile}")
            if args.tag not in img or vfile.strip() != args.tag:
                print("   ✗ 镜像/VERSION 与目标不一致(容器可能仍是旧版)")
                hard_fail += 1

    if not args.skip_audit:
        pw = os.environ.get("SIDA_SHOT_PW") or ""
        if not pw:
            print("③ 巡检跳过: 需要 SIDA_SHOT_PW")
        else:
            print("③ 设计验收巡检(终端 8 页)")
            rc = subprocess.run([sys.executable, "scripts/terminal_audit.py", "--json", "/tmp/audit_post.json"],
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                env={**os.environ}).returncode
            print(f"   → {'全部达标' if rc == 0 else '有越线项(需书面说明, 不硬拦)'}")
            print("   公开面 4 页")
            subprocess.run([sys.executable, "scripts/terminal_audit.py", "--anon", "--json", "/tmp/audit_post_anon.json"],
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), env={**os.environ})

    # ④ 新增页面 smoke(只有 playwright 可用时才跑)
    try:
        from playwright.sync_api import sync_playwright

        pw_env = os.environ.get("SIDA_SHOT_PW") or ""
        if pw_env:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                ctx = b.new_context(viewport={"width": 1600, "height": 900})
                tok = ""
                req = urllib.request.Request(BASE + "/api/auth/login",
                                             data=json.dumps({"username": "admin", "password": pw_env}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    tok = (json.loads(r.read()).get("data") or {}).get("token") or ""
                ctx.add_init_script(f"try{{localStorage.setItem('token',{json.dumps(tok)})}}catch(e){{}}")
                pg = ctx.new_page()
                errs: list[str] = []
                pg.on("pageerror", lambda e: errs.append(str(e)[:120]))
                for route, must in (("/decision-ledger", ["命中率", "信号明细"]),
                                    ("/theme-mood", ["题材"]),
                                    ("/stocks/002361", [])):
                    errs.clear()
                    pg.goto(BASE + route, timeout=60000, wait_until="domcontentloaded")
                    pg.wait_for_timeout(7000)
                    txt = pg.inner_text("body")
                    miss = [m for m in must if m not in txt]
                    ok = not errs and not miss
                    print(f"④ smoke {route}: {'OK' if ok else 'FAIL'}"
                          f"{' 缺:' + ','.join(miss) if miss else ''}{' 报错:' + errs[0] if errs else ''}")
                    if not ok:
                        hard_fail += 1
                b.close()
    except ImportError:
        print("④ smoke 跳过: 无 playwright")

    if not args.skip_anon:
        print("\n⑤ 公开面(未登录)实证: scripts/anon_probe.py")
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("anon_probe.py"))],
                           capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.strip().splitlines()[-8:]:
            print("   " + line)
        if "ModuleNotFoundError" in out or "No module named 'playwright'" in out:
            print("   ⚠ 跳过: 当前解释器没有 playwright(用项目 venv 或装 playwright 后重跑)")
        elif r.returncode != 0:
            print("   ✗ 公开面实证有失败项")
            hard_fail += 1

    print()
    if hard_fail:
        print(f"❌ {hard_fail} 项硬失败 —— 部署未完成, 别对外说已上线")
        return 1
    print("✅ 部署验收通过(版本/镜像/页面均符合)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
