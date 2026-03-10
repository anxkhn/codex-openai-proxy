from __future__ import annotations

import os
import sys

from openai import OpenAI


def _required(value: str | None, name: str) -> str:
    if value and value.strip():
        return value.strip()
    raise RuntimeError(f"Missing required value: {name}")


def main() -> int:
    base_url = _required(os.getenv("BASE_URL", "http://127.0.0.1:8787/v1"), "BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY", "placeholder")
    model = os.getenv("MODEL", "gpt-5")

    client = OpenAI(base_url=base_url, api_key=api_key)

    print(f"[python] base_url={base_url} model={model}")

    models = client.models.list()
    model_count = len(models.data) if getattr(models, "data", None) else 0
    if model_count <= 0:
        raise RuntimeError("models.list returned no models")
    print(f"[python] models.list ok ({model_count} models)")

    response = client.responses.create(
        model=model,
        instructions="You are a helpful assistant.",
        input="Reply exactly: python responses non-stream ok",
    )
    response_text = getattr(response, "output_text", "") or ""
    if not isinstance(response_text, str) or not response_text.strip():
        raise RuntimeError("responses.create non-stream returned empty output_text")
    print("[python] responses.create non-stream ok")

    stream = client.responses.create(
        model=model,
        instructions="You are a helpful assistant.",
        input="Reply with five words about streaming",
        stream=True,
    )
    streamed_text_parts: list[str] = []
    stream_events = 0
    for event in stream:
        stream_events += 1
        event_type = getattr(event, "type", "")
        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", "")
            if isinstance(delta, str):
                streamed_text_parts.append(delta)
    if stream_events == 0:
        raise RuntimeError("responses.create stream produced zero events")
    if not "".join(streamed_text_parts).strip():
        raise RuntimeError("responses.create stream produced no text deltas")
    print("[python] responses.create stream ok")

    print(
        "[python] all compatibility checks passed (models + responses non-stream + responses stream)"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[python] FAIL: {exc}", file=sys.stderr)
        raise
