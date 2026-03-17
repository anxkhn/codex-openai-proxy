from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import asyncio
import json
import time

from codex_openai_proxy.config import Settings

from .oauth import (
    OAuthCallbackServer,
    build_authorize_url,
    exchange_code_for_tokens,
    generate_code_challenge,
    generate_code_verifier,
    generate_state,
    open_browser,
    parse_id_token_claims,
    refresh_tokens,
)
from .store import AuthStore
from .types import AuthRecord, Identity


class AuthNotConfiguredError(RuntimeError):
    pass


@dataclass(slots=True)
class Authorization:
    access_token: str
    account_id: str | None


class AuthService:
    def __init__(self, settings: Settings, store: AuthStore | None = None) -> None:
        self.settings = settings
        self.store = store or AuthStore(settings.auth_file_path)
        self._refresh_lock = asyncio.Lock()

    def login_via_browser(self, timeout_seconds: float = 300.0) -> AuthRecord:
        verifier = generate_code_verifier()
        challenge = generate_code_challenge(verifier)
        state = generate_state()
        scope = " ".join(self.settings.oauth_scopes)

        callback_server = OAuthCallbackServer(
            host=self.settings.oauth_callback_host,
            port=self.settings.oauth_callback_port,
            callback_path=self.settings.oauth_callback_path,
        )
        try:
            callback_server.start()
        except OSError as error:
            raise RuntimeError(
                f"Failed to start callback server on {self.settings.oauth_callback_host}:{self.settings.oauth_callback_port}. "
                "This flow emulates Codex and expects that callback port. "
                "Set CODEX_PROXY_CALLBACK_PORT to choose another port."
            ) from error

        redirect_uri = (
            f"http://{self.settings.oauth_redirect_host}:{callback_server.bound_port}"
            f"{self.settings.oauth_callback_path}"
        )
        try:
            authorize_url = build_authorize_url(
                authorize_url=self.settings.oauth_authorize_url,
                client_id=self.settings.oauth_client_id,
                redirect_uri=redirect_uri,
                scope=scope,
                state=state,
                code_challenge=challenge,
                originator=self.settings.oauth_originator,
            )
            open_browser(authorize_url)

            result = callback_server.wait(timeout_seconds=timeout_seconds)
            if result.error:
                message = result.error_description or result.error
                raise RuntimeError(f"OAuth callback error: {message}")
            if result.state != state:
                raise RuntimeError("OAuth callback state mismatch")
            if not result.code:
                raise RuntimeError("OAuth callback missing authorization code")

            token = exchange_code_for_tokens(
                token_url=self.settings.oauth_token_url,
                client_id=self.settings.oauth_client_id,
                code=result.code,
                redirect_uri=redirect_uri,
                code_verifier=verifier,
                timeout_seconds=self.settings.request_timeout_seconds,
            )
        finally:
            callback_server.stop()

        now = time.time()
        expires_in = int(token.get("expires_in", 3600))
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")

        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("OAuth token response missing access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RuntimeError("OAuth token response missing refresh_token")

        id_token = token.get("id_token") if isinstance(token.get("id_token"), str) else None
        claims = parse_id_token_claims(id_token)
        identity = Identity.from_claims(claims)

        record = AuthRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=str(token.get("token_type", "Bearer")),
            expires_at=now + expires_in,
            issued_at=now,
            scope=str(token.get("scope", "")),
            client_id=self.settings.oauth_client_id,
            id_token=id_token,
            identity=identity,
        )
        self.store.save(record)
        return record

    def import_from_codex_auth_file(self, path: Path) -> AuthRecord:
        if not path.exists():
            raise RuntimeError(f"Codex auth file not found: {path}")

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise RuntimeError("Invalid Codex auth file format")

        tokens = data.get("tokens")
        source = tokens if isinstance(tokens, Mapping) else data

        access_token = _extract_text(source, "access_token", "accessToken", "OPENAI_API_KEY")
        refresh_token = _extract_text(source, "refresh_token", "refreshToken")
        id_token = _extract_text(source, "id_token", "idToken")

        if not access_token:
            raise RuntimeError("Codex auth file does not contain an access token")
        if not refresh_token:
            raise RuntimeError("Codex auth file does not contain a refresh token")

        access_claims = parse_id_token_claims(access_token)
        id_claims = parse_id_token_claims(id_token)

        account_id = _extract_text(source, "account_id", "accountId")
        auth_claims = _auth_claims(access_claims) or _auth_claims(id_claims)
        if not account_id:
            account_id = _extract_text(auth_claims, "chatgpt_account_id")

        exp_value = access_claims.get("exp")
        if isinstance(exp_value, (int, float)):
            expires_at = float(exp_value)
        else:
            expires_at = time.time() + 3600

        identity = Identity.from_claims(id_claims or access_claims)
        if account_id and not identity.account_id:
            identity.account_id = account_id

        record = AuthRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=_extract_text(data, "token_type") or "Bearer",
            expires_at=expires_at,
            issued_at=time.time(),
            scope=_extract_text(data, "scope") or "",
            client_id=_extract_text(data, "client_id") or self.settings.oauth_client_id,
            id_token=id_token,
            identity=identity,
        )
        self.store.save(record)
        return record

    def get_record(self) -> AuthRecord | None:
        return self.store.load()

    async def get_authorization(self) -> Authorization:
        token = await self.ensure_valid_access_token()
        record = self.store.load()
        if record is None:
            raise AuthNotConfiguredError("No auth record found")
        account_id = record.identity.account_id if record.identity else None
        return Authorization(access_token=token, account_id=account_id)

    async def ensure_valid_access_token(self, min_validity_seconds: int = 60) -> str:
        record = self.store.load()
        if record is None:
            raise AuthNotConfiguredError(
                "No auth record found. Run `codex-openai-proxy setup` first."
            )
        if not record.is_expired(skew_seconds=min_validity_seconds):
            return record.access_token

        await self.refresh_access_token(force=False)
        refreshed = self.store.load()
        if refreshed is None:
            raise AuthNotConfiguredError("Auth record missing after refresh")
        return refreshed.access_token

    async def refresh_access_token(self, force: bool) -> None:
        async with self._refresh_lock:
            record = self.store.load()
            if record is None:
                raise AuthNotConfiguredError(
                    "No auth record found. Run `codex-openai-proxy setup` first."
                )
            if not force and not record.is_expired(skew_seconds=60):
                return

            token = await refresh_tokens(
                token_url=self.settings.oauth_token_url,
                client_id=self.settings.oauth_client_id,
                refresh_token=record.refresh_token,
                timeout_seconds=self.settings.request_timeout_seconds,
            )

            now = time.time()
            expires_in = int(token.get("expires_in", 3600))
            new_access_token = token.get("access_token")
            if not isinstance(new_access_token, str) or not new_access_token:
                raise RuntimeError("OAuth refresh response missing access_token")

            new_refresh_token = token.get("refresh_token")
            next_refresh_token = (
                new_refresh_token
                if isinstance(new_refresh_token, str) and new_refresh_token
                else record.refresh_token
            )

            id_token_value = token.get("id_token")
            next_id_token = id_token_value if isinstance(id_token_value, str) else record.id_token

            claims = parse_id_token_claims(next_id_token)
            identity = Identity.from_claims(claims) if claims else record.identity

            updated = AuthRecord(
                access_token=new_access_token,
                refresh_token=next_refresh_token,
                token_type=str(token.get("token_type", record.token_type)),
                expires_at=now + expires_in,
                issued_at=now,
                scope=str(token.get("scope", record.scope)),
                client_id=record.client_id,
                id_token=next_id_token,
                identity=identity,
            )
            self.store.save(updated)


def _extract_text(source: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _auth_claims(claims: Mapping[str, Any]) -> Mapping[str, Any]:
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, Mapping):
        return auth
    return {}
