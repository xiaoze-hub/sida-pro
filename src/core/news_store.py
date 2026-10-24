"""新闻/政策沉淀与检索（2026-10-24）。

**为什么要有这个模块**：`news_cache` 表早就建好（`source/external_id` 唯一约束），
但生产实测 **0 行** —— 快讯只在 `get_market_news` 时实时拉一次就丢，从不落库。
于是参谋问「上周有哪些政策」「最近三天关于半导体的消息」只能答"没有这个能力"：
不是缺工具，是**缺历史**。

修法：取快讯时顺手 upsert 入库（幂等，`uq_news_source_external`），
再开 `search_news` 工具查本地历史；关键词命中政策类词时打 `policy` 标记。

⚠️ 只沉淀、不补造：库里没有的区间就是没有，查询必须如实说「本地无该区间记录」，
不拿当下的快讯冒充历史。数据从部署后开始积累，这一点在工具返回里也要写清楚。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

# 政策/监管类关键词。命中即视为「政策口径」，用于 policy_only 过滤。
# 只做**分类**，不做判断 —— 不因为命中就说是利好还是利空。
POLICY_KEYWORDS = (
    "工信部", "发改委", "国务院", "证监会", "央行", "人民银行", "财政部", "商务部",
    "国家能源局", "市场监管总局", "税务总局", "银保监", "金融监管总局", "交易所",
    "政策", "规划", "指导意见", "管理办法", "实施细则", "专项行动", "征求意见",
    "补贴", "关税", "降准", "降息", "加息", "逆回购", "专项债", "国常会",
)


def is_policy(title: str, content: str = "") -> bool:
    """标题/正文是否政策类。纯关键词分类，不做情绪判断。"""
    blob = f"{title or ''} {content or ''}"
    return any(k in blob for k in POLICY_KEYWORDS)


def _parse_time(v: Any) -> datetime | None:
    """把来源侧的时间统一成 naive CST datetime（DB 列是 timestamp without tz）。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        if not s:
            return None
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d%H%M%S", "%Y%m%d"):
            try:
                dt = datetime.strptime(s[: len(fmt) + 2].strip(), fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_CST).replace(tzinfo=None)
    return dt


def _external_id(item: Any, source: str, title: str, ts: datetime | None) -> str:
    """来源没给 id 时用 (source+title+分钟) 做稳定指纹，保证重复拉取不会重复入库。"""
    for attr in ("id", "external_id", "uid", "news_id"):
        v = getattr(item, attr, None) if not isinstance(item, dict) else item.get(attr)
        if v:
            return str(v)[:64]
    stamp = ts.strftime("%Y%m%d%H%M") if ts else ""
    return re.sub(r"\s+", "", f"{source}:{stamp}:{title}")[:64]


_warned = [False]


