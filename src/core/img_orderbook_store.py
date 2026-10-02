# -*- coding: utf-8 -*-
"""`.img` 十档盘口帧落库(入库层, 2026-10-02)。

范围: 把通达信 `.img` 逐帧十档盘口 + 委托队列(解析见 `src.core.tdx_img_parser`)
落到版本化表 `img_orderbook_frames`(schema 见 `src/web/migrations.py: _m181_...`),
供盘后回放 / 盘口队列展示。收口遗留 A2「`.img` 生产无消费方」。

口径(项目硬约束):
- 单位: 价格=元, 量=股, 笔数=笔。
- **缺失一律 NULL, 不补 0**: 0 是有意义的挂单量, 与「无数据」必须可区分。
- `source` / `as_of` 随行落库: 来源与数据时间显式标注, 不拿陈旧值冒充实时。
- **多用户隔离**: 每行带 `user_id`; 读路径一律按 `user_id` 过滤(4 账号并存)。
- 表结构唯一入口是版本化迁移, 本模块**不**建表/加列(AGENTS 铁律)。

`trade_date` 来源: 显式传参 > 文件名里的 8 位日期(如 `sz002361_20260827.img`)。
两者皆无 → 显式报错(不猜日期; 猜错会把帧挂到错误交易日)。

写库永不抛(返回错误 dict / 0 行), 与 quote_snapshots 一致。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import text

logger = logging.getLogger(__name__)

_TABLE = "img_orderbook_frames"
# 插入列(与 _m181 迁移 DDL 对齐; 冲突列单独给出)
_INSERT_COLS = (
    "trade_date", "as_of", "seq", "symbol", "market", "user_id", "source", "img_path",
    "bid_prices", "bid_vols", "ask_prices", "ask_vols",
    "bid_queue", "ask_queue", "bid_orders", "ask_orders",
    "spread", "bid_pressure",
)
# 身份键含 seq(源文件内帧序): .img 存在同秒多帧, 仅 as_of 会丢帧
_CONFLICT_COLS = ("user_id", "market", "symbol", "trade_date", "seq")

# 未指定用户时的共享 scope(市场公共数据; 显式命名, 不隐式跨用户)
SHARED_SCOPE = "shared"

_DATE_RE = re.compile(r"(20\d{6})")


def _engine():
    from src.db.session import engine

    return engine


def trade_date_from_path(path: str | Path) -> Optional[str]:
    """从 `.img` 文件名提取 8 位交易日(如 `sz002361_20260827.img` → `20260827`)。"""
    m = _DATE_RE.search(Path(path).name)
    return m.group(1) if m else None


def _iso_as_of(trade_date: str, t: str) -> str:
    """(交易日, HH:MM:SS) → ISO 风格 as_of(上海本地, 无时区后缀; 与既有 as_of 口径一致)。"""
    return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T{t}"


def _json_or_null(values: Optional[Sequence[Optional[Any]]]) -> Optional[str]:
    """档位/队列列表 → JSON 文本; 空 → None(不写 `[]` 冒充有数据)。"""
    if not values:
        return None
    return json.dumps(list(values))


def snapshot_to_row(
    snap: Any,
    *,
    trade_date: str,
    symbol: str,
    seq: int = 0,
    market: str = "CN",
    user_id: str = SHARED_SCOPE,
    source: str = "img",
    img_path: Optional[str] = None,
) -> Optional[dict]:
    """ImgSnapshot → 一行(缺失为 None, 不补 0)。帧无时间(无法定位)→ None。

    `seq` = 源文件内帧序(0 起), 进唯一键: .img 存在同秒多帧(实测 22/5066),
    只用 as_of 会把同秒帧覆盖掉。
    """
    if not getattr(snap, "t", None):
        return None
    row = {
        "trade_date": trade_date,
        "as_of": _iso_as_of(trade_date, snap.t),
        "seq": int(seq),
        "symbol": symbol,
        "market": market,
        "user_id": user_id,
        "source": source,
        "img_path": str(img_path) if img_path else None,
        "bid_prices": _json_or_null(snap.bid_prices),
        "bid_vols": _json_or_null(snap.bid_vols),
        "ask_prices": _json_or_null(snap.ask_prices),
        "ask_vols": _json_or_null(snap.ask_vols),
        "bid_queue": _json_or_null(getattr(snap, "bid_queue", None)),
        "ask_queue": _json_or_null(getattr(snap, "queue", None)),
        "bid_orders": snap.bid_orders,
        "ask_orders": snap.ask_orders,
        "spread": snap.spread(),
        "bid_pressure": snap.bid_pressure(),
    }
    return row


def persist_frames(rows: Sequence[dict]) -> int:
    """幂等写入(唯一键冲突即覆盖), 返回写入行数。失败不抛。"""
    if not rows:
        return 0
    from src.db.dialect import upsert_sql

    sql = upsert_sql(
        _TABLE, _INSERT_COLS, _CONFLICT_COLS,
        tuple(c for c in _INSERT_COLS if c not in _CONFLICT_COLS),
    )
    try:
        with _engine().begin() as conn:
            for r in rows:
                conn.execute(text(sql), {c: r.get(c) for c in _INSERT_COLS})
    except Exception as e:  # noqa: BLE001
        logger.warning("img 盘口落库失败(%d 行): %s", len(rows), e)
        return 0
    return len(rows)


def ingest_img_file(
    path: str | Path,
    *,
    symbol: Optional[str] = None,
    market: str = "CN",
    user_id: str = SHARED_SCOPE,
    trade_date: Optional[str] = None,
    limit: Optional[int] = None,
    source: str = "img",
) -> dict:
    """解析 `.img` 并落库(永不抛)。返回显式状态 dict。

    Returns:
        {available, source, as_of, trade_date, symbol, n_frames, frames_saved,
         frames_skipped, img_path, note} —— 失败时 available=False + note/error,
         不编造任何盘口数字。
    """
    from src.core.tdx_img_parser import ImgParseError, parse_img

    p = Path(path)
    out: dict = {
        "available": False, "source": source, "as_of": None,
        "trade_date": trade_date or None, "symbol": symbol, "n_frames": 0,
        "frames_saved": 0, "frames_skipped": 0, "img_path": str(p), "note": "",
    }
    td = trade_date or trade_date_from_path(p)
    if not td:
        out["note"] = f"无法确定交易日(文件名无 8 位日期且未显式传入): {p.name}"
        return out
    if not symbol:
        out["note"] = "缺少 symbol(无法归属标的, 不猜)"
        return out
    out["trade_date"] = td
    try:
        snaps = parse_img(p)
    except FileNotFoundError:
        out["note"] = f".img 文件不存在: {p}"
        return out
    except ImgParseError as e:
        out["note"] = f".img 解析失败: {e}"
        out["error"] = str(e)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("img 入库解析异常 %s: %s", p, e)
        out["note"] = f".img 解析异常: {e}"
        out["error"] = str(e)
        return out

    rows = []
    for seq, s in enumerate(snaps):   # seq = 源文件内帧序(确定性; limit 不改它)
        r = snapshot_to_row(
            s, trade_date=td, symbol=symbol, seq=seq, market=market,
            user_id=user_id, source=source, img_path=str(p),
        )
        if r is None:
            out["frames_skipped"] += 1
            continue
        rows.append(r)
    if limit:
        rows = rows[:limit]
    out["n_frames"] = len(rows)
    out["as_of"] = rows[-1]["as_of"] if rows else None
    out["frames_saved"] = persist_frames(rows)
    out["available"] = bool(rows) and out["frames_saved"] > 0
    out["note"] = (
        f"已入库 {out['frames_saved']} 帧"
        + (f"(跳过无时间帧 {out['frames_skipped']})" if out["frames_skipped"] else "")
        if out["available"]
        else "解析到帧但写库为 0 行(见日志)"
    )
    return out


def _decode(value: Optional[str]) -> Optional[list]:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _row_to_dict(r: Any) -> dict:
    m = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
    for k in ("bid_prices", "bid_vols", "ask_prices", "ask_vols", "bid_queue", "ask_queue"):
        if k in m:
            m[k] = _decode(m[k])
    return m


def load_frames(
    symbol: str,
    *,
    user_id: str,
    market: str = "CN",
    trade_date: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """按 user_id 隔离读取已入库盘口帧(按 as_of 升序)。失败/无数据 → []。

    **必须显式给 `user_id`**(不隐式跨用户); 缺失值保持 None, 不补 0。
    """
    sql = (
        f"SELECT * FROM {_TABLE} WHERE user_id = :uid AND symbol = :sym AND market = :mkt"
    )
    params: dict = {"uid": user_id, "sym": symbol, "mkt": market}
    if trade_date:
        sql += " AND trade_date = :td"
        params["td"] = trade_date
    sql += " ORDER BY as_of ASC, seq ASC"
    if limit:
        sql += " LIMIT :lim"
        params["lim"] = int(limit)
    try:
        with _engine().connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("img 盘口读取失败 %s/%s: %s", user_id, symbol, e)
        return []
    return [_row_to_dict(r) for r in rows]


def latest_frame(
    symbol: str, *, user_id: str, market: str = "CN"
) -> Optional[dict]:
    """按 user_id 隔离取最新一帧; 无数据 → None(调用方显式标「无数据」)。"""
    rows = load_frames(symbol, user_id=user_id, market=market)
    return rows[-1] if rows else None


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: 把本地 .img 入库(供运维/盘后回放脚本调用)。

        python -m src.core.img_orderbook_store --img ~/tdx_data/sz002361_20260827.img \
            --symbol 002361 --user-id shared [--trade-date 20260827] [--limit 500]
    """
    import argparse

    ap = argparse.ArgumentParser(description="通达信 .img 十档盘口帧入库")
    ap.add_argument("--img", required=True, help=".img 文件路径")
    ap.add_argument("--symbol", required=True, help="6 位代码")
    ap.add_argument("--market", default="CN")
    ap.add_argument("--user-id", default=SHARED_SCOPE)
    ap.add_argument("--trade-date", default=None, help="YYYYMMDD; 缺省从文件名提取")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    res = ingest_img_file(
        args.img, symbol=args.symbol, market=args.market, user_id=args.user_id,
        trade_date=args.trade_date, limit=args.limit,
    )
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("available") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
