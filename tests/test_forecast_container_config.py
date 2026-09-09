import importlib


def test_panwatch_client_service_token_no_forge(tmp_path, monkeypatch):
    """0.0: sqlite 直读主库自签 JWT 的通道已整体删除。

    无服务令牌/凭据时 get_token() 返回空串、auth_headers() 为空
    (调用方显式报错, 绝不静默伪造 owner JWT); 服务令牌经环境变量注入,
    请求头为 X-Service-Token。
    """
    monkeypatch.setenv("SIDA_MAIN_API_URL", "http://panwatch:8000/")
    monkeypatch.setenv("PANWATCH_SERVICE_TOKEN", "svc-token-0")
    for key in (
        "PANWATCH_DB",
        "PANWATCH_TOKEN",
        "PANWATCH_USERNAME",
        "PANWATCH_PASSWORD",
        "AUTH_USERNAME",
        "AUTH_PASSWORD",
        "SIDA_SERVICE_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    from forecast_lib import panwatch_client

    importlib.reload(panwatch_client)

    assert panwatch_client.get_panwatch_url() == "http://panwatch:8000"
    assert panwatch_client.get_token() == ""
    assert panwatch_client.auth_headers() == {"X-Service-Token": "svc-token-0"}


def test_panwatch_client_no_credentials_yields_empty_auth(tmp_path, monkeypatch):
    """连服务令牌都没有 → auth_headers() 为空 dict, 调用方必须显式失败。"""
    monkeypatch.setenv("SIDA_MAIN_API_URL", "http://panwatch:8000/")
    for key in (
        "PANWATCH_SERVICE_TOKEN",
        "SIDA_SERVICE_TOKEN",
        "PANWATCH_TOKEN",
        "PANWATCH_USERNAME",
        "PANWATCH_PASSWORD",
        "AUTH_USERNAME",
        "AUTH_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)

    from forecast_lib import panwatch_client

    importlib.reload(panwatch_client)

    assert panwatch_client.auth_headers() == {}


def test_forecast_db_path_uses_persistent_directory(tmp_path, monkeypatch):
    database = tmp_path / "forecast" / "panwatch_forecast.db"
    monkeypatch.setenv("FORECAST_DB_PATH", str(database))

    from forecast_lib import forecast_paths

    importlib.reload(forecast_paths)
    assert forecast_paths.FORECAST_DB_PATH == str(database)
    assert database.parent.is_dir()
