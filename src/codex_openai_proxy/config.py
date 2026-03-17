from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os
import platform
import re
import subprocess

DEFAULT_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_CODEX_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "api.connectors.read",
    "api.connectors.invoke",
)


def _detect_codex_cli_version() -> str:
    env_value = os.getenv("CODEX_CLI_VERSION")
    if env_value:
        return env_value.strip()

    try:
        result = subprocess.run(
            ["codex", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return "0.0.0"

    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    match = re.search(r"(\d+\.\d+\.\d+)", output)
    if match:
        return match.group(1)
    return "0.0.0"


def _detect_terminal_name() -> str:
    term_program = os.getenv("TERM_PROGRAM")
    term_version = os.getenv("TERM_PROGRAM_VERSION")
    if term_program and term_version:
        return f"{term_program}/{term_version}"
    if term_program:
        return term_program
    return "python-httpx"


def _build_codex_user_agent(originator: str, codex_version: str) -> str:
    os_name = platform.system() or "Unknown"
    os_version = platform.release() or "0"
    arch = platform.machine() or "unknown"
    terminal_name = _detect_terminal_name()
    return f"{originator}/{codex_version} ({os_name} {os_version}; {arch}) {terminal_name}"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    oauth_client_id: str
    oauth_authorize_url: str
    oauth_token_url: str
    oauth_callback_host: str
    oauth_callback_port: int
    oauth_redirect_host: str
    oauth_callback_path: str
    oauth_originator: str
    oauth_scopes: tuple[str, ...]
    auth_file_path: Path
    upstream_base_url: str
    upstream_user_agent: str
    upstream_version: str
    upstream_models_client_version: str
    request_timeout_seconds: float
    auto_default_instructions: bool


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    home = Path.home()
    data_dir = Path(os.getenv("CODEX_PROXY_DATA_DIR", home / ".codex-openai-proxy"))
    auth_file_path = data_dir / "auth.json"

    scopes = os.getenv("CODEX_PROXY_OAUTH_SCOPES")
    oauth_scopes = tuple(scopes.split()) if scopes else DEFAULT_CODEX_SCOPES
    oauth_originator = os.getenv("CODEX_PROXY_ORIGINATOR", "codex_cli_rs")
    codex_version = _detect_codex_cli_version()
    default_user_agent = _build_codex_user_agent(oauth_originator, codex_version)

    return Settings(
        oauth_client_id=os.getenv("CODEX_PROXY_CLIENT_ID", DEFAULT_CODEX_CLIENT_ID),
        oauth_authorize_url=os.getenv(
            "CODEX_PROXY_OAUTH_AUTHORIZE_URL", "https://auth.openai.com/oauth/authorize"
        ),
        oauth_token_url=os.getenv(
            "CODEX_PROXY_OAUTH_TOKEN_URL", "https://auth.openai.com/oauth/token"
        ),
        oauth_callback_host=os.getenv("CODEX_PROXY_CALLBACK_HOST", "127.0.0.1"),
        oauth_callback_port=int(os.getenv("CODEX_PROXY_CALLBACK_PORT", "1455")),
        oauth_redirect_host=os.getenv("CODEX_PROXY_REDIRECT_HOST", "localhost"),
        oauth_callback_path=os.getenv("CODEX_PROXY_CALLBACK_PATH", "/auth/callback"),
        oauth_originator=oauth_originator,
        oauth_scopes=oauth_scopes,
        auth_file_path=auth_file_path,
        upstream_base_url=os.getenv(
            "CODEX_PROXY_UPSTREAM_BASE_URL", "https://chatgpt.com/backend-api/codex"
        ),
        upstream_user_agent=os.getenv("CODEX_PROXY_USER_AGENT", default_user_agent),
        upstream_version=os.getenv("CODEX_PROXY_UPSTREAM_VERSION", codex_version),
        upstream_models_client_version=os.getenv("CODEX_PROXY_MODELS_CLIENT_VERSION", "1.0.0"),
        request_timeout_seconds=float(os.getenv("CODEX_PROXY_REQUEST_TIMEOUT_SECONDS", "45")),
        auto_default_instructions=_env_bool("CODEX_PROXY_AUTO_DEFAULT_INSTRUCTIONS", False),
    )