def save_flash_news(db: Session, items: Iterable[Any]) -> int:
    """把快讯幂等写入 news_cache，返回本次**新增**条数。

    幂等键 = (source, external_id)；已存在则跳过（不更新正文，避免抖动覆盖）。
    失败一律吞异常并记日志 —— 沉淀是旁路，绝不能让它影响取快讯本身。
    """
    saved = 0
    for it in items or []:
        try:
            get = it.get if isinstance(it, dict) else (lambda k, d=None: getattr(it, k, d))
            title = str(get("title", "") or "").strip()
            if not title:
                continue
            source = str(get("source", "") or "").strip() or "unknown"
            ts = _parse_time(get("publish_time", None)) or datetime.now(_CST).replace(tzinfo=None)
            ext = _external_id(it, source, title, ts)
            content = str(get("content", "") or "")[:4000]
            symbols = get("symbols", None) or []
            if not isinstance(symbols, list):
                symbols = [str(symbols)]
            # policy 命中记 importance=1（可供排序/过滤），不影响原始 0-3 语义？
            # → 不动 importance（那是来源给的），policy 由查询侧按关键词判，避免篡改来源字段
            res = db.execute(
                text(
                    "INSERT INTO news_cache (source, external_id, title, content, publish_time, symbols, importance) "
                    "VALUES (:src, :ext, :title, :content, :pt, :syms, :imp) "
                    "ON CONFLICT (source, external_id) DO NOTHING"
                ),
                {
                    "src": source,
                    "ext": ext,
                    "title": title[:500],
                    "content": content,
                    "pt": ts,
                    # JSON 列必须自己序列化: 直接把 list 绑进 raw SQL 会
                    # `type 'list' is not supported`(SQLite/PG 都不接受), 而异常被下面
                    # 的 except 吞掉 → 表现为"永远写 0 条"(本地踩过, 别再用 debug 掩盖)
                    "syms": json.dumps(symbols, ensure_ascii=False),
                    "imp": int(get("importance", 0) or 0),
                },
            )
            saved += max(res.rowcount or 0, 0)
        except Exception as e:  # noqa: BLE001 — 沉淀失败不影响主流程
            # 首次失败用 warning: 静默的"永远写 0 条"在测试里表现为断言失败、
            # 在生产里表现为"库里永远没数据", 两者都比一条告警难查得多。
            if not _warned[0]:
                logger.warning("news_cache 写入失败(后续降为 debug): %s", e)
                _warned[0] = True
            try:
                db.rollback()
            except Exception:
                pass
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("news_cache 提交失败: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    return saved


def _fmt_ts(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M")
    return str(v)[:16]


def _is_postgres(db: Session) -> bool:
    try:
        name = db.get_bind().dialect.name
    except Exception:  # noqa: BLE001
        return False
    return name in ("postgresql", "postgres")


def search_news(
    db: Session,
    keyword: str | None = None,
    days: int = 7,
    policy_only: bool = False,
    limit: int = 30,
) -> list[dict]:
    """按时间窗口 + 关键词检索本地新闻库。

    返回按 publish_time 倒序的条目。`days` 上限 120（避免全表扫）。
    """
    days = max(1, min(int(days or 7), 120))
    limit = max(1, min(int(limit or 30), 100))
    since = datetime.now(_CST).replace(tzinfo=None) - timedelta(days=days)
    sql = (
        "SELECT source, title, content, publish_time, importance FROM news_cache "
        "WHERE publish_time >= :since"
    )
    params: dict[str, Any] = {"since": since, "lim": limit}
    if keyword:
        # ILIKE 只有 PG 有; 测试跑 SQLite(它的 LIKE 对 ASCII 本就大小写不敏感)。
        # 写死在 SQL 里会让所有 SQLite 用例挂掉 —— 按方言选操作符。
        like_op = "ILIKE" if _is_postgres(db) else "LIKE"
        sql += f" AND (title {like_op} :kw OR content {like_op} :kw)"
        params["kw"] = f"%{keyword.strip()}%"
    sql += " ORDER BY publish_time DESC LIMIT :lim"
    rows = db.execute(text(sql), params).mappings().all()
    out = [
        {
            "source": r["source"],
            "title": r["title"],
            # SQLite 下 publish_time 回来是 str(PG 是 datetime) —— 两种都要能格式化,
            # 否则测试全绿/SQLite 部署直接 AttributeError。
            "publish_time": _fmt_ts(r["publish_time"]),
            "content": (r["content"] or "")[:200],
            "policy": is_policy(r["title"], r["content"] or ""),
        }
        for r in rows
    ]
    if policy_only:
        out = [r for r in out if r["policy"]]
    return out


def coverage(db: Session) -> dict:
    """本地新闻库覆盖区间 —— 让调用方知道「历史从哪天开始」，不假装有更早的数据。"""
    try:
        row = db.execute(
            text("SELECT COUNT(*), MIN(publish_time), MAX(publish_time) FROM news_cache")
        ).first()
        n, lo, hi = (row[0], row[1], row[2]) if row else (0, None, None)
    except Exception:  # noqa: BLE001
        n, lo, hi = 0, None, None
    return {
        "rows": int(n or 0),
        "from": _fmt_ts(lo)[:10] if lo else None,
        "to": _fmt_ts(hi)[:10] if hi else None,
    }
