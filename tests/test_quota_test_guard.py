import pytest

from tests.test_quota_concurrency_pg import _validate_admin_url


@pytest.mark.parametrize('url', [
    'postgresql://user:password@production.example.com/postgres',
    'postgresql://user:password@localhost/sida',
    'postgresql://user:password@localhost/panwatch',
    'sqlite:///:memory:',
])
def test_destructive_probe_rejects_unsafe_admin_target(url):
    with pytest.raises(ValueError):
        _validate_admin_url(url)


def test_destructive_probe_allows_only_loopback_maintenance_database():
    _validate_admin_url('postgresql://user:password@127.0.0.1/postgres')
