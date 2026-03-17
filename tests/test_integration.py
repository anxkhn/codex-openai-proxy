"""
Integration tests for codex-openai-proxy.

Requires the proxy to be running on http://127.0.0.1:8787.
Run: uv run codex-openai-proxy serve --port 8787

Run tests: uv run pytest tests/ -v
"""
import httpx
import pytest

BASE_URL = "http://127.0.0.1:8787"
HEADERS = {"Authorization": "Bearer placeholder", "Content-Type": "application/json"}
MODEL = "gpt-5.4"


def _chat(messages: list[dict], **kwargs) -> dict:
    """POST /v1/chat/completions and return the parsed JSON response."""
    payload = {"model": MODEL, "messages": messages, **kwargs}
    resp = httpx.post(f"{BASE_URL}/v1/chat/completions", json=payload, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _reply(response: dict) -> str:
    """Extract the assistant reply text from a chat completions response."""
    return response["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

class TestSmoke:
    def test_models_endpoint(self):
        resp = httpx.get(f"{BASE_URL}/v1/models", headers=HEADERS, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        model_ids = [m["id"] for m in data["data"]]
        assert MODEL in model_ids

    def test_single_turn_response(self):
        resp = _chat([{"role": "user", "content": "Reply with exactly: hello"}])
        assert resp["object"] == "chat.completion"
        assert len(resp["choices"]) == 1
        assert resp["choices"][0]["message"]["role"] == "assistant"
        content = _reply(resp)
        assert isinstance(content, str) and len(content) > 0

    def test_system_message_is_respected(self):
        resp = _chat([
            {"role": "system", "content": "You are a robot. Always prefix every reply with ROBOT:"},
            {"role": "user", "content": "Say hello"},
        ])
        content = _reply(resp)
        assert "ROBOT:" in content.upper() or "robot" in content.lower()


# ---------------------------------------------------------------------------
# Context / memory retention
# ---------------------------------------------------------------------------

class TestContextRetention:
    def test_remembers_name_in_same_request(self):
        """Model should recall a fact introduced earlier in the same messages array."""
        resp = _chat([
            {"role": "user", "content": "My name is Anas. Remember it."},
            {"role": "assistant", "content": "Got it, your name is Anas."},
            {"role": "user", "content": "What is my name? Reply with just the name."},
        ])
        content = _reply(resp).strip()
        assert "anas" in content.lower(), f"Expected 'Anas' in reply, got: {content!r}"

    def test_multi_turn_arithmetic_context(self):
        """Model should use a number established earlier in the conversation."""
        resp = _chat([
            {"role": "user", "content": "I am thinking of the number 42."},
            {"role": "assistant", "content": "Noted, the number is 42."},
            {"role": "user", "content": "What is that number plus 8? Reply with only the number."},
        ])
        content = _reply(resp).strip()
        assert "50" in content, f"Expected 50 in reply, got: {content!r}"

    def test_context_with_system_and_user_history(self):
        """System instruction should persist alongside user conversation history."""
        resp = _chat([
            {"role": "system", "content": "You are a helpful assistant. Always be concise."},
            {"role": "user", "content": "The codeword is BANANA."},
            {"role": "assistant", "content": "Understood, the codeword is BANANA."},
            {"role": "user", "content": "What is the codeword? One word answer."},
        ])
        content = _reply(resp).strip().upper()
        assert "BANANA" in content, f"Expected BANANA in reply, got: {content!r}"


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------

class TestJsonMode:
    def test_json_mode_returns_valid_json(self):
        """response_format=json_object should cause the model to return parseable JSON."""
        import json
        resp = _chat(
            [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": 'Return {"name": "Anas", "role": "engineer"} exactly.'},
            ],
            response_format={"type": "json_object"},
        )
        content = _reply(resp)
        parsed = json.loads(content)
        assert isinstance(parsed, dict)

    def test_unsupported_params_do_not_error(self):
        """temperature and max_tokens should be accepted by proxy without causing errors."""
        resp = _chat(
            [{"role": "user", "content": "Say hi"}],
            temperature=0.7,
            max_tokens=50,
        )
        assert resp["object"] == "chat.completion"
        assert len(_reply(resp)) > 0


# ---------------------------------------------------------------------------
# Image understanding
# ---------------------------------------------------------------------------

# Valid 32x32 solid red PNG (generated: make_png(32, 32, 255, 0, 0))
# Larger size ensures the model reliably identifies the color.
_RED_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKElEQVR4nO3NsQ0AAAzCMP5/un0CNkuZ41wybXsHAAAAAAAAAAAAxR4yw/wuPL6QkAAAAABJRU5ErkJggg=="


class TestImageUnderstanding:
    def test_base64_image_is_accepted_and_understood(self):
        """Proxy must accept a base64 image and the model must identify its color."""
        data_url = f"data:image/png;base64,{_RED_PNG_B64}"
        resp = _chat([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is the dominant color in this image? One word."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ])
        assert resp["object"] == "chat.completion"
        content = _reply(resp).lower()
        assert "red" in content, f"Expected 'red' in reply, got: {content!r}"

    def test_image_context_retained_in_follow_up(self):
        """After asking about an image, the model should be able to answer a follow-up."""
        data_url = f"data:image/png;base64,{_RED_PNG_B64}"
        resp = _chat([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "I just showed you a tiny image."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
            {
                "role": "assistant",
                "content": "I see a very small image.",
            },
            {
                "role": "user",
                "content": "Did I show you an image in this conversation? Answer yes or no.",
            },
        ])
        content = _reply(resp).lower()
        assert "yes" in content, f"Expected 'yes', got: {content!r}"
