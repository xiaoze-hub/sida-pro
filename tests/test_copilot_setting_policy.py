"""Safety policy for the initial copilot settings allowlist."""
import pytest


def test_allowlisted_values_are_normalized():
    from src.harness.setting_policy import validate_setting
    assert validate_setting('notify_retry_attempts', '03') == '3'
    assert validate_setting('notify_retry_backoff_seconds', '2.5') == '2.5'
    assert validate_setting('notify_quiet_hours', '9:05-07:00') == '09:05-07:00'


@pytest.mark.parametrize(('key', 'value'), [
    ('notify_quiet_hours', ''),
    ('notify_quiet_hours', '   '),
    ('notify_retry_attempts', ''),
    ('notify_retry_attempts', ' '),
    ('notify_retry_backoff_seconds', ''),
    ('notify_retry_backoff_seconds', '\t'),
])
def test_blank_value_rejected_not_treated_as_off(key, value):
    """回归钉：空值一律拒收 —— 运行时是 `or env默认`，写空串只会静默回退默认值，
    允许空值等于让用户以为「关掉了」。错误文案必须说清这一点。"""
    from src.harness.setting_policy import BLANK_VALUE_MESSAGE, validate_setting
    with pytest.raises(ValueError) as exc:
        validate_setting(key, value)
    assert str(exc.value) == BLANK_VALUE_MESSAGE
    assert '回退' in str(exc.value)


@pytest.mark.parametrize(('key', 'value'), [
    ('notify_retry_attempts', '4'),
    ('notify_retry_attempts', '-1'),
    ('notify_retry_backoff_seconds', '10.1'),
    ('notify_retry_backoff_seconds', '1e2'),
    ('notify_quiet_hours', '25:00-07:00'),
    ('notify_quiet_hours', '07:00-07:00'),
    ('jwt_secret', 'x'),
    ('http_proxy', 'http://example.com'),
    ('ths_sdk_password', 'x'),
])
def test_unapproved_key_or_invalid_value_rejected(key, value):
    from src.harness.setting_policy import validate_setting
    with pytest.raises(ValueError):
        validate_setting(key, value)
