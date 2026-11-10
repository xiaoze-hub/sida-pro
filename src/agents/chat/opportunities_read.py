"""机会候选只读查询与真实日期说明。

2026-11-10 时效审计：
  · 未来日期的快照（库内时区错位/预写脏数据）**不参与**「最新快照」选择 —— 否则一条
    2026-12-01 的脏行会把真实候选全挤掉，还会被当成「最新」；
  · header 由 `opportunity_header` 按真实基准日 + 盘口时段标注（今日已收盘 / 今日盘中 /
    前日 / 最近交易日 / 历史 / 未知），盘中快照不再自称「今日候选可用于今日结论」。
"""
from sqlalchemy import func
from src.core.signal_summary import now_cn, opportunity_header
from src.web.models import EntryCandidate


def read_opportunities(db, limit: int = 10, *, now=None) -> str:
    today = now_cn(now).date().isoformat()
    latest = db.query(func.max(EntryCandidate.snapshot_date)).filter(
        EntryCandidate.status == 'active', EntryCandidate.snapshot_date <= today
    ).scalar()
    if not latest:
        # 「没有候选」和「只有未来日期的脏行」是两件事，分开说（2026-11-10 时效审计）
        future = db.query(func.count(EntryCandidate.id)).filter(
            EntryCandidate.status == 'active', EntryCandidate.snapshot_date > today
        ).scalar()
        if future:
            return (
                f'暂无机会候选（库内 {future} 条 active 候选的快照日期晚于今天 {today}，'
                '属未来日期/时区错位的脏数据，已排除，不得当作今日候选）。'
            )
        return '暂无机会候选（库内没有 active 候选）。'
    head = opportunity_header(latest, now=now)
    total = db.query(func.count(EntryCandidate.id)).filter(
        EntryCandidate.status == 'active', EntryCandidate.snapshot_date == latest
    ).scalar()
    rows = (db.query(EntryCandidate).filter(
        EntryCandidate.status == 'active', EntryCandidate.snapshot_date == latest,
        EntryCandidate.signal.isnot(None), EntryCandidate.signal != '',
    ).order_by(EntryCandidate.score.desc()).limit(max(1, min(int(limit), 50))).all())
    if not rows:
        return f'{head} 共{total}条active，暂无带信号的机会候选。'
    lines = [f'{head} 共{total}条active，按得分Top{len(rows)}:']
    for c in rows:
        target = c.target_price
        target_str = f'{target:.2f}' if isinstance(target, (int, float)) else '—'
        lines.append(f'- {c.stock_symbol} {c.stock_name} 得分{c.score:g} 操作:{c.action_label} '
                     f'信号:{c.signal} 目标价:{target_str}')
    return '\n'.join(lines)
