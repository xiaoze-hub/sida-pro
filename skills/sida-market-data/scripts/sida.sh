#!/usr/bin/env bash
# SIDA Skill Gateway 调用包装：自动载入 ~/.sida.env，无需每次手动 source。
#
# 用法：
#   scripts/sida.sh skills
#   scripts/sida.sh run get_stock_quote '{"symbol":"600519","market":"CN"}'
#   scripts/sida.sh usage
#
# 凭据文件（可选）：~/.sida.env，权限建议 600，内容形如
#   export SIDA_KEY=sk_xxxxxxxx
#   export SIDA_BASE=https://www.sida.hengsheng-elec.com
set -euo pipefail

ENVF="${SIDA_ENV_FILE:-$HOME/.sida.env}"
if [ -f "$ENVF" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENVF"
  set +a
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "未找到 python3，请先安装 Python 3.9+" >&2
  exit 2
fi

if [ -z "${SIDA_KEY:-}" ]; then
  echo "缺少 SIDA_KEY。先领一个免费 trial key：" >&2
  echo "  python3 \"$DIR/sida_client.py\" register \"你的手机或微信标识\"" >&2
  echo "然后写入 $ENVF（chmod 600）或直接 export SIDA_KEY=sk_..." >&2
  exit 2
fi

exec "$PY" "$DIR/sida_client.py" "$@"
