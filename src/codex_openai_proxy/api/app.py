from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
import asyncio
import base64
import hashlib
import json
import hmac
import logging
import secrets
import shutil
import time

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from codex_openai_proxy.auth.service import AuthNotConfiguredError, AuthService
from codex_openai_proxy.auth.store import AuthStore
from codex_openai_proxy.codex.client import CodexUpstreamClient, iter_streaming_body
from codex_openai_proxy.codex.rate_limits import RateLimitState
from codex_openai_proxy.codex.realtime import CodexRealtimeSession
from codex_openai_proxy.codex.transcription import TranscriptionError, TranscriptionService
from codex_openai_proxy.config import Settings, get_settings

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("uvicorn.error")


def _request_log_record(
    *,
    headers: Mapping[str, str],
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
) -> dict[str, Any]:
    """Build a structured access-log record with Cloudflare correlation IDs."""
    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "method": method,
        "path": path,
        "status_code": status_code,
        "duration_ms": round(duration_ms, 3),
        # Cf-Ray is Cloudflare's request identifier at the origin. AI Gateway
        # identifiers are retained too when Cloudflare forwards them.
        "cf_ray": headers.get("cf-ray"),
        "cf_aig_event_id": headers.get("cf-aig-event-id"),
        "cf_aig_log_id": headers.get("cf-aig-log-id"),
        "x_request_id": headers.get("x-request-id"),
    }


def _openai_error(
    status_code: int,
    message: str,
    *,
    param: str | None = None,
    code: str | None = None,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        },
    )


def _valid_inbound_bearer(authorization: str, expected: str | None) -> bool:
    if not expected:
        return False
    scheme, _, presented = authorization.partition(" ")
    return bool(
        scheme.lower() == "bearer" and presented and hmac.compare_digest(presented, expected)
    )


REALTIME_TOKEN_TTL_SECONDS = 60


