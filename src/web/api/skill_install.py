"""智能体一键安装(2026-09-16)。

- `GET /api/skills/install.sh`  返回可 `bash` 执行的安装脚本(text/plain, 不经 JSON 包装)
  用法: curl -sL https://<host>/api/skills/install.sh | bash -s -- YOUR_API_KEY
- `GET /api/skills/config`      返回当前登录用户的 skill 配置 JSON(不含明文 key)

安全:
- install.sh 本身不内嵌任何密钥; key 由调用方以 bash 参数 $1 传入
- /skills/config 走 JWT, 只回 key_prefix, 绝不回明文
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.web.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["skill-install"])

# 生产对外域名(前端「智能体安装」区块展示用; 与落地页一致)
PUBLIC_BASE_URL = "https://www.sida.hengsheng-elec.com"

_INSTALL_SH = r'''#!/usr/bin/env bash
# SIDA Skill Gateway 智能体一键安装
# 用法: curl -sL https://www.sida.hengsheng-elec.com/api/skills/install.sh | bash -s -- YOUR_API_KEY
set -euo pipefail

API_KEY="${1:-${SIDA_API_KEY:-}}"
BASE_URL="${SIDA_BASE_URL:-https://www.sida.hengsheng-elec.com}"

if [ -z "$API_KEY" ]; then
  echo "错误: 缺少 API Key" >&2
  echo "用法: curl -sL ${BASE_URL}/api/skills/install.sh | bash -s -- sk_xxx" >&2
  exit 1
fi

case "$API_KEY" in
  sk_*) ;;
  *) echo "错误: API Key 必须以 sk_ 开头" >&2; exit 1 ;;
esac

echo "==> 配置 SIDA Skill Gateway 环境变量"

write_rc() {
  local rc="$1"
  touch "$rc" 2>/dev/null || return 0
  # 幂等: 先移除旧的 SIDA 导出行
  if grep -q '^export SIDA_' "$rc" 2>/dev/null; then
    grep -v '^export SIDA_' "$rc" > "${rc}.sida.bak.$$" || true
    mv "${rc}.sida.bak.$$" "$rc"
  fi
  {
    echo ""
    echo "# SIDA Skill Gateway (auto-installed $(date -u +%Y-%m-%dT%H:%M:%SZ))"
    echo "export SIDA_API_KEY='${API_KEY}'"
    echo "export SIDA_BASE_URL='${BASE_URL}'"
  } >> "$rc"
}

for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  write_rc "$rc"
done

# 当前会话立即生效
export SIDA_API_KEY="$API_KEY"
export SIDA_BASE_URL="$BASE_URL"

echo "==> 验证 API Key..."
tmp_body="$(mktemp)"
trap 'rm -f "$tmp_body"' EXIT
code="$(curl -sS -o "$tmp_body" -w '%{http_code}' \
  -H "X-API-Key: ${API_KEY}" \
  "${BASE_URL}/api/usage" || echo 000)"

if [ "$code" != "200" ]; then
  echo "✗ 验证失败(HTTP ${code}), 请检查 Key 是否有效" >&2
  head -c 400 "$tmp_body" >&2 || true
  echo "" >&2
  exit 1
fi

echo "✓ API Key 验证通过"
if command -v python3 >/dev/null 2>&1; then
  python3 - "$tmp_body" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    data = d.get("data") if isinstance(d, dict) and "data" in d else d
    if isinstance(data, dict):
        print("  档位: {}  今日已用: {}/{}".format(
            data.get("tier", "?"),
            data.get("used_today", "?"),
            data.get("daily_limit", "?"),
        ))
except Exception:
    pass
PY
fi

cat <<EOF

安装完成。调用示例:

  curl -sS -X POST "\${SIDA_BASE_URL}/api/skills/get_stock_quote/run" \\
    -H "X-API-Key: \${SIDA_API_KEY}" \\
    -H 'Content-Type: application/json' \\
    -d '{"args": {"symbol": "000001"}}'

可用 skill 列表: GET \${SIDA_BASE_URL}/api/skills  (Header: X-API-Key)
EOF
'''


@router.get("/skills/install.sh")
def install_sh() -> Response:
    """返回安装脚本(text/plain)。中间件对非 JSON 响应原样透传, 可直接 pipe 给 bash。"""
    return Response(
        content=_INSTALL_SH,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="sida-skill-install.sh"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/skills/config")
def skills_config(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """当前登录用户的 skill 配置(不含明文 key, 只有 prefix)。

    前端「智能体安装」区块用它生成可复制的配置 JSON;
    若用户刚创建/重置过 key, 前端可把明文填入展示用 JSON(仅本地)。
    """
    from src.web.api.skills_gateway import (
        OPEN_SKILLS,
        TIER_DAILY_LIMIT,
        _best_active_key_for_user,
        _downgrade_if_expired,
        _header_str,
        _optional_jwt_user,
        refresh_tier_configs,
    )

    user = _optional_jwt_user(_header_str(request, "Authorization"), db)
    if user is None:
        raise HTTPException(401, "请先登录")

    refresh_tier_configs(db)
    uid = str(user.id)
    best = _best_active_key_for_user(db, uid)
    if best is not None:
        _downgrade_if_expired(best, db)

    tier = best.tier if best is not None else "free"
    daily_limit = best.daily_limit if best is not None else TIER_DAILY_LIMIT.get("free", 100)
    # free 档 skill 全量; 更高档包含 free
    from src.web.api.skills_gateway import TIER_RANK

    skills = sorted(
        name
        for name, meta in OPEN_SKILLS.items()
        if TIER_RANK.get(tier, 0) >= TIER_RANK.get(meta.get("tier_min", "free"), 0)
    )

    return {
        "base_url": PUBLIC_BASE_URL,
        "api_key_prefix": best.key_prefix if best is not None else None,
        "api_key": None,  # 绝不回明文
        "tier": tier,
        "daily_limit": daily_limit,
        "skills": skills,
        "endpoint": f"{PUBLIC_BASE_URL}/api/skills/{{name}}/run",
        "usage_endpoint": f"{PUBLIC_BASE_URL}/api/usage",
        "install_command": (
            f"curl -sL {PUBLIC_BASE_URL}/api/skills/install.sh | bash -s -- YOUR_API_KEY"
        ),
        "docs_url": f"{PUBLIC_BASE_URL}/developers",
    }
