from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import base64
import hashlib
import json
import secrets
import threading
import webbrowser

import httpx


def generate_code_verifier(length: int = 64) -> str:
    random_bytes = secrets.token_bytes(length)
    return base64.urlsafe_b64encode(random_bytes).rstrip(b"=").decode("ascii")


def generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    random_bytes = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(random_bytes).rstrip(b"=").decode("ascii")


def parse_id_token_claims(id_token: str | None) -> dict[str, Any]:
    if not id_token:
        return {}
    parts = id_token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def open_browser(url: str) -> None:
    webbrowser.open(url, new=2)


def build_authorize_url(
    *,
    authorize_url: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    originator: str,
    allowed_workspace_id: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    }
    if allowed_workspace_id:
        params["allowed_workspace_id"] = allowed_workspace_id
    return f"{authorize_url}?{urlencode(params)}"


@dataclass(slots=True)
class OAuthCallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None


class OAuthCallbackServer:
    def __init__(self, host: str, port: int, callback_path: str) -> None:
        self.host = host
        self.port = port
        self.callback_path = callback_path
        self._event = threading.Event()
        self._result: OAuthCallbackResult | None = None
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def redirect_uri(self) -> str:
        if self._server is None:
            raise RuntimeError("Callback server is not started")
        address = self._server.server_address
        bound_host = str(address[0])
        bound_port = int(address[1])
        return f"http://{bound_host}:{bound_port}{self.callback_path}"

    @property
    def bound_port(self) -> int:
        if self._server is None:
            raise RuntimeError("Callback server is not started")
        address = self._server.server_address
        bound_port = int(address[1])
        return int(bound_port)

    def start(self) -> None:
        if self._server is not None:
            return

        outer = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != outer.callback_path:
                    self.send_response(404)
                    self.end_headers()
                    return

                params = parse_qs(parsed.query)
                outer._result = OAuthCallbackResult(
                    code=_single(params.get("code")),
                    state=_single(params.get("state")),
                    error=_single(params.get("error")),
                    error_description=_single(params.get("error_description")),
                )
                outer._event.set()

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h3>Authorization received.</h3>"
                    b"<p>You can close this tab and return to codex-openai-proxy.</p></body></html>"
                )

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = HTTPServer((self.host, self.port), CallbackHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, timeout_seconds: float) -> OAuthCallbackResult:
        if not self._event.wait(timeout_seconds):
            raise TimeoutError("OAuth callback timed out")
        if self._result is None:
            raise RuntimeError("OAuth callback did not produce a result")
        return self._result

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None


def exchange_code_for_tokens(
    *,
    token_url: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(
            token_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("OAuth token endpoint returned invalid JSON")
    return data


async def refresh_tokens(
    *,
    token_url: str,
    client_id: str,
    refresh_token: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            token_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("OAuth refresh endpoint returned invalid JSON")
    return data


def _single(values: list[str] | None) -> str | None:
    if not values:
        return None
    value = values[0]
    return value if value else None
