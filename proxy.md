# OpenAI-compatible proxy

This server exposes the Codex ChatGPT subscription through an OpenAI-compatible API. It uses local Codex OAuth credentials for upstream requests and a separate bearer token to protect every inbound `/v1/*` request. It supports text responses, chat completions, image generation, and image editing without an OpenAI Platform API key.

## Endpoints

Local base URL:

```text
http://127.0.0.1:8787/v1
```

Public base URL:

```text
https://codex-proxy.hrnavi.com/v1
```

Available routes:

- `GET /v1/models`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`
- `POST /v1/images/edits`
- `GET /v1/usage`
- `GET /v1/balance`

## Authentication

Every `/v1/*` request requires the dedicated bearer token configured as `CODEX_PROXY_INBOUND_BEARER_TOKEN` in the proxy process environment. The token is stored locally in `.dev.env` and must not be committed or printed. The local proxy still accepts `Authorization: Bearer placeholder` only when inbound bearer authentication is disabled.

## Test the public API

Load the proxy credential into the shell without printing it:

```bash
set -a
. /home/peter/codex-openai-proxy/.dev.env
set +a
```

List models:

```bash
curl -sS https://codex-proxy.hrnavi.com/v1/models \
  -H "Authorization: Bearer $CODEX_PROXY_INBOUND_BEARER_TOKEN" | jq
```

Chat Completions with Luna:

```bash
curl -sS https://codex-proxy.hrnavi.com/v1/chat/completions \
  -H "Authorization: Bearer $CODEX_PROXY_INBOUND_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.6-luna",
    "messages": [{"role": "user", "content": "Hello in one sentence."}]
  }' | jq
```

Responses API with explicit reasoning effort:

```bash
curl -sS https://codex-proxy.hrnavi.com/v1/responses \
  -H "Authorization: Bearer $CODEX_PROXY_INBOUND_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.6-luna",
    "reasoning": {"effort": "low"},
    "input": "Explain what this proxy does in two sentences."
  }' | jq
```

## Generate an image

The image route accepts the OpenAI-compatible `gpt-image-2` model name. Internally, the proxy invokes Codex's native image-generation capability through the existing ChatGPT/Codex OAuth session.

Generate locally and decode the returned base64 image into a PNG:

```bash
curl -sS http://127.0.0.1:8787/v1/images/generations \
  -H "Authorization: Bearer $CODEX_PROXY_INBOUND_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-image-2",
    "prompt": "A cinematic Japanese street at night, no text",
    "size": "1024x1024",
    "quality": "low",
    "n": 1
  }' | jq -r '.data[0].b64_json' | base64 -d > generated.png
```

Use the public tunnel by replacing the URL with:

```text
https://codex-proxy.hrnavi.com/v1/images/generations
```

Supported request fields are `model`, `prompt`, `size`, `quality`, and `n`. The proxy currently accepts only `model: gpt-image-2`; `n` may be from 1 through 10. Each additional image causes another upstream generation.

## Edit an image

Image editing uses `multipart/form-data`. One request may attach up to 16 input images by repeating the `image` field.

```bash
curl -sS http://127.0.0.1:8787/v1/images/edits \
  -H "Authorization: Bearer $CODEX_PROXY_INBOUND_BEARER_TOKEN" \
  -F 'model=gpt-image-2' \
  -F 'image=@generated.png;type=image/png' \
  -F 'prompt=Change the scene to daytime while preserving the composition' \
  -F 'size=1024x1024' \
  -F 'quality=low' \
  -F 'n=1' | jq -r '.data[0].b64_json' | base64 -d > edited.png
```

Multiple input images:

```bash
curl -sS http://127.0.0.1:8787/v1/images/edits \
  -H "Authorization: Bearer $CODEX_PROXY_INBOUND_BEARER_TOKEN" \
  -F 'model=gpt-image-2' \
  -F 'image=@subject.png;type=image/png' \
  -F 'image=@background.png;type=image/png' \
  -F 'prompt=Place the subject naturally into the supplied background' \
  -F 'quality=high' | jq -r '.data[0].b64_json' | base64 -d > composite.png
```

An optional `mask` file is accepted. The Codex OAuth image transport does not expose the platform Image API's native pixel-mask parameter, so the proxy supplies the mask as an additional image with mask instructions; treat it as prompt-guided masking rather than exact pixel enforcement.

## Image response format

Generation and editing return the OpenAI-compatible base64 shape:

```json
{
  "created": 1788162020,
  "data": [
    {
      "b64_json": "iVBORw0KGgo...",
      "revised_prompt": "Expanded image instruction..."
    }
  ]
}
```

Remove credentials when finished:

```bash
unset CODEX_PROXY_INBOUND_BEARER_TOKEN
```

## OpenAI CLI / SDK configuration

Point an OpenAI-compatible client at the public `/v1` base URL. Use the bearer token as the API key. For local use, the base URL is `http://127.0.0.1:8787/v1`.

For the OpenAI Python SDK:

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url="https://codex-proxy.hrnavi.com/v1",
    api_key=os.environ["CODEX_PROXY_INBOUND_BEARER_TOKEN"],
)

response = client.chat.completions.create(
    model="gpt-5.6-luna",
    messages=[{"role": "user", "content": "Hello."}],
)
print(response.choices[0].message.content)

image = client.images.generate(
    model="gpt-image-2",
    prompt="A minimalist blue circle on white",
    size="1024x1024",
    quality="low",
)
```

The Python SDK returns the base64 image in `image.data[0].b64_json`. Decode it before writing the PNG file.

## Troubleshooting

- `401 Valid bearer authentication is required`: load `.dev.env` and send `CODEX_PROXY_INBOUND_BEARER_TOKEN` as the bearer token.
- `401` or `403` from the upstream service: check the local Codex OAuth session with `uv run codex-openai-proxy whoami` and authenticate again if needed.
- Image response is large: this is expected because `b64_json` is embedded in JSON. Pipe it through `jq -r` and `base64 -d` as shown above.
- Local debugging: first test `http://127.0.0.1:8787`; only then test the public tunnel.
- Logs: inspect `/tmp/codex-openai-proxy.log` and look for the requested route and HTTP status. Image base64 content is not intentionally logged by the application.

## Security notes

- Do not expose port `8787` directly; keep the proxy bound to `127.0.0.1`.
- The proxy rejects unauthenticated `/v1/*` requests before contacting the Codex backend.
- Rotate `CODEX_PROXY_INBOUND_BEARER_TOKEN` if it is exposed, then restart the proxy.
- The bearer token is separate from the proxy’s Codex OAuth credentials.