class RealtimeTokenStore:
    """Process-local, one-use credentials for the realtime WebSocket only."""

    def __init__(self, *, ttl_seconds: int = REALTIME_TOKEN_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._tokens: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def mint(self) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()
        async with self._lock:
            self._discard_expired(now)
            self._tokens[digest] = now + self.ttl_seconds
        return token, self.ttl_seconds

    async def consume(self, authorization: str) -> bool:
        scheme, _, presented = authorization.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return False
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        now = time.monotonic()
        async with self._lock:
            self._discard_expired(now)
            expires_at = self._tokens.pop(digest, None)
        return expires_at is not None and expires_at > now

    def _discard_expired(self, now: float) -> None:
        expired = [digest for digest, expires_at in self._tokens.items() if expires_at <= now]
        for digest in expired:
            self._tokens.pop(digest, None)


DEFAULT_RESPONSES_BODY: dict[str, Any] = {
    "model": "gpt-5",
    "input": "Return a one-line hello from Codex OAuth proxy.",
    "stream": False,
}

DEFAULT_CHAT_COMPLETIONS_BODY: dict[str, Any] = {
    "model": "gpt-5",
    "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
    "stream": False,
}

DEFAULT_IMAGE_GENERATION_BODY: dict[str, Any] = {
    "model": "gpt-image-2",
    "prompt": "A simple blue circle centered on a white background.",
    "n": 1,
    "size": "1024x1024",
    "quality": "low",
}


def _copy_passthrough_headers(headers: dict[str, str]) -> dict[str, str]:
    passthrough: dict[str, str] = {}
    allowed = {
        "content-type",
        "x-request-id",
        "openai-processing-ms",
        "cache-control",
    }
    for key, value in headers.items():
        normalized = key.lower()
        if normalized in allowed or normalized.startswith("x-codex-"):
            passthrough[key] = value
    return passthrough


def _normalize_models(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        source = payload.get("data", [])
    elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
        source = payload.get("models", [])
    elif isinstance(payload, list):
        source = payload
    else:
        source = []

    models: list[dict[str, Any]] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("slug") or item.get("name")
        if not isinstance(model_id, str):
            continue
        display_name = (
            item.get("display_name") if isinstance(item.get("display_name"), str) else model_id
        )
        models.append(
            {
                "id": model_id,
                "object": "model",
                "created": (
                    int(item.get("created", 0)) if str(item.get("created", "")).isdigit() else 0
                ),
                "owned_by": item.get("owned_by", "openai"),
                "display_name": display_name,
            }
        )

    return {"object": "list", "data": models}


def _json_or_text_error(response_text: str) -> dict[str, Any]:
    return {"error": {"message": response_text or "Upstream request failed"}}


def _adapt_responses_body(
    body: dict[str, Any], *, auto_default_instructions: bool
) -> dict[str, Any]:
    payload = dict(body)
    payload["input"] = _coerce_responses_input(payload.get("input"))
    payload["store"] = False
    payload["stream"] = True

    if not auto_default_instructions:
        return payload

    instructions = payload.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        payload["instructions"] = "You are a helpful assistant. Please respond to the user's query."
    return payload


def _chat_completions_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """Convert Chat Completions request to Responses API format for upstream."""

    messages: list[Any] = body.get("messages", [])
    instructions: str | None = None
    input_messages: list[Any] = []

    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            content = msg.get("content", "")
            instructions = content if isinstance(content, str) else ""
        else:
            input_messages.append(_from_openai_message(role=role, content=msg.get("content")))

    # If client requested JSON mode, inject instruction since Codex doesn't support response_format
    response_format = body.get("response_format", {})
    wants_json = isinstance(response_format, dict) and response_format.get("type") == "json_object"
    if wants_json:
        json_hint = "\n\nIMPORTANT: You MUST respond with valid JSON only. No markdown, no prose, no code fences. Output raw JSON."
        instructions = (instructions or "You are a helpful assistant.") + json_hint
        logger.warning(
            "response_format=json_object not supported by Codex backend -- injecting JSON instruction into prompt instead"
        )

    _unsupported = [
        "temperature",
        "max_tokens",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "n",
        "stop",
        "logprobs",
        "top_logprobs",
    ]
    for param in _unsupported:
        if param in body:
            logger.warning(
                "%s=%s not supported by Codex backend -- parameter ignored", param, body[param]
            )

    payload: dict[str, Any] = {
        "model": body.get("model", "gpt-5"),
        "input": input_messages,
        "store": False,
        "stream": True,  # always stream upstream; proxy de-streams for non-streaming clients
    }
    payload["instructions"] = instructions or "You are a helpful assistant."

    return payload


def _responses_payload_to_chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a Responses API response payload to Chat Completions format."""
    import time

    output_text = ""
    for item in payload.get("output", []):
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "assistant"
        ):
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    output_text += part.get("text", "")

    if not output_text and isinstance(payload.get("output_text"), str):
        output_text = payload["output_text"]

    raw_usage = payload.get("usage", {})
    # Codex uses input_tokens/output_tokens; OpenAI chat.completion uses prompt_tokens/completion_tokens
    prompt_tokens = raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0))
    completion_tokens = raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0))
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": raw_usage.get("total_tokens", prompt_tokens + completion_tokens),
    }

    return {
        "id": payload.get("id", "chatcmpl-proxy"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model", "gpt-5"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": output_text},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": usage,
    }


def _coerce_responses_input(input_value: Any) -> list[Any]:
    if isinstance(input_value, list):
        return input_value

    if isinstance(input_value, str):
        return [_as_user_message(input_value)]

    if isinstance(input_value, dict):
        input_type = input_value.get("type")
        if isinstance(input_type, str):
            return [input_value]

        role = input_value.get("role")
        if isinstance(role, str):
            return [_from_openai_message(role=role, content=input_value.get("content"))]

        return [_as_user_message(json.dumps(input_value))]

    if input_value is None:
        return [_as_user_message("")]

    return [_as_user_message(str(input_value))]


def _from_openai_message(role: str, content: Any) -> dict[str, Any]:
    normalized_role = role.lower().strip()
    codex_role = "assistant" if normalized_role == "assistant" else "user"
    text_type = "output_text" if codex_role == "assistant" else "input_text"

    if isinstance(content, str):
        return {
            "type": "message",
            "role": codex_role,
            "content": [{"type": text_type, "text": content}],
        }

    if isinstance(content, list):
        codex_content: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, str):
                codex_content.append({"type": text_type, "text": item})
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                codex_content.append({"type": text_type, "text": item.get("text", "")})
            elif item_type == "image_url":
                # Convert OpenAI image_url format to Codex input_image format
                image_url_val = item.get("image_url", {})
                url = (
                    image_url_val.get("url", "")
                    if isinstance(image_url_val, dict)
                    else image_url_val
                )
                if isinstance(url, str) and not url.startswith("data:"):
                    logger.warning(
                        "image_url with remote URL '%s...' may not be supported by Codex backend"
                        " -- only base64 data URLs are guaranteed to work (data:image/...;base64,...)",
                        url[:60],
                    )
                codex_content.append({"type": "input_image", "image_url": url})
            else:
                # Unknown part type -- try to preserve text if available
                text_val = item.get("text")
                if isinstance(text_val, str):
                    codex_content.append({"type": text_type, "text": text_val})
        return {"type": "message", "role": codex_role, "content": codex_content}

    return {
        "type": "message",
        "role": codex_role,
        "content": [{"type": text_type, "text": ""}],
    }


def _as_user_message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _parse_sse_events(raw_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _responses_payload_from_sse(raw_text: str) -> dict[str, Any]:
    events = _parse_sse_events(raw_text)
    completed_response: dict[str, Any] | None = None
    for event in reversed(events):
        if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
            completed_response = event["response"]
            break

    text_parts: list[str] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)

    output_text = "".join(text_parts)
    if completed_response is not None:
        if completed_response.get("output") or not output_text:
            return completed_response
        completed_response["output"] = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": output_text}],
            }
        ]
        completed_response["output_text"] = output_text
        return completed_response

    return {
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": output_text}],
            }
        ],
        "output_text": output_text,
    }


def _image_result_from_sse(raw_text: str) -> dict[str, Any] | None:
    """Extract the completed image item, which Codex omits from response.completed."""
    for event in reversed(_parse_sse_events(raw_text)):
        item = event.get("item")
        if (
            event.get("type") == "response.output_item.done"
            and isinstance(item, dict)
            and item.get("type") == "image_generation_call"
            and isinstance(item.get("result"), str)
        ):
            return {
                "b64_json": item["result"],
                "revised_prompt": item.get("revised_prompt"),
            }
    return None


def _image_error_from_sse(raw_text: str) -> dict[str, Any] | None:
    for event in reversed(_parse_sse_events(raw_text)):
        error = event.get("error")
        if isinstance(error, dict):
            return error
        response = event.get("response")
        if isinstance(response, dict) and isinstance(response.get("error"), dict):
            return response["error"]
    return None


def _image_tool(*, size: str | None, quality: str | None) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "image_generation"}
    if size:
        tool["size"] = size
    if quality:
        tool["quality"] = quality
    return tool


async def _run_image_generation(
    request: Request,
    *,
    prompt: str,
    images: list[tuple[bytes, str]] | None = None,
    mask: tuple[bytes, str] | None = None,
    size: str | None = None,
    quality: str | None = None,
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    settings: Settings = request.app.state.settings
    upstream: CodexUpstreamClient = request.app.state.upstream

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for image_bytes, media_type in images or []:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:{media_type};base64,{encoded}"})
    if mask is not None:
        mask_bytes, mask_media_type = mask
        encoded_mask = base64.b64encode(mask_bytes).decode("ascii")
        content[0]["text"] += (
            " The final attached image is the edit mask: change transparent/white regions only "
            "and preserve black regions."
        )
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mask_media_type};base64,{encoded_mask}",
            }
        )

    payload = {
        "model": settings.image_controller_model,
        "input": [{"type": "message", "role": "user", "content": content}],
        "tools": [_image_tool(size=size, quality=quality)],
        "tool_choice": {"type": "image_generation"},
        "store": False,
        "stream": True,
    }
    try:
        result = await upstream.stream_request(
            method="POST",
            path="/responses",
            json_body=payload,
            accept="text/event-stream",
        )
    except AuthNotConfiguredError as exc:
        return None, JSONResponse(status_code=401, content={"error": {"message": str(exc)}})

    response = result.response
    raw_text = (await response.aread()).decode("utf-8", errors="replace")
    await response.aclose()
    passthrough = _copy_passthrough_headers(dict(response.headers))
    if response.status_code >= 400:
        error = _image_error_from_sse(raw_text) or _json_or_text_error(raw_text)["error"]
        return None, JSONResponse(
            status_code=response.status_code, content={"error": error}, headers=passthrough
        )

    image = _image_result_from_sse(raw_text)
    if image is None:
        error = _image_error_from_sse(raw_text)
        return None, JSONResponse(
            status_code=502,
            content={
                "error": error
                or {"message": "Codex completed without an image_generation_call result."}
            },
            headers=passthrough,
        )
    return image, None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    auth_store = AuthStore(settings.auth_file_path)
    auth_service = AuthService(settings=settings, store=auth_store)
    rate_limits = RateLimitState()
    upstream = CodexUpstreamClient(
        settings=settings, auth_service=auth_service, rate_limits=rate_limits
    )

    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.rate_limits = rate_limits
    app.state.upstream = upstream
    app.state.transcription = None
    app.state.transcription_error = None
    app.state.realtime_slots = asyncio.Semaphore(2)
    app.state.realtime_tokens = RealtimeTokenStore()

    ffmpeg_path = shutil.which(settings.ffmpeg_executable)
    if not settings.transcription_enabled:
        app.state.transcription_error = "Transcription is disabled"
    elif ffmpeg_path is None:
        app.state.transcription_error = "FFmpeg executable was not found"
    else:
        app.state.transcription = TranscriptionService(
            upstream,
            ffmpeg_executable=ffmpeg_path,
            transcription_url=settings.transcription_url,
            timeout_seconds=settings.transcription_timeout_seconds,
            max_concurrency=settings.transcription_max_concurrency,
        )

    try:
        yield
    finally:
        await upstream.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Codex OpenAI Proxy",
        description="OpenAI-compatible local proxy using Codex OAuth subscription auth",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def require_inbound_bearer(request: Request, call_next):
        settings: Settings = request.app.state.settings
        token = settings.inbound_bearer_token
        if token and request.url.path.startswith("/v1/"):
            authorization = request.headers.get("authorization", "")
            if not _valid_inbound_bearer(authorization, token):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"message": "Valid bearer authentication is required."}},
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            record = _request_log_record(
                headers=request.headers,
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            access_logger.info("access %s", json.dumps(record, separators=(",", ":")))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> str:
        auth_service: AuthService = request.app.state.auth_service
        record = auth_service.get_record()
        authenticated = record is not None
        account_id = record.identity.account_id if record and record.identity else None
        plan_type = record.identity.plan_type if record and record.identity else None
        host = request.headers.get("host", "localhost:8787")
        base_url = f"http://{host}"
        auth_badge_class = "ok" if authenticated else "warn"
        auth_label = "Authenticated" if authenticated else "Authentication required"
        auth_detail = (
            f"account={account_id or 'unknown'} plan={plan_type or 'unknown'}"
            if authenticated
            else "Run setup from this machine before using /v1 endpoints"
        )
        auth_setup_html = (
            ""
            if authenticated
            else """
        <section class=\"card\">
          <h2>Authenticate now</h2>
          <ol>
            <li>Browser login: <code>uv run codex-openai-proxy setup</code></li>
            <li>Or import existing Codex login: <code>uv run codex-openai-proxy setup-non-interactive</code></li>
            <li>Confirm status: <code>uv run codex-openai-proxy whoami</code></li>
          </ol>
        </section>
        """
        )
        return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Codex OpenAI Proxy</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --card: #ffffff;
      --ink: #0f172a;
      --muted: #475569;
      --line: #e2e8f0;
      --ok: #065f46;
      --ok-bg: #d1fae5;
      --warn: #92400e;
      --warn-bg: #fef3c7;
    }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      min-height: 100vh;
    }}
    .wrap {{ max-width: 760px; margin: 0 auto; padding: 20px 14px; }}
    .hero {{ margin-bottom: 8px; }}
    h1 {{ margin: 0 0 6px; font-size: 1.35rem; }}
    .lead {{ margin: 0; color: var(--muted); }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      margin-top: 12px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
    }}
    h2 {{ margin: 0 0 8px; font-size: 1rem; }}
    ul {{ margin: 0; padding-left: 18px; }}
    ol {{ margin: 0; padding-left: 18px; }}
    li {{ margin: 6px 0; color: var(--muted); }}
    a {{ color: #0f172a; text-decoration: underline; text-underline-offset: 2px; }}
    a:hover {{ text-decoration: underline; }}
    code {{
      background: #f1f5f9;
      color: #111827;
      border-radius: 6px;
      padding: 2px 6px;
      font-family: "IBM Plex Mono", Menlo, monospace;
      font-size: 0.9em;
    }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 0.82rem;
      font-weight: 600;
      margin: 8px 0;
    }}
    .badge.ok {{ background: var(--ok-bg); color: var(--ok); }}
    .badge.warn {{ background: var(--warn-bg); color: var(--warn); }}
    .mono {{ font-family: "IBM Plex Mono", Menlo, monospace; font-size: 0.85rem; color: var(--muted); }}
  </style>
</head>
<body>
  <main class=\"wrap\">
    <section class=\"hero\">
      <h1>Welcome to codex-openai-proxy</h1>
      <p class=\"lead\">OpenAI-compatible endpoints powered by your Codex OAuth session.</p>
      <div class=\"badge {auth_badge_class}\">{auth_label}</div>
      <div class=\"mono\">{auth_detail}</div>
    </section>

    <section class=\"grid\">
      <article class=\"card\">
        <h2>Docs</h2>
        <ul>
          <li><a href=\"/docs\">Swagger UI: /docs</a></li>
          <li><a href=\"/redoc\">ReDoc: /redoc</a></li>
          <li>Health: <code>/health</code></li>
        </ul>
      </article>
      <article class=\"card\">
        <h2>Core API</h2>
        <ul>
          <li><code>GET /v1/models</code></li>
          <li><code>POST /v1/responses</code></li>
          <li><code>POST /v1/chat/completions</code></li>
          <li><code>POST /v1/images/generations</code></li>
          <li><code>POST /v1/images/edits</code></li>
          <li><code>GET /v1/usage</code> and <code>/v1/balance</code></li>
        </ul>
      </article>
    </section>

    <section class=\"card\">
      <h2>Quick test</h2>
      <code>curl -sS {base_url}/v1/models -H "Authorization: Bearer placeholder"</code>
    </section>
    {auth_setup_html}
  </main>
</body>
</html>
"""

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        auth_service: AuthService = request.app.state.auth_service
        authentication_error: str | None = None
        try:
            # A stored OAuth record is not sufficient: access and refresh tokens
            # can be expired or revoked.  Use the same validation path as real
            # upstream requests so health reports readiness accurately.
            await auth_service.get_authorization()
        except AuthNotConfiguredError as exc:
            authentication_error = str(exc)
        except Exception:
            logger.exception("Upstream authentication health check failed")
            authentication_error = "Upstream authentication check failed."

        authenticated = authentication_error is None
        transcription_ready = request.app.state.transcription is not None
        payload = {
            "ok": authenticated and transcription_ready,
            "authenticated": authenticated,
            "authentication": {"ready": authenticated, "detail": authentication_error},
            "upstream_base_url": request.app.state.settings.upstream_base_url,
            "billing_mode": "codex_oauth_subscription",
            "transcription": {
                "ready": transcription_ready,
                "experimental": True,
                "detail": request.app.state.transcription_error,
            },
        }
        return JSONResponse(status_code=200 if payload["ok"] else 503, content=payload)

    @app.websocket("/v1/realtime/codex")
    async def codex_realtime(websocket: WebSocket) -> None:
        """Restricted bridge to Codex app-server's experimental v3 WebRTC Realtime API."""
        settings: Settings = websocket.app.state.settings
        authorization = websocket.headers.get("authorization", "")
        permanent_bearer = _valid_inbound_bearer(authorization, settings.inbound_bearer_token)
        ephemeral_bearer = False
        if not permanent_bearer:
            ephemeral_bearer = await websocket.app.state.realtime_tokens.consume(authorization)
        if not permanent_bearer and not ephemeral_bearer:
            await websocket.close(code=1008, reason="Valid bearer authentication is required.")
            return

        slots = websocket.app.state.realtime_slots
        if slots.locked():
            await websocket.close(code=1013, reason="Realtime capacity is currently full.")
            return
        async with slots:
            session = CodexRealtimeSession(websocket, cwd=str(Path.cwd()))
            await session.run()

    @app.post("/v1/realtime/token")
    async def mint_realtime_token(request: Request) -> dict[str, Any]:
        """Mint a one-use bearer accepted only by the realtime WebSocket."""
        token, expires_in = await request.app.state.realtime_tokens.mint()
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": expires_in,
        }

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/v1/models")
    async def list_models(
        request: Request,
        client_version: str | None = Query(
            default="1.0.0",
            description="Client version forwarded to Codex model listing.",
        ),
    ):
        upstream: CodexUpstreamClient = request.app.state.upstream
        settings: Settings = request.app.state.settings

        query_params = dict(request.query_params)
        if "client_version" not in query_params and client_version:
            query_params["client_version"] = client_version
        if "client_version" not in query_params:
            query_params["client_version"] = settings.upstream_models_client_version

        try:
            result = await upstream.request(
                method="GET",
                path="/models",
                query_params=query_params,
                accept="application/json",
            )
        except AuthNotConfiguredError as exc:
            return JSONResponse(status_code=401, content={"error": {"message": str(exc)}})

        response = result.response
        passthrough = _copy_passthrough_headers(dict(response.headers))

        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = _json_or_text_error(response.text)
            return JSONResponse(
                status_code=response.status_code, content=payload, headers=passthrough
            )

        try:
            payload = response.json()
        except ValueError:
            return JSONResponse(
                status_code=502,
                content={"error": {"message": "Upstream model response was not JSON"}},
            )
        normalized = _normalize_models(payload)
        return JSONResponse(status_code=200, content=normalized, headers=passthrough)

    @app.post("/v1/audio/transcriptions")
    async def audio_transcriptions(
        request: Request,
        file: UploadFile = File(...),
        model: str = Form(...),
        language: str | None = Form(default=None),
        prompt: str | None = Form(default=None),
        response_format: str = Form(default="json"),
        temperature: float = Form(default=0.0),
    ):
        if model != "whisper-1":
            return _openai_error(
                400,
                "Only the compatibility model 'whisper-1' is supported.",
                param="model",
                code="model_not_found",
            )
        if response_format not in {"json", "text"}:
            return _openai_error(
                400,
                "response_format must be 'json' or 'text'; timestamped formats are not supported.",
                param="response_format",
                code="unsupported_response_format",
            )
        if temperature < 0 or temperature > 1:
            return _openai_error(
                400,
                "temperature must be between 0 and 1.",
                param="temperature",
                code="invalid_value",
            )
        service: TranscriptionService | None = request.app.state.transcription
        if service is None:
            return _openai_error(
                503,
                request.app.state.transcription_error or "Transcription is unavailable.",
                code="transcription_unavailable",
                error_type="server_error",
            )

        limit = request.app.state.settings.transcription_max_upload_bytes
        chunks: list[bytes] = []
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                return _openai_error(
                    413,
                    f"Audio file exceeds the {limit}-byte upload limit.",
                    param="file",
                    code="file_too_large",
                )
            chunks.append(chunk)
        if size == 0:
            return _openai_error(400, "Audio file is empty.", param="file", code="invalid_file")

        try:
            text = await service.transcribe(
                b"".join(chunks), language=language or None, prompt=prompt or None
            )
        except TranscriptionError as exc:
            logger.warning("Transcription failed: %s", exc)
            message = str(exc)
            invalid_audio = message.startswith(("Invalid or unsupported", "Audio contains"))
            status = exc.upstream_status or (400 if invalid_audio else 502)
            client_error = 400 <= status < 500
            return _openai_error(
                status,
                message,
                param="file" if invalid_audio else None,
                code="invalid_audio" if invalid_audio else "transcription_failed",
                error_type="invalid_request_error" if client_error else "server_error",
            )

        if response_format == "text":
            return Response(content=text, media_type="text/plain; charset=utf-8")
        return JSONResponse(content={"text": text})

    @app.post("/v1/images/generations")
    async def image_generations(
        request: Request,
        body: dict[str, Any] = Body(default=DEFAULT_IMAGE_GENERATION_BODY),
    ):
        model = body.get("model", "gpt-image-2")
        if model != "gpt-image-2":
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "Only gpt-image-2 is supported."}},
            )
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "prompt must be a non-empty string."}},
            )
        n = body.get("n", 1)
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 10:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "n must be an integer from 1 to 10."}},
            )

        data: list[dict[str, Any]] = []
        for _ in range(n):
            image, error_response = await _run_image_generation(
                request,
                prompt=prompt,
                size=body.get("size"),
                quality=body.get("quality"),
            )
            if error_response is not None:
                return error_response
            assert image is not None
            data.append(image)
        return JSONResponse(status_code=200, content={"created": int(time.time()), "data": data})

    @app.post("/v1/images/edits")
    async def image_edits(
        request: Request,
        image: list[UploadFile] = File(...),
        prompt: str = Form(...),
        model: str = Form("gpt-image-2"),
        mask: UploadFile | None = File(default=None),
        n: int = Form(1),
        size: str | None = Form(default=None),
        quality: str | None = Form(default=None),
    ):
        if model != "gpt-image-2":
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "Only gpt-image-2 is supported."}},
            )
        if not prompt.strip():
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "prompt must be non-empty."}},
            )
        if not 1 <= n <= 10:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "n must be an integer from 1 to 10."}},
            )
        if not 1 <= len(image) <= 16:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "Provide from 1 to 16 input images."}},
            )

        image_inputs: list[tuple[bytes, str]] = []
        for upload in image:
            image_inputs.append((await upload.read(), upload.content_type or "image/png"))
        mask_input = (
            (await mask.read(), mask.content_type or "image/png") if mask is not None else None
        )

        data: list[dict[str, Any]] = []
        for _ in range(n):
            generated, error_response = await _run_image_generation(
                request,
                prompt=prompt,
                images=image_inputs,
                mask=mask_input,
                size=size,
                quality=quality,
            )
            if error_response is not None:
                return error_response
            assert generated is not None
            data.append(generated)
        return JSONResponse(status_code=200, content={"created": int(time.time()), "data": data})

    @app.post("/v1/responses")
    async def responses_proxy(
        request: Request,
        body: dict[str, Any] = Body(
            default=DEFAULT_RESPONSES_BODY,
            description="OpenAI Responses API payload.",
            openapi_examples={
                "basic": {
                    "summary": "Basic non-streaming request",
                    "value": DEFAULT_RESPONSES_BODY,
                },
                "streaming": {
                    "summary": "Streaming request",
                    "value": {
                        "model": "gpt-5",
                        "instructions": "You are a helpful assistant.",
                        "input": "Count from 1 to 5.",
                        "stream": True,
                    },
                },
            },
        ),
    ):
        settings: Settings = request.app.state.settings
        client_stream = body.get("stream") is True
        return await _proxy_generation(
            request,
            "/responses",
            _adapt_responses_body(
                body,
                auto_default_instructions=settings.auto_default_instructions,
            ),
            client_stream=client_stream,
        )

    @app.post("/v1/chat/completions")
    async def chat_completions_proxy(
        request: Request,
        body: dict[str, Any] = Body(
            default=DEFAULT_CHAT_COMPLETIONS_BODY,
            description="OpenAI Chat Completions payload.",
            openapi_examples={
                "basic": {
                    "summary": "Basic non-streaming request",
                    "value": DEFAULT_CHAT_COMPLETIONS_BODY,
                },
                "streaming": {
                    "summary": "Streaming request",
                    "value": {
                        "model": "gpt-5",
                        "messages": [
                            {
                                "role": "user",
                                "content": "Write three short tips for debugging.",
                            }
                        ],
                        "stream": True,
                    },
                },
            },
        ),
    ):
        # Codex upstream only supports /responses, not /chat/completions.
        # Convert to Responses API format, forward upstream, convert response back.
        client_wants_stream = body.get("stream") is True
        responses_body = _chat_completions_to_responses(body)
        upstream: CodexUpstreamClient = request.app.state.upstream
        query_params = dict(request.query_params)

        try:
            result = await upstream.stream_request(
                method="POST",
                path="/responses",
                json_body=responses_body,
                query_params=query_params,
                accept="text/event-stream",
            )
        except AuthNotConfiguredError as exc:
            return JSONResponse(status_code=401, content={"error": {"message": str(exc)}})

        response = result.response
        passthrough = _copy_passthrough_headers(dict(response.headers))
        raw_text = (await response.aread()).decode("utf-8", errors="replace")
        await response.aclose()

        if response.status_code >= 400:
            events = _parse_sse_events(raw_text)
            for event in reversed(events):
                error_payload = event.get("error")
                if isinstance(error_payload, dict):
                    return JSONResponse(
                        status_code=response.status_code,
                        content={"error": error_payload},
                        headers=passthrough,
                    )
            try:
                payload = json.loads(raw_text)
            except ValueError:
                payload = _json_or_text_error(raw_text)
            return JSONResponse(
                status_code=response.status_code, content=payload, headers=passthrough
            )

        responses_payload = _responses_payload_from_sse(raw_text)

        if client_wants_stream:
            # Re-emit as chat completions SSE stream
            import time

            chat_id = responses_payload.get("id", "chatcmpl-proxy")
            model = responses_payload.get("model", body.get("model", "gpt-5"))
            output_text = ""
            for item in responses_payload.get("output", []):
                if isinstance(item, dict) and item.get("role") == "assistant":
                    for part in item.get("content", []):
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            output_text += part.get("text", "")
            if not output_text and isinstance(responses_payload.get("output_text"), str):
                output_text = responses_payload["output_text"]

            chunk = json.dumps(
                {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": output_text},
                            "finish_reason": None,
                        }
                    ],
                }
            )
            done_chunk = json.dumps(
                {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )

            async def stream_chunks():
                yield f"data: {chunk}\n\n".encode()
                yield f"data: {done_chunk}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                stream_chunks(), media_type="text/event-stream", headers=passthrough
            )

        chat_payload = _responses_payload_to_chat_completions(responses_payload)
        return JSONResponse(status_code=200, content=chat_payload, headers=passthrough)

    async def _proxy_generation(
        request: Request,
        upstream_path: str,
        body: dict[str, Any],
        *,
        client_stream: bool | None = None,
    ):
        upstream: CodexUpstreamClient = request.app.state.upstream

        upstream_stream = body.get("stream") is True
        wants_stream = upstream_stream if client_stream is None else client_stream
        query_params = dict(request.query_params)

        try:
            if upstream_stream:
                result = await upstream.stream_request(
                    method="POST",
                    path=upstream_path,
                    json_body=body,
                    query_params=query_params,
                    accept="text/event-stream",
                )
                response = result.response
                passthrough = _copy_passthrough_headers(dict(response.headers))
                if not wants_stream:
                    raw_text = (await response.aread()).decode("utf-8", errors="replace")
                    await response.aclose()
                    if response.status_code >= 400:
                        events = _parse_sse_events(raw_text)
                        for event in reversed(events):
                            error_payload = event.get("error")
                            if isinstance(error_payload, dict):
                                return JSONResponse(
                                    status_code=response.status_code,
                                    content={"error": error_payload},
                                    headers=passthrough,
                                )
                        return JSONResponse(
                            status_code=response.status_code,
                            content=_json_or_text_error(raw_text),
                            headers=passthrough,
                        )
                    payload = _responses_payload_from_sse(raw_text)
                    return JSONResponse(
                        status_code=response.status_code,
                        content=payload,
                        headers=passthrough,
                    )

                media_type = response.headers.get("content-type", "text/event-stream")
                return StreamingResponse(
                    iter_streaming_body(response),
                    status_code=response.status_code,
                    media_type=media_type,
                    headers=passthrough,
                )

            result = await upstream.request(
                method="POST",
                path=upstream_path,
                json_body=body,
                query_params=query_params,
                accept="application/json",
            )
        except AuthNotConfiguredError as exc:
            return JSONResponse(status_code=401, content={"error": {"message": str(exc)}})

        response = result.response
        passthrough = _copy_passthrough_headers(dict(response.headers))
        try:
            payload = response.json()
        except ValueError:
            payload = _json_or_text_error(response.text)
        return JSONResponse(status_code=response.status_code, content=payload, headers=passthrough)

    @app.get("/v1/usage")
    async def usage(request: Request):
        auth_service: AuthService = request.app.state.auth_service
        rate_limits: RateLimitState = request.app.state.rate_limits
        record = auth_service.get_record()
        account_id = record.identity.account_id if record and record.identity else None
        plan_type = record.identity.plan_type if record and record.identity else None
        return JSONResponse(
            content=await rate_limits.usage_payload(account_id=account_id, plan_type=plan_type)
        )

    @app.get("/v1/balance")
    async def balance(request: Request):
        auth_service: AuthService = request.app.state.auth_service
        rate_limits: RateLimitState = request.app.state.rate_limits
        record = auth_service.get_record()
        account_id = record.identity.account_id if record and record.identity else None
        plan_type = record.identity.plan_type if record and record.identity else None
        return JSONResponse(
            content=await rate_limits.balance_payload(account_id=account_id, plan_type=plan_type)
        )

    return app


app = create_app()
