# codex-openai-proxy

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](#install)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.135.1%2B-009688?logo=fastapi&logoColor=white)](#what-this-is)
[![uv](https://img.shields.io/badge/uv-managed-6C47FF)](#install)

**Tags:** `openai-compatible` `codex-oauth` `subscription-auth` `fastapi` `localhost` `hackable`

`codex-openai-proxy` is an extremely minimal OpenAI-compatible local server that uses **Codex ChatGPT subscription OAuth**.

It is built for people who want to point OpenAI-compatible clients to localhost and use their Codex subscription auth flow, without platform API key billing.

## What this is

- Local OpenAI-compatible proxy for common endpoints:
  - `GET /`
  - `GET /health`
  - `GET /v1/models`
  - `POST /v1/responses`
  - `POST /v1/chat/completions`
- Browser-based OAuth login using `auth.openai.com` with PKCE.
- Local token storage with refresh support.
- Usage and balance-style visibility from Codex rate-limit headers:
  - `GET /v1/usage`
  - `GET /v1/balance`

## What this is not

- Not an OpenAI platform API-key billing proxy.
- Not designed for public internet exposure.
- Not multi-tenant.

By default this server binds to localhost and ignores inbound bearer tokens. Client apps can still send placeholder API keys for SDK compatibility.

## Architecture

```text
OpenAI SDK / CLI / app
         |
         |  base_url=http://127.0.0.1:8787/v1
         v
codex-openai-proxy (FastAPI)
  - /v1/models
  - /v1/responses
  - /v1/chat/completions
  - /v1/usage
  - /v1/balance
         |
         |  Authorization: Bearer <Codex OAuth access token>
         |  ChatGPT-Account-ID: <workspace/account>
         v
https://chatgpt.com/backend-api/codex
```

## Why this works

Codex CLI supports ChatGPT subscription auth and stores refreshable OAuth credentials locally. This project follows that same approach and translates it into an OpenAI-compatible local server surface.

## Install

Requirements:

- Python 3.11+
- `uv`

From this folder:

```bash
uv sync
```

If you want pip-style compatibility too:

```bash
uv pip install -r requirements.txt
```

## Interactive setup and login

Run browser OAuth setup once:

```bash
uv run codex-openai-proxy setup
```

This will:

1. Start a localhost callback server on port `1455`.
2. Open your browser to complete OAuth login.
3. Store tokens at `~/.codex-openai-proxy/auth.json`.

The OAuth URL intentionally emulates Codex CLI conventions:

- redirect URI shape: `http://localhost:1455/auth/callback`
- scopes: `openid profile email offline_access api.connectors.read api.connectors.invoke`
- query flags: `id_token_add_organizations=true`, `codex_cli_simplified_flow=true`, `originator=codex_cli_rs`

Check current login:

```bash
uv run codex-openai-proxy whoami
```

Log out:

```bash
uv run codex-openai-proxy logout
```

## Non-interactive setup

If you already have Codex CLI logged in on the same machine, you can import existing auth directly without opening a browser.

```bash
uv run codex-openai-proxy setup-non-interactive
```

By default this imports from `~/.codex/auth.json` and writes a normalized auth record to `~/.codex-openai-proxy/auth.json`.

Use a custom source path:

```bash
uv run codex-openai-proxy setup-non-interactive --codex-auth-file /path/to/auth.json
```

## Run the server

```bash
uv run codex-openai-proxy serve --port 8787
```

By default `serve` binds with an empty host (`""`), which maps to all available network interfaces.

To use from other devices on your LAN, call it via your machine IP (for example `192.168.1.20`):

```bash
curl http://192.168.1.20:8787/
```

Health check:

```bash
curl http://127.0.0.1:8787/health
```

API docs pages:

```bash
open http://127.0.0.1:8787/docs
open http://127.0.0.1:8787/redoc
```

## OpenAI Python SDK example

Both `chat.completions` and `responses` work:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="placeholder-not-used",
)

# Chat Completions (most compatible with existing apps)
resp = client.chat.completions.create(
    model="gpt-5.4",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Write a tiny Python function that reverses a string."},
    ],
)
print(resp.choices[0].message.content)

# Responses API (native Codex format)
resp = client.responses.create(
    model="gpt-5",
    instructions="You are a helpful assistant.",
    input="Write a tiny Python function that reverses a string.",
)
print(resp.output_text)
```

## curl examples

Set base URL:

```bash
BASE_URL="http://127.0.0.1:8787"
```

List models:

```bash
curl -sS "$BASE_URL/v1/models" -H "Authorization: Bearer placeholder"
```

Responses API:

```bash
curl -sS "$BASE_URL/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer placeholder" \
  -d '{
    "model": "gpt-5",
    "instructions": "You are a helpful assistant.",
    "input": "Explain PKCE in 3 bullet points"
  }'
