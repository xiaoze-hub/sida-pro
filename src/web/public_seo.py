"""公开介绍页的首个 HTML 响应元信息；不读取用户、权限或业务数据库。"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def load_site(static_dir: str) -> dict | None:
    """旧版本静态目录没有清单时保留原 SPA 服务行为。"""
    path = Path(static_dir) / "site-seo.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def site_origin(site: dict) -> str:
    """不使用 Host/转发头构造 canonical，避免不可信请求污染公开地址。"""
    candidate = os.getenv("SIDA_PUBLIC_SITE_URL", "").strip() or site["siteUrl"]
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        parts = urlsplit(site["siteUrl"])
    return f"{parts.scheme}://{parts.netloc.lower()}"


def indexable_host(host: str | None) -> bool:
    return os.getenv("SIDA_SEO_INDEXABLE", "true").lower() not in ("0", "false", "no") and host not in (None, "localhost", "127.0.0.1", "::1", "testserver")


def render_page(source: str, path: str, site: dict, indexable: bool) -> str:
    """元信息对所有 User-Agent 一致；非公开功能页默认 noindex。"""
    page = site["pages"].get(path)
    origin = site_origin(site)
    robots = "index,follow,max-image-preview:large" if page and indexable else "noindex,follow"
    source = re.sub(r"<title>.*?</title>", "", source, flags=re.S | re.I)
    source = re.sub(r'<meta\s+(?:name|property)=[\"\'](?:description|robots|og:[^\"\']+|twitter:[^\"\']+)[\"\'][^>]*>', "", source, flags=re.I)
    tags = [f'<title>{html.escape(page["title"] if page else site["siteName"])}</title>']

    def meta(key: str, value: str, attribute: str = "name") -> None:
        tags.append(f'<meta {attribute}="{key}" content="{html.escape(value, quote=True)}" data-public-seo="true">')

    meta("robots", robots)
    tags.append(f'<meta name="sida-site-url" content="{html.escape(origin, quote=True)}">')
    tags.append(f'<meta name="sida-site-indexable" content="{str(indexable).lower()}">')
    if page:
        canonical = origin + path
        image = origin + site["image"]
        meta("description", page["description"])
        for key, value in {"title": page["title"], "description": page["description"], "url": canonical, "type": "website", "site_name": site["siteName"], "locale": "zh_CN", "image": image, "image:alt": "SIDA Pro 个股研究工作台真实界面", "image:width": "1440", "image:height": "1100"}.items():
            meta(f"og:{key}", value, "property")
        for key, value in {"card": "summary_large_image", "title": page["title"], "description": page["description"], "image": image}.items():
            meta(f"twitter:{key}", value)
        tags.append(f'<link rel="canonical" href="{html.escape(canonical, quote=True)}" data-public-seo="true">')
        graph = [
            {"@type": "WebSite", "@id": origin + "/#website", "name": site["siteName"], "url": origin + "/", "sameAs": site["repository"]},
            {"@type": "WebPage", "@id": canonical + "#webpage", "name": page["title"], "description": page["description"], "url": canonical, "inLanguage": "zh-CN", "isPartOf": {"@id": origin + "/#website"}},
        ]
        if path == "/":
            graph.append({"@type": "WebApplication", "name": site["siteName"], "url": canonical, "applicationCategory": "BusinessApplication", "operatingSystem": "Web", "description": page["description"], "screenshot": image})
        else:
            graph.append({"@type": "BreadcrumbList", "itemListElement": [{"@type": "ListItem", "position": 1, "name": "产品概览", "item": origin + "/"}, {"@type": "ListItem", "position": 2, "name": page["heading"], "item": canonical}]})
        encoded = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("<", "\\u003c")
        tags.append(f'<script id="public-site-schema" type="application/ld+json">{encoded}</script>')
        links = '<nav>' + ' · '.join(f'<a href="{html.escape(url, quote=True)}">{html.escape(row["heading"])}</a>' for url, row in site["pages"].items()) + '</nav>'
        fallback = f'<noscript><main><h1>{html.escape(page["heading"])}</h1><p>{html.escape(page["description"])}</p>{links}<p><a href="{html.escape(site["repository"], quote=True)}">GitHub 项目仓库</a></p><p>启用 JavaScript 后查看完整界面、实时目录与账户方案。技术研究信息不构成投资建议。</p></main></noscript>'
        source = source.replace('<div id="root"></div>', '<div id="root">' + fallback + '</div>')
    return source.replace("</head>", "\n".join(tags) + "\n</head>")


def sitemap(site: dict) -> str:
    origin = site_origin(site)
    entries = "".join(f"<url><loc>{html.escape(origin + path)}</loc></url>" for path in site["pages"])
    return '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries + '</urlset>'
