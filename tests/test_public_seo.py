"""公开页首响应、规范地址和索引边界；无业务数据库和网络依赖。"""
from __future__ import annotations

import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.bootstrap import app as bootstrap_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    root = Path(bootstrap_app.__file__).resolve().parents[2]
    config = root / 'frontend/public/site-seo.json'
    if not config.exists():
        config = root / 'static/site-seo.json'
    (tmp_path / 'site-seo.json').write_text(config.read_text(), encoding='utf-8')
    (tmp_path / 'index.html').write_text('<html><head><title>old</title><meta name="description" content="old"><meta name="robots" content="noindex,follow"></head><body><div id="root"></div></body></html>', encoding='utf-8')
    monkeypatch.setattr(bootstrap_app, '_static_dir', lambda: str(tmp_path))
    monkeypatch.setenv('SIDA_PUBLIC_SITE_URL', 'https://public.example')
    monkeypatch.setenv('SIDA_SEO_INDEXABLE', 'true')
    app = FastAPI()
    bootstrap_app._mount_spa(app)
    return TestClient(app, base_url='https://public.example')


@pytest.mark.parametrize('path,heading', [('/', 'AI 股票技术分析'), ('/developers', '开发者文档'), ('/tiers', '账户方案')])
def test_public_first_response_has_metadata_and_noscript(client: TestClient, path: str, heading: str) -> None:
    response = client.get(path + '?utm_source=test&section=sec-auth')
    assert response.status_code == 200
    assert heading in response.text
    assert '<link rel="canonical" href="https://public.example' + path + '"' in response.text
    assert response.text.count('rel="canonical"') == 1
    assert response.text.count('name="description"') == 1
    assert response.text.count('name="robots"') == 1
    assert 'property="og:image"' in response.text
    assert 'name="twitter:card"' in response.text
    assert '<noscript><main>' in response.text
    assert response.headers['x-robots-tag'] == 'index,follow'
    schema = json.loads(re.search(r'<script id="public-site-schema" type="application/ld\+json">(.*?)</script>', response.text)[1])
    assert schema['@context'] == 'https://schema.org'
    assert 'offers' not in json.dumps(schema)
    assert 'aggregateRating' not in json.dumps(schema)
    assert schema['@graph'][1]['url'] == 'https://public.example' + path


@pytest.mark.parametrize('path', ['/login', '/settings', '/stock/CN/000001'])
def test_private_workspace_paths_noindex(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.headers['x-robots-tag'] == 'noindex,follow'
    assert 'rel="canonical"' not in response.text
    assert 'public-site-schema' not in response.text


def test_canonical_ignores_untrusted_host_and_forwarded_headers(client: TestClient) -> None:
    response = client.get('/developers', headers={'host': 'poison.invalid', 'x-forwarded-host': 'poison.invalid', 'x-forwarded-proto': 'http'})
    assert 'https://public.example/developers' in response.text
    assert 'poison.invalid' not in response.text


def test_local_and_private_deployments_noindex(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get('/', headers={'host': 'localhost:8000'}).headers['x-robots-tag'] == 'noindex,follow'
    monkeypatch.setenv('SIDA_SEO_INDEXABLE', 'false')
    assert client.get('/').headers['x-robots-tag'] == 'noindex,follow'
    assert 'Disallow: /\n' in client.get('/robots.txt').text


def test_sitemap_and_robots_only_advertise_public_pages(client: TestClient) -> None:
    response = client.get('/sitemap.xml')
    assert response.headers['content-type'].startswith('application/xml')
    urls = [item.text for item in ET.fromstring(response.text).iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
    assert urls == ['https://public.example/', 'https://public.example/developers', 'https://public.example/tiers']
    robots = client.get('/robots.txt').text
    assert 'Disallow: /api/' in robots
    assert 'Sitemap: https://public.example/sitemap.xml' in robots


def test_invalid_site_override_does_not_inject_javascript(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SIDA_PUBLIC_SITE_URL', 'javascript:alert(1)')
    response = client.get('/')
    assert 'javascript:alert' not in response.text
    assert 'https://sida.win/' in response.text
