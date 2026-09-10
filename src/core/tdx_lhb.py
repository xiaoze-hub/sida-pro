"""通达信龙虎榜(客户端页面缓存)解析 — 2026-09-10 老板要求"龙虎榜也可以接入吧"。

链路: 客户端打开「龙虎榜」页 → 数据落地 `T0002/cloud_cache/list/func_lhbfx101_1.jsn`
(榜单) + `T0002/cloud_cache/lhbfx/<关联ID>.jsn`(单股席位明细) → 宿主脚本拷进
容器可读目录(默认 /app/data/tdx_lhb) → 本模块解析。

格式(实测 2026-09-10): GBK 编码 JSON 数组, 首元素形如
  {"colheader": [...客户端列名...], "data": [[...行...], ...]}
列名映射(取自客户端 cloud_cfg/func_lhbfx101.cfg):
  $ZQDM1=代码 $SC1=市场(1沪/0深/2北?) date=上榜日期 bzb=买入成交占比% szb=卖出成交占比%
  jmr=净买入(元) zmr=买入合计 zmc=卖出合计 sl1=陆股通席位数 sl2=机构席位数
  lx=异动类型 lb=3日上榜 $ZQDM=关联ID(席位明细文件名)
席位明细列: $ZQDM sc date lb yyb=营业部(含"买(n):/卖(n):"前缀) czjl(内部链接,忽略)
  yzbq yyb1=买卖标记 yyb2=序号 bje=买入额 sje=卖出额 jmr=净买入 zb=占比%
  mrcgl1/3/5=买入后1/3/5日成功率% ygcb=预估成本 ygsy=预估收益%
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = "/app/data/tdx_lhb"
_BOARD_FILE = "func_lhbfx101_1.jsn"
_SEATS_DIR = "seats"


def _cache_dir() -> Path:
    return Path(os.environ.get("TDX_LHB_CACHE_DIR") or DEFAULT_CACHE_DIR)


def _num(v: object) -> float | None:
    try:
        s = str(v).strip()
        return float(s) if s not in ("", "None", "null") else None
    except Exception:  # noqa: BLE001
        return None


def _first_existing(*cands: Path) -> Path | None:
    for p in cands:
        if p.exists():
            return p
    return None


def _int(v: object) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def _board_path() -> Path | None:
    """榜单文件: 兼容"扁平同步布局"与客户端原生 `list/` 布局。"""
    root = _cache_dir()
    return _first_existing(root / _BOARD_FILE, root / "list" / _BOARD_FILE)


def _seat_path(ref_id: str) -> Path | None:
    root = _cache_dir()
    return _first_existing(root / _SEATS_DIR / f"{ref_id}.jsn", root / "lhbfx" / f"{ref_id}.jsn")


def _read_json(path: Path) -> dict | None:
    """缓存文件是 GBK JSON; 容忍 utf-8 回退与 BOM。"""
    try:
        raw = path.read_bytes()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取通达信缓存失败 %s: %s", path, e)
        return None
    for enc in ("gbk", "utf-8-sig", "utf-8"):
        try:
            data = json.loads(raw.decode(enc))
        except Exception:  # noqa: BLE001
            continue
        # 实测为 [{colheader,data}]; 兼容裸对象写法(格式漂移容错)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        if isinstance(data, dict) and "data" in data:
            return data
    logger.warning("通达信缓存解析失败(非预期结构): %s", path)
    return None


def parse_board(payload: dict) -> list[dict]:
    """榜单文件 → 规范化条目(纯函数)。"""
    cols = [str(c) for c in (payload.get("colheader") or [])]
    idx = {c: i for i, c in enumerate(cols)}
    out: list[dict] = []
    for row in payload.get("data") or []:
        if not isinstance(row, list):
            continue

        def cell(name: str):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None

        sym = str(cell("$ZQDM1") or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        out.append(
            {
                "symbol": sym,
                "market": _int(cell("$SC1")),
                "trade_date": str(cell("date") or "").strip(),
                "buy_ratio": _num(cell("bzb")),
                "sell_ratio": _num(cell("szb")),
                "net_buy": _num(cell("jmr")),
                "buy_amt": _num(cell("zmr")),
                "sell_amt": _num(cell("zmc")),
                "lg_seats": _int(cell("sl1")),
                "inst_seats": _int(cell("sl2")),
                "reason": str(cell("lx") or "").strip(),
                "merged_3d": _int(cell("lb")),
                "ref_id": str(cell("$ZQDM") or "").strip(),
            }
        )
    return out


def parse_seats(payload: dict) -> list[dict]:
    """席位明细文件 → 规范化条目(纯函数)。营业部名去掉"买(2): "前缀, 保留 side/rank。"""
    cols = [str(c) for c in (payload.get("colheader") or [])]
    idx = {c: i for i, c in enumerate(cols)}
    out: list[dict] = []
    for row in payload.get("data") or []:
        if not isinstance(row, list):
            continue

        def cell(name: str):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None

        raw_dept = str(cell("yyb") or "").strip()
        side, rank = None, None
        dept = raw_dept
        if raw_dept.startswith(("买(", "卖(")):
            side = "buy" if raw_dept.startswith("买(") else "sell"
            head, _, rest = raw_dept.partition(")")
            rank = _int(head[2:].rstrip("("))
            dept = rest.lstrip(": ").strip()
        out.append(
            {
                "symbol": str(cell("$ZQDM") or "").strip(),
                "trade_date": str(cell("date") or "").strip(),
                "side": side,
                "rank": rank,
                "dept": dept,
                "buy_amt": _num(cell("bje")),
                "sell_amt": _num(cell("sje")),
                "net_buy": _num(cell("jmr")),
                "ratio_pct": _num(cell("zb")),
                "win1_pct": _num(cell("mrcgl1")),
                "win3_pct": _num(cell("mrcgl3")),
                "win5_pct": _num(cell("mrcgl5")),
                "est_cost": _num(cell("ygcb")),
                "est_profit_pct": _num(cell("ygsy")),
            }
        )
    return out


def board(limit: int = 500) -> dict:
    """当前榜单(含文件新鲜度); 文件缺失/过期 → available=False(不伪造)。"""
    path = _board_path()
    payload = _read_json(path) if path else None
    if payload is None:
        return {"available": False, "reason": "通达信龙虎榜缓存不存在(客户端未打开过龙虎榜页或未同步)", "items": []}
    items = parse_board(payload)
    try:
        mtime = path.stat().st_mtime
    except Exception:  # noqa: BLE001
        mtime = None
    dates = [i["trade_date"] for i in items if i["trade_date"]]
    return {
        "available": True,
        "trade_date": max(dates) if dates else None,
        "count": len(items),
        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else None,
        "items": items[:limit],
    }


def seats(ref_id: str) -> dict:
    """单股席位明细(按榜单的 ref_id); 不存在 → available=False。"""
    rid = str(ref_id or "").strip()
    if not rid or not rid.isdigit():
        return {"available": False, "reason": "ref_id 非法", "items": []}
    path = _seat_path(rid)
    payload = _read_json(path) if path else None
    if payload is None:
        return {"available": False, "reason": f"席位明细缓存不存在(ref_id={rid})", "items": []}
    items = parse_seats(payload)
    return {"available": True, "ref_id": rid, "count": len(items), "items": items}
