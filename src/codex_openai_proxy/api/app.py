from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
import json
import logging

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from codex_openai_proxy.auth.service import AuthNotConfiguredError, AuthService
from codex_openai_proxy.auth.store import AuthStore
from codex_openai_proxy.codex.client import CodexUpstreamClient, iter_streaming_body
from codex_openai_proxy.codex.rate_limits import RateLimitState
from codex_openai_proxy.config import Settings, get_settings

logger = logging.getLogger(__name__)

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
            input_messages.append(
                _from_openai_message(role=role, content=msg.get("content"))
            )

    # If client requested JSON mode, inject instruction since Codex doesn't support response_format
    response_format = body.get("response_format", {})
    wants_json = (
        isinstance(response_format, dict) and response_format.get("type") == "json_object"
    )
    if wants_json:
        json_hint = "\n\nIMPORTANT: You MUST respond with valid JSON only. No markdown, no prose, no code fences. Output raw JSON."
        instructions = (instructions or "You are a helpful assistant.") + json_hint
        logger.warning("response_format=json_object not supported by Codex backend -- injecting JSON instruction into prompt instead")

    _unsupported = ["temperature", "max_tokens", "top_p", "presence_penalty", "frequency_penalty", "seed", "n", "stop", "logprobs", "top_logprobs"]
    for param in _unsupported:
        if param in body:
            logger.warning("%s=%s not supported by Codex backend -- parameter ignored", param, body[param])

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
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "assistant":
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
                url = image_url_val.get("url", "") if isinstance(image_url_val, dict) else image_url_val
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
    for event in reversed(events):
        if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
            return event["response"]

    text_parts: list[str] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)

    output_text = "".join(text_parts)
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
    async def health(request: Request) -> dict[str, Any]:
        auth_service: AuthService = request.app.state.auth_service
        record = auth_service.get_record()
        return {
            "ok": True,
            "authenticated": record is not None,
            "upstream_base_url": request.app.state.settings.upstream_base_url,
            "billing_mode": "codex_oauth_subscription",
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

            chunk = json.dumps({
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": output_text}, "finish_reason": None}],
            })
            done_chunk = json.dumps({
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })

            async def stream_chunks():
                yield f"data: {chunk}\n\n".encode()
                yield f"data: {done_chunk}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(stream_chunks(), media_type="text/event-stream", headers=passthrough)

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
