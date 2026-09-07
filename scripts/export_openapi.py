"""导出 OpenAPI 契约快照(P1 成熟化)。

生产 app 为防接口地图泄露关了 /docs/openapi.json, 此脚本在进程内临时打开,
落盘到 docs/_frozen/openapi.p1.json, 作为前后端契约基线:
  python scripts/export_openapi.py

前端 codegen(P3 接): pnpm dlx orval --input docs/_frozen/openapi.p1.json \
  --output frontend/src/api/__gen__ --client react-query
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.web.app import app  # noqa: E402

app.openapi_url = "/openapi.json"
spec = app.openapi()
out = PROJECT_ROOT / "docs" / "_frozen" / "openapi.p1.json"
out.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
paths = len(spec.get("paths", {}))
print(f"openapi snapshot: {paths} paths -> {out}")
