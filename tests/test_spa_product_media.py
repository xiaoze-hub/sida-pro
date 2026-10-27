"""产品媒体必须具有确定的 MIME，缺失文件不得回退到 SPA HTML。"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.bootstrap import app as bootstrap_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    (tmp_path / "index.html").write_text("<main>SPA</main>", encoding="utf-8")
    media = tmp_path / "media" / "product"
    media.mkdir(parents=True)
    for suffix in ("webp", "mp4", "webm", "vtt"):
        (media / f"demo.{suffix}").write_bytes(b"test media")
    monkeypatch.setattr(bootstrap_app, "_static_dir", lambda: str(tmp_path))
    app = FastAPI()
    bootstrap_app._mount_spa(app)
    return TestClient(app)


@pytest.mark.parametrize("suffix,media_type", [
    ("webp", "image/webp"), ("mp4", "video/mp4"),
    ("webm", "video/webm"), ("vtt", "text/vtt"),
])
def test_media_mime_does_not_depend_on_system_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, suffix: str, media_type: str
) -> None:
    # 模拟未安装完整系统 MIME 表的精简容器。
    monkeypatch.setattr("mimetypes.guess_type", lambda *args, **kwargs: (None, None))
    response = client.get(f"/media/product/demo.{suffix}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == media_type
    assert response.content == b"test media"


@pytest.mark.parametrize("suffix", ["webp", "mp4", "webm", "vtt"])
def test_missing_media_returns_404(client: TestClient, suffix: str) -> None:
    response = client.get(f"/media/product/missing.{suffix}")
    assert response.status_code == 404
    assert b"<main>SPA</main>" not in response.content


def test_non_asset_route_keeps_spa_fallback(client: TestClient) -> None:
    assert client.get("/settings").text == "<main>SPA</main>"
    assert client.get("/api/missing").status_code == 404
