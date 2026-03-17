from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import asyncio
import time


def _coerce(value: str) -> Any:
    text = value.strip()
    if not text:
        return text
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


@dataclass(slots=True)
class RateLimitSnapshot:
    captured_at: float
    codex_headers: dict[str, Any]


class RateLimitState:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._snapshot: RateLimitSnapshot | None = None

    async def capture(self, headers: dict[str, str]) -> None:
        codex_headers: dict[str, Any] = {}
        for key, value in headers.items():
            normalized = key.lower()
            if normalized.startswith("x-codex-"):
                codex_headers[normalized] = _coerce(value)
        if not codex_headers:
            return

        snapshot = RateLimitSnapshot(captured_at=time.time(), codex_headers=codex_headers)
        async with self._lock:
            self._snapshot = snapshot

    async def snapshot(self) -> RateLimitSnapshot | None:
        async with self._lock:
            return self._snapshot

    async def usage_payload(self, account_id: str | None, plan_type: str | None) -> dict[str, Any]:
        snapshot = await self.snapshot()
        if snapshot is None:
            return {
                "object": "codex.usage",
                "available": False,
                "account_id": account_id,
                "plan_type": plan_type,
                "message": "No x-codex-* headers captured yet.",
            }

        return {
            "object": "codex.usage",
            "available": True,
            "account_id": account_id,
            "plan_type": plan_type,
            "captured_at": snapshot.captured_at,
            "rate_limits": {
                "primary_used_percent": snapshot.codex_headers.get("x-codex-primary-used-percent"),
                "primary_window_minutes": snapshot.codex_headers.get(
                    "x-codex-primary-window-minutes"
                ),
                "primary_reset_at": snapshot.codex_headers.get("x-codex-primary-reset-at"),
                "secondary_used_percent": snapshot.codex_headers.get(
                    "x-codex-secondary-used-percent"
                ),
                "secondary_window_minutes": snapshot.codex_headers.get(
                    "x-codex-secondary-window-minutes"
                ),
                "secondary_reset_at": snapshot.codex_headers.get("x-codex-secondary-reset-at"),
            },
            "raw_headers": snapshot.codex_headers,
        }

    async def balance_payload(
        self, account_id: str | None, plan_type: str | None
    ) -> dict[str, Any]:
        snapshot = await self.snapshot()
        if snapshot is None:
            return {
                "object": "codex.balance",
                "available": False,
                "account_id": account_id,
                "plan_type": plan_type,
                "billing_mode": "codex_oauth_subscription",
                "message": "No x-codex-* headers captured yet.",
            }

        return {
            "object": "codex.balance",
            "available": True,
            "account_id": account_id,
            "plan_type": plan_type,
            "billing_mode": "codex_oauth_subscription",
            "captured_at": snapshot.captured_at,
            "credits": {
                "has_credits": snapshot.codex_headers.get("x-codex-credits-has-credits"),
                "unlimited": snapshot.codex_headers.get("x-codex-credits-unlimited"),
                "balance": snapshot.codex_headers.get("x-codex-credits-balance"),
            },
            "raw_headers": snapshot.codex_headers,
        }
