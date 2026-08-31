import base64
import hashlib
import hmac
import importlib
import json
import sqlite3


def _decode_segment(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_panwatch_client_uses_url_and_database_signed_token(tmp_path, monkeypatch):
    database = tmp_path / "panwatch.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO app_settings(key, value) VALUES (?, ?)",
            (("jwt_secret", "test-secret"), ("auth_token_version", "7")),
        )

    monkeypatch.setenv("PANWATCH_URL", "http://panwatch:8000/")
    monkeypatch.setenv("PANWATCH_DB", str(database))
    for key in (
        "PANWATCH_TOKEN",
        "PANWATCH_JWT_SECRET",
        "PANWATCH_USERNAME",
        "PANWATCH_PASSWORD",
        "AUTH_USERNAME",
        "AUTH_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)

    from forecast_lib import panwatch_client

    importlib.reload(panwatch_client)
    token = panwatch_client.get_token()
    header, payload, signature = token.split(".")

    assert panwatch_client.get_panwatch_url() == "http://panwatch:8000"
    assert _decode_segment(payload)["ver"] == 7
    expected = base64.urlsafe_b64encode(
        hmac.new(b"test-secret", f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    assert hmac.compare_digest(signature, expected)


def test_forecast_db_path_uses_persistent_directory(tmp_path, monkeypatch):
    database = tmp_path / "forecast" / "panwatch_forecast.db"
    monkeypatch.setenv("FORECAST_DB_PATH", str(database))

    from forecast_lib import forecast_paths

    importlib.reload(forecast_paths)
    assert forecast_paths.FORECAST_DB_PATH == str(database)
    assert database.parent.is_dir()
