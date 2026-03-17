from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from codex_openai_proxy.auth.service import AuthNotConfiguredError, AuthService
from codex_openai_proxy.config import Settings

from .rate_limits import RateLimitState


@dataclass(slots=True)
class CodexResponse:
    response: httpx.Response
    refreshed_after_auth_error: bool = False


class CodexUpstreamClient:
    def __init__(
        self, settings: Settings, auth_service: AuthService, rate_limits: RateLimitState
    ) -> None:
        self.settings = settings
        self.auth_service = auth_service
        self.rate_limits = rate_limits
        self._client = httpx.AsyncClient(
            base_url=self.settings.upstream_base_url.rstrip("/"),
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        *,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        accept: str,
    ) -> CodexResponse:
        headers = await self._build_headers(accept)
        response = await self._client.request(
            method=method,
            url=path,
            json=json_body,
            params=query_params,
            headers=headers,
        )
        await self.rate_limits.capture(dict(response.headers))

        refreshed = False
        if response.status_code in {401, 403}:
            await response.aread()
            await self.auth_service.refresh_access_token(force=True)
            headers = await self._build_headers(accept)
            response = await self._client.request(
                method=method,
                url=path,
                json=json_body,
                params=query_params,
                headers=headers,
            )
            await self.rate_limits.capture(dict(response.headers))
            refreshed = True

        return CodexResponse(response=response, refreshed_after_auth_error=refreshed)

    async def stream_request(
        self,
        *,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        accept: str,
    ) -> CodexResponse:
        headers = await self._build_headers(accept)
        request = self._client.build_request(
            method=method,
            url=path,
            json=json_body,
            params=query_params,
            headers=headers,
        )
        response = await self._client.send(request, stream=True)
        await self.rate_limits.capture(dict(response.headers))

        refreshed = False
        if response.status_code in {401, 403}:
            await response.aclose()
            await self.auth_service.refresh_access_token(force=True)
            headers = await self._build_headers(accept)
            request = self._client.build_request(
                method=method,
                url=path,
                json=json_body,
                params=query_params,
                headers=headers,
            )
            response = await self._client.send(request, stream=True)
            await self.rate_limits.capture(dict(response.headers))
            refreshed = True

        return CodexResponse(response=response, refreshed_after_auth_error=refreshed)

    async def _build_headers(self, accept: str) -> dict[str, str]:
        try:
            auth = await self.auth_service.get_authorization()
        except AuthNotConfiguredError:
            raise

        headers: dict[str, str] = {
            "Authorization": f"Bearer {auth.access_token}",
            "Accept": accept,
            "User-Agent": self.settings.upstream_user_agent,
            "Version": self.settings.upstream_version,
            "Openai-Beta": "responses=experimental",
        }
        if accept == "text/event-stream":
            headers["Connection"] = "Keep-Alive"
            headers["Content-Type"] = "application/json"
            headers["Originator"] = "codex_cli_rs"
        if auth.account_id:
            headers["ChatGPT-Account-ID"] = auth.account_id
        return headers


async def iter_streaming_body(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()
