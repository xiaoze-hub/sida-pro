"""Safety policy for the initial copilot settings allowlist."""
import pytest


def test_allowlisted_values_are_normalized():
    from src.harness.setting_policy import validate_setting
    assert validate_setting('notify_retry_attempts', '03') == '3'
    assert validate_setting('notify_retry_backoff_seconds', '2.5') == '2.5'
    assert validate_setting('notify_quiet_hours', '9:05-07:00') == '09:05-07:00'
    assert validate_setting('notify_quiet_hours', '') == ''


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
