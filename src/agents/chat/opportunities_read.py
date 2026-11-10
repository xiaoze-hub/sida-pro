"""机会候选只读查询与真实日期说明。"""
from sqlalchemy import func
from src.core.signal_summary import opportunity_header
from src.web.models import EntryCandidate


def read_opportunities(db, limit: int = 10, *, now=None) -> str:
    latest = db.query(func.max(EntryCandidate.snapshot_date)).filter(EntryCandidate.status == 'active').scalar()
    if not latest:
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
