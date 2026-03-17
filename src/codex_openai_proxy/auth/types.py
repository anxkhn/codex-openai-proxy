from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import time


@dataclass(slots=True)
class Identity:
    email: str | None = None
    account_id: str | None = None
    plan_type: str | None = None
    subject: str | None = None

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> "Identity":
        auth_claims = claims.get("https://api.openai.com/auth")
        auth_map = auth_claims if isinstance(auth_claims, Mapping) else {}

        profile_claims = claims.get("https://api.openai.com/profile")
        profile_map = profile_claims if isinstance(profile_claims, Mapping) else {}

        email = _pick_text(claims, "email") or _pick_text(profile_map, "email")
        account_id = _pick_text(auth_map, "chatgpt_account_id") or _pick_text(claims, "account_id")
        plan_type = _pick_text(auth_map, "chatgpt_plan_type") or _pick_text(claims, "plan")
        subject = _pick_text(claims, "sub")
        return cls(email=email, account_id=account_id, plan_type=plan_type, subject=subject)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Identity":
        return cls(
            email=data.get("email") if isinstance(data.get("email"), str) else None,
            account_id=data.get("account_id") if isinstance(data.get("account_id"), str) else None,
            plan_type=data.get("plan_type") if isinstance(data.get("plan_type"), str) else None,
            subject=data.get("subject") if isinstance(data.get("subject"), str) else None,
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "email": self.email,
            "account_id": self.account_id,
            "plan_type": self.plan_type,
            "subject": self.subject,
        }


@dataclass(slots=True)
class AuthRecord:
    access_token: str
    refresh_token: str
    token_type: str
    expires_at: float
    issued_at: float
    scope: str
    client_id: str
    id_token: str | None = None
    identity: Identity | None = None

    def is_expired(self, skew_seconds: int = 60) -> bool:
        return self.expires_at <= (time.time() + skew_seconds)

    @property
    def expires_in_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AuthRecord":
        identity_raw = data.get("identity")
        identity = Identity.from_dict(identity_raw) if isinstance(identity_raw, Mapping) else None
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            token_type=str(data.get("token_type", "Bearer")),
            expires_at=float(data["expires_at"]),
            issued_at=float(data.get("issued_at", data["expires_at"])),
            scope=str(data.get("scope", "")),
            client_id=str(data.get("client_id", "")),
            id_token=data.get("id_token") if isinstance(data.get("id_token"), str) else None,
            identity=identity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "issued_at": self.issued_at,
            "scope": self.scope,
            "client_id": self.client_id,
            "id_token": self.id_token,
            "identity": self.identity.to_dict() if self.identity else None,
        }


def _pick_text(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None
