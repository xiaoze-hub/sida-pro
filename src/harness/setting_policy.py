"""Value validation for a narrow, non-secret copilot settings allowlist.

This module does not itself authorize, persist, or execute settings changes.
"""
from __future__ import annotations

import re
from decimal import Decimal

ALLOWED_SETTINGS = frozenset({
    'notify_retry_attempts', 'notify_retry_backoff_seconds', 'notify_quiet_hours',
})
_TIME_RANGE = re.compile(r'([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)')


def validate_setting(key: str, value: str) -> str:
    """Validate and normalize; raises ValueError for out-of-scope keys or values."""
    if key not in ALLOWED_SETTINGS or not isinstance(value, str):
        raise ValueError('设置项不在允许范围或值不是字符串')
    val = value.strip()
    if not val:
        return ''
    if key == 'notify_retry_attempts':
        if not re.fullmatch(r'[0-9]{1,2}', val) or int(val) > 3:
            raise ValueError('重试次数须为 0 至 3 的整数')
        return str(int(val))
    if key == 'notify_retry_backoff_seconds':
        if not re.fullmatch(r'[0-9]{1,2}(?:\.[0-9])?', val) or Decimal(val) > 10:
            raise ValueError('退避秒数须为 0 至 10，最多一位小数')
        return str(Decimal(val))
    match = _TIME_RANGE.fullmatch(val)
    if not match:
        raise ValueError('静默时间格式须为 HH:MM-HH:MM')
    h1, m1, h2, m2 = (int(x) for x in match.groups())
    if (h1, m1) == (h2, m2):
        raise ValueError('静默时间起止不可相同')
    return f'{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}'