```

Chat Completions API:

```bash
curl -sS "$BASE_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer placeholder" \
  -d '{
    "model": "gpt-5",
    "messages": [
      {"role": "user", "content": "Give me a regex for UUID v4"}
    ]
  }'
```

Usage:

```bash
curl -sS "$BASE_URL/v1/usage"
```

Balance:

```bash
curl -sS "$BASE_URL/v1/balance"
```

## Configuration

Environment variables:

- `CODEX_PROXY_CLIENT_ID` (default baked in)
- `CODEX_PROXY_OAUTH_AUTHORIZE_URL` (default `https://auth.openai.com/oauth/authorize`)
- `CODEX_PROXY_OAUTH_TOKEN_URL` (default `https://auth.openai.com/oauth/token`)
- `CODEX_PROXY_CALLBACK_HOST` (default `127.0.0.1`)
- `CODEX_PROXY_CALLBACK_PORT` (default `1455`)
- `CODEX_PROXY_REDIRECT_HOST` (default `localhost`)
- `CODEX_PROXY_CALLBACK_PATH` (default `/auth/callback`)
- `CODEX_PROXY_ORIGINATOR` (default `codex_cli_rs`)
- `CODEX_PROXY_DATA_DIR` (default `~/.codex-openai-proxy`)
- `CODEX_PROXY_UPSTREAM_BASE_URL` (default `https://chatgpt.com/backend-api/codex`)
- `CODEX_PROXY_MODELS_CLIENT_VERSION` (default `1.0.0`)
- `CODEX_PROXY_UPSTREAM_VERSION` (default detected from local `codex --version`, fallback `0.0.0`)
- `CODEX_PROXY_USER_AGENT` (default Codex-like `codex_cli_rs/<version> (...)` string)
- `CODEX_PROXY_AUTO_DEFAULT_INSTRUCTIONS` (default `false`; if enabled, injects fallback `instructions` for `/v1/responses` when absent)

## Chat Completions compatibility

`POST /v1/chat/completions` is fully compatible with the OpenAI Python and JavaScript SDKs.

The Codex upstream only exposes `POST /v1/responses`. The proxy converts transparently:

- `messages` array -- system role becomes `instructions`, the rest become `input`
- Response converted back to `chat.completion` format with `choices`, `usage` (`prompt_tokens`/`completion_tokens`/`total_tokens`)
- Streaming supported (`stream: true`) -- re-emitted as `chat.completion.chunk` SSE
- Multi-turn context works -- include the full message history in each request as normal

**Unsupported parameters** (`temperature`, `max_tokens`, `top_p`, `presence_penalty`, `frequency_penalty`, `seed`, `n`, `stop`) are accepted by the proxy but not forwarded to the upstream. A warning is logged per request:

```
temperature=0.7 not supported by Codex backend -- parameter ignored
```

**`response_format={"type":"json_object"}`** is handled by injecting a JSON instruction into the system prompt instead of passing the parameter upstream:

```
response_format=json_object not supported by Codex backend -- injecting JSON instruction into prompt instead
```

## Vision / image support

`image_url` content parts with base64 data URLs work end-to-end:

```python
import base64

with open("image.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

resp = client.chat.completions.create(
    model="gpt-5.4",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "What color is dominant in this image?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }],
)
```

Only base64 data URLs are supported. Remote URLs are passed through as-is (support depends on Codex upstream).

## Known limitations

These limitations stem from the Codex upstream (`https://chatgpt.com/backend-api/codex`), not the proxy itself.

### Unsupported parameters (accepted but ignored)

The following OpenAI parameters are accepted by the proxy for SDK compatibility but are **not forwarded** to Codex. A warning is logged for each:

| Parameter | Notes |
|-----------|-------|
| `temperature` | Codex controls sampling internally |
| `max_tokens` | No token cap exposed by upstream |
| `top_p` | Not supported |
| `presence_penalty` | Not supported |
| `frequency_penalty` | Not supported |
| `seed` | Not supported |
| `n` | Always returns 1 completion |
| `stop` | Not supported |
| `logprobs` / `top_logprobs` | Not supported |

