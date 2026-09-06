import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from codex_openai_proxy.api.app import create_app
from codex_openai_proxy.auth.service import AuthNotConfiguredError, AuthService
from codex_openai_proxy.config import get_settings


class FakeAuthService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def get_authorization(self):
        if self.error:
            raise self.error
        return SimpleNamespace(access_token="redacted")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_PROXY_TRANSCRIPTION_ENABLED", "false")
    monkeypatch.delenv("CODEX_PROXY_INBOUND_BEARER_TOKEN", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        test_client.app.state.transcription = object()
        test_client.app.state.transcription_error = None
        yield test_client
    get_settings.cache_clear()


def test_health_reports_valid_upstream_authentication(client: TestClient) -> None:
    client.app.state.auth_service = FakeAuthService()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["authenticated"] is True
    assert response.json()["authentication"] == {"ready": True, "detail": None}


def test_health_reports_revoked_or_expired_authentication(client: TestClient) -> None:
    client.app.state.auth_service = FakeAuthService(RuntimeError("refresh token rejected"))
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["authenticated"] is False
    assert body["authentication"]["ready"] is False
    assert body["authentication"]["detail"] == "Upstream authentication check failed."


def test_health_reports_missing_auth_record(client: TestClient) -> None:
    client.app.state.auth_service = FakeAuthService(AuthNotConfiguredError("No auth record found."))
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["authentication"]["detail"] == "No auth record found."


def test_auto_imports_codex_cli_auth_after_auth_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    source = tmp_path / "codex-auth.json"
    target_dir = tmp_path / "proxy"
    source.write_text("{}")
    target_dir.mkdir()
    monkeypatch.setenv("CODEX_PROXY_DATA_DIR", str(target_dir))
    monkeypatch.setenv("CODEX_PROXY_CODEX_AUTH_FILE", str(source))
    monkeypatch.setenv("CODEX_PROXY_AUTO_IMPORT_CODEX_AUTH", "true")
    get_settings.cache_clear()
    service = AuthService(get_settings())
    imported: list = []
    service.import_from_codex_auth_file = lambda path: imported.append(path)  # type: ignore[method-assign]
    assert asyncio.run(service._try_import_codex_auth_fallback()) is True
    assert imported == [source]
    get_settings.cache_clear()
