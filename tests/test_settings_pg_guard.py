import pytest

from tests.test_settings_write_pg_concurrency import _admin_url


@pytest.mark.parametrize('value', [
    'postgresql://localhost.evil.example/postgres',
    'postgresql://localhost@evil.example/postgres',
    'postgresql://127.0.0.1/sida',
    'sqlite:///localhost',
])
def test_settings_pg_fixture_rejects_remote_and_production_targets(monkeypatch, value):
    monkeypatch.setenv('QUOTA_TEST_PG_ADMIN', value)
    with pytest.raises((ValueError, pytest.skip.Exception)):
        _admin_url()