### `response_format`

`response_format={"type":"json_object"}` is handled by injecting a JSON instruction into the system prompt. The upstream does not support a native JSON mode.

### Images: base64 only

`image_url` content parts **must** use base64 data URLs (`data:image/...;base64,...`). Remote HTTP/HTTPS URLs are passed through as-is, but Codex upstream support for remote URLs is not guaranteed. The proxy logs a warning when a non-base64 URL is detected.

### No fine-tuning or embeddings

`POST /v1/embeddings` and fine-tuning endpoints are not implemented. The proxy targets chat/generation use cases only.

### Single-tenant / localhost only

This proxy holds a single OAuth session and is designed for local personal use. It is not multi-tenant and should not be exposed to the public internet.

## OpenAI compliance note on `instructions`

`instructions` is a valid field on the OpenAI **Responses API** (`POST /v1/responses`).

- It is not a `chat/completions` field.
- This proxy auto-converts `input` values (string/object) into the list format required by Codex upstream.
- For Codex upstream compatibility, the proxy enforces `store=false` on `/v1/responses` and uses upstream streaming internally.
- If client `stream` is `false`, the proxy aggregates upstream SSE and returns JSON.
- Strict pass-through for missing `instructions` is the default.
- If you want a fallback instruction automatically added, set:

```bash
export CODEX_PROXY_AUTO_DEFAULT_INSTRUCTIONS=true
```

## Testing

Integration tests run against a live proxy instance:

```bash
# Start the proxy first
uv run codex-openai-proxy serve --port 8787

# Run tests (requires authenticated proxy)
uv run pytest tests/ -v
```

Tests cover: models endpoint, single-turn responses, system messages, multi-turn context retention, JSON mode, unsupported parameter handling, base64 image understanding, and image context in follow-up messages.

## File layout

```text
codex-openai-proxy/
  pyproject.toml
  requirements.txt
  README.md
  src/codex_openai_proxy/
    cli.py
    config.py
    api/app.py
    auth/
      oauth.py
      service.py
      store.py
      types.py
    codex/
      client.py
      rate_limits.py
  tests/
    test_integration.py
```

## Security notes

- Prefer localhost unless you intentionally need LAN access.
- This server binds to all local interfaces by default. Use network controls or reverse-proxy auth if needed.
- `auth.json` contains refreshable credentials. Treat it like a password.
- Do not commit `~/.codex-openai-proxy/auth.json` anywhere.
- If you suspect compromise, run `logout` and perform `setup` again.

## Troubleshooting OAuth `unknown_error`

If browser setup opens `https://auth.openai.com/error?...unknown_error...`, the most common causes are OAuth URL mismatch and stale callback assumptions.

Quick checks:

1. Use the Codex-like defaults:
   - `CODEX_PROXY_CALLBACK_HOST=127.0.0.1`
   - `CODEX_PROXY_REDIRECT_HOST=localhost`
   - `CODEX_PROXY_CALLBACK_PORT=1455`
   - `CODEX_PROXY_CALLBACK_PATH=/auth/callback`
2. Retry with non-interactive import if Codex CLI login already works:
   - `uv run codex-openai-proxy setup-non-interactive`
3. If port 1455 is occupied, choose a new one and retry:
   - `CODEX_PROXY_CALLBACK_PORT=1555 uv run codex-openai-proxy setup`

## Hackability notes

This project is intentionally small and straightforward:

- Change upstream headers in `src/codex_openai_proxy/codex/client.py`.
- Change model normalization in `src/codex_openai_proxy/api/app.py`.
- Extend usage parsing in `src/codex_openai_proxy/codex/rate_limits.py`.
- Add more OpenAI-compatible endpoints in `src/codex_openai_proxy/api/app.py`.

## Dev commands

Format:

```bash
uv run black src
```

Smoke run:

```bash
uv run codex-openai-proxy --help
```

## Official SDK smoke apps

Compatibility smoke scripts live in `examples/openai-sdk-smoke`.

Python official SDK:

```bash
BASE_URL="http://127.0.0.1:8787/v1" uv run --with openai python examples/openai-sdk-smoke/python_smoke.py
```

JavaScript official SDK:

```bash
npm --prefix examples/openai-sdk-smoke install
BASE_URL="http://127.0.0.1:8787/v1" npm --prefix examples/openai-sdk-smoke run smoke:js
```
