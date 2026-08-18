import json

import pytest

from epiq.research import OpenAIResearchRunner, OpenAIWorkspaceAgentRunner, _parse_values


class _StreamingResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def __iter__(self):
        result = {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "results": [
                                            {
                                                "entity_id": "co_acme",
                                                "status": "not_found",
                                                "value_json": "null",
                                                "source_url": None,
                                                "source_title": None,
                                                "excerpt": None,
                                                "published_at": None,
                                                "observed_at": None,
                                                "confidence": "low",
                                                "notes": "No supported answer found.",
                                            }
                                        ]
                                    }
                                ),
                            }
                        ],
                    }
                ]
            },
        }
        yield f"data: {json.dumps(result)}\n".encode()
        yield b"data: [DONE]\n"


def test_openai_research_prompt_supports_reference_object_example(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _StreamingResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    runner = OpenAIResearchRunner(api_key="test-key", model="test-model")

    results = runner(
        "Startup",
        {
            "question_id": "q_founders_v1",
            "name": "founders",
            "value_type": "Ref[Founder]",
            "definition": {"cardinality": "many"},
        },
        [{"entity_id": "co_acme", "name": "Acme"}],
    )

    prompt = captured["payload"]["input"]
    assert '[{"name":"Ada Lovelace","birth_year":1815}]' in prompt
    assert captured["timeout"] == 900
    assert results[0]["entity_id"] == "co_acme"
    assert results[0]["status"] == "not_found"


@pytest.mark.parametrize("value_json", ["", "   ", None])
def test_not_found_research_normalizes_empty_value_json(value_json) -> None:
    results = _parse_values(
        [{"entity_id": "theater_savoy", "status": "not_found", "value_json": value_json}]
    )

    assert results[0]["value"] is None


def test_answered_research_rejects_empty_value_json_with_context() -> None:
    with pytest.raises(
        RuntimeError,
        match="theater_savoy has status answered but an empty value_json",
    ):
        _parse_values(
            [{"entity_id": "theater_savoy", "status": "answered", "value_json": ""}]
        )


def test_research_rejects_malformed_value_json_with_context() -> None:
    with pytest.raises(
        RuntimeError,
        match="Research result for theater_savoy has invalid value_json",
    ):
        _parse_values(
            [{"entity_id": "theater_savoy", "status": "answered", "value_json": "not-json"}]
        )


def test_not_found_research_rejects_non_null_value() -> None:
    with pytest.raises(
        RuntimeError,
        match="status not_found but a non-null value_json",
    ):
        _parse_values(
            [{"entity_id": "theater_savoy", "status": "not_found", "value_json": "1891"}]
        )


def test_workspace_planning_does_not_enable_web_search(monkeypatch) -> None:
    captured: dict[str, object] = {}
    plan = {
        "summary": "Start with a compact comparison table.",
        "entity_kinds": ["Platform"],
        "entities": [{"kind": "Platform", "name": "Qualtrics"}],
        "questions": [],
        "research": [],
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            event = {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": json.dumps(plan)}
                            ],
                        }
                    ]
                },
            }
            yield f"data: {json.dumps(event)}\n".encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    runner = OpenAIWorkspaceAgentRunner(api_key="test-key", model="test-model")

    result = runner("Compare survey platforms", {"project": {}, "tables": []})

    assert "tools" not in captured["payload"]
    assert "Do not perform web research during this planning step" in captured["payload"]["input"]
    assert result == plan
