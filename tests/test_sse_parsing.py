import json

from codex_openai_proxy.api.app import _image_result_from_sse, _responses_payload_from_sse


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def test_reconstructs_output_when_completed_response_has_no_output() -> None:
    raw = "".join(
        [
            _event({"type": "response.output_text.delta", "delta": "hello "}),
            _event({"type": "response.output_text.delta", "delta": "world"}),
            _event(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_test",
                        "object": "response",
                        "status": "completed",
                        "output": [],
                        "usage": {"output_tokens": 2},
                    },
                }
            ),
        ]
    )

    payload = _responses_payload_from_sse(raw)

    assert payload["id"] == "resp_test"
    assert payload["output_text"] == "hello world"
    assert payload["output"][0]["content"][0]["text"] == "hello world"
    assert payload["usage"] == {"output_tokens": 2}


def test_extracts_image_item_omitted_from_completed_response() -> None:
    raw = "".join(
        [
            _event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": "aW1hZ2U=",
                        "revised_prompt": "A revised prompt",
                    },
                }
            ),
            _event(
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "output": []},
                }
            ),
        ]
    )

    assert _image_result_from_sse(raw) == {
        "b64_json": "aW1hZ2U=",
        "revised_prompt": "A revised prompt",
    }
