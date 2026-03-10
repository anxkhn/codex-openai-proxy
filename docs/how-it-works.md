# How It Works

This guide explains the proxy design in language-neutral terms so you can reimplement it in Go, Rust, Node.js, Java, or any other stack.

## Goal

Expose OpenAI-compatible endpoints while authenticating upstream requests with Codex ChatGPT OAuth credentials, not platform API key billing.

## Minimum Endpoint Surface

Implement these routes first:

1. `GET /health`
2. `GET /v1/models`
3. `POST /v1/responses`
4. `POST /v1/chat/completions`
5. `GET /v1/usage`
6. `GET /v1/balance`

Optional but useful:

- `GET /` for a status page.
- `/docs` and `/redoc` if your framework can generate OpenAPI docs.

## Core Design Principles

- Keep inbound API compatibility, but use your own auth backend.
- Treat OAuth refresh tokens as high-sensitivity secrets.
- Preserve streaming behavior for `stream=true` without buffering.
- Retry once on upstream auth failures after a forced refresh.
- Normalize response shapes where needed, but avoid destructive transformations.

## Auth Flow Design

### Interactive Browser OAuth (PKCE)

1. Start a local callback listener.
2. Generate `state`, PKCE verifier/challenge.
3. Open browser to authorize URL.
4. Capture callback `code` and verify `state`.
5. Exchange code at token endpoint.
6. Persist `access_token`, `refresh_token`, and expiry metadata.

### Non-Interactive Import

1. Read existing Codex auth JSON from local machine.
2. Extract token fields and identity metadata.
3. Normalize to your proxy's internal auth schema.
4. Save to your secure auth store.

### Refresh Strategy

- Before each upstream call, ensure access token validity with skew (for example, 60 seconds).
- If expired or near expiry, refresh using refresh token grant.
- Use a lock to avoid concurrent refresh races.
- If upstream responds `401/403`, force-refresh once and retry request once.

## Request Mapping Rules

### `/v1/models`

- Upstream request: `GET /backend-api/codex/models`.
- Forward query params (including `client_version`).
- Normalize output into OpenAI-style `{"object":"list","data":[...]}` if needed.

### `/v1/responses`

- Upstream request: `POST /backend-api/codex/responses`.
- Pass through JSON payload.
- If `stream=true`, return event-stream directly.
- If non-streaming, return JSON body.

### `/v1/chat/completions`

- Upstream request: `POST /backend-api/codex/chat/completions`.
- Same streaming/non-streaming handling as responses route.

### `/v1/usage` and `/v1/balance`

- Build from the latest captured `x-codex-*` headers.
- Return stable JSON schema for dashboards/tools.
- Include account metadata when available.

## Upstream Header Contract

For each upstream request, send at least:

- `Authorization: Bearer <access_token>`
- `Accept: application/json` or `text/event-stream`
- `ChatGPT-Account-ID: <account_id>` when available
- `Openai-Beta: responses=experimental`
- `User-Agent` and `Version` values that follow Codex conventions

For SSE requests, also include connection-oriented headers expected by your upstream provider.

## State Management Model

Maintain two independent state stores:

1. **Auth store**: refreshable OAuth credential record.
2. **Runtime usage snapshot**: latest parsed rate/credit headers.

Keep them decoupled so usage telemetry failure does not break generation endpoints.

## Suggested Internal Interfaces

Use equivalent interfaces in your language:

```text
AuthStore:
  load() -> AuthRecord?
  save(AuthRecord) -> void
  delete() -> void

AuthService:
  loginViaBrowser() -> AuthRecord
  importFromCodexAuth(filePath) -> AuthRecord
  ensureValidAccessToken() -> string
  getAuthorization() -> { accessToken, accountId }

UpstreamClient:
  request(method, path, body?, query?) -> Response
  streamRequest(method, path, body?, query?) -> StreamResponse

RateLimitState:
  capture(headers) -> void
  usagePayload(accountId, planType) -> object
  balancePayload(accountId, planType) -> object
```

## Portability Checklist

When rebuilding in another language, ensure these are true:

- OAuth callback server can securely receive one-time code.
- PKCE generation follows RFC rules.
- Token refresh is atomic and race-safe.
- Streaming is chunk-forwarded, not line-reconstructed incorrectly.
- JSON passthrough preserves upstream fields not explicitly transformed.
- Errors are consistent and machine-readable.

## Testing Strategy

Build automated tests for:

1. OAuth state mismatch and callback error handling.
2. Refresh-before-expiry logic.
3. Retry-on-401/403 exactly once behavior.
4. Streaming passthrough correctness.
5. Model list normalization.
6. Usage header parsing robustness.

## Common Pitfalls

- Using empty POST body in docs without examples.
- Dropping account scoping header, causing model/usage mismatch.
- Refreshing tokens concurrently without lock protection.
- Buffering SSE responses and breaking real-time client behavior.
- Exposing auth file in logs or repos.

## Build Your Own Variant

To create your own implementation:

1. Pick your framework and HTTP client.
2. Implement auth subsystem first.
3. Implement one generation endpoint and make it pass through.
4. Add streaming mode.
5. Add models and usage endpoints.
6. Add docs and examples.
7. Harden auth storage and network exposure.

If you follow this order, you can ship a working first version quickly and evolve safely.
