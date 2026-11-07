"""Value validation for a narrow, non-secret copilot settings allowlist.

This module does not itself authorize, persist, or execute settings changes.

2026-11-07 复审阻断项（空值语义）:
  运行时的解析口径是 `bootstrap/runtime._build_notifier`:

      quiet_hours = _get_app_setting("notify_quiet_hours") or settings.notify_quiet_hours

  空串是 falsy ⇒ 写空串**不会**关闭静默期，而是静默回退到环境变量默认值。所以
  「空 = 关闭」是谎话，必须拒绝空值：用户想关，就得让默认值本身为空；想开，就得
  给出明确取值。validate_setting 因此对空/纯空白一律抛 ValueError，并且错误文案
  要把真相说明白（会回退默认值），不留「非法值」这种含糊话。
"""
from __future__ import annotations

import re
from decimal import Decimal

ALLOWED_SETTINGS = frozenset({
    'notify_retry_attempts', 'notify_retry_backoff_seconds', 'notify_quiet_hours',
})
_TIME_RANGE = re.compile(r'([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)')

#: 空值被拒时的统一说明 —— 不许出现「空=关闭」这种与运行时相反的说法。
BLANK_VALUE_MESSAGE = (
    '设置值不可为空：空值不会被当成「关闭」，运行时会回退到环境变量默认值；'
    '请给出明确取值'
)


def validate_setting(key: str, value: str) -> str:
    """Validate and normalize; raises ValueError for out-of-scope keys or values."""
    if key not in ALLOWED_SETTINGS or not isinstance(value, str):
        raise ValueError('设置项不在允许范围或值不是字符串')
    val = value.strip()
    if not val:
        # 空值 ≠ 关闭：运行时回落 env 默认值，见模块 docstring。
        raise ValueError(BLANK_VALUE_MESSAGE)
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


def setting_value_state(present: bool, value: str | None) -> str:
    """三态标签：absent（无行）/ null（有行但值为空）/ value（有明确值）。

    只用于**如实展示**旧值，绝不用来表示「关闭」—— 空/NULL 在运行时都会回退到
    环境变量默认值。
    """
    if not present:
        return 'absent'
    return 'null' if value is None or value == '' else 'value'
