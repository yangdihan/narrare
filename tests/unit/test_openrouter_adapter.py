from types import SimpleNamespace

import pytest

from config.models import LlmConfig
from llm.adapters import openrouter


def test_output_limit_error_reports_stage_specific_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class FakeOpenAI:
        def __init__(self, **_: object) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs: object) -> SimpleNamespace:
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(content="{}"),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=2_400),
            )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(openrouter, "OpenAI", FakeOpenAI)
    adapter = openrouter.OpenRouterAdapter(LlmConfig(max_output_tokens=24_000))

    with pytest.raises(RuntimeError, match="max_output_tokens=2400"):
        adapter.complete_json(
            "system",
            "user",
            max_output_tokens=2_400,
            reasoning_effort="minimal",
        )

    assert requests[0]["max_tokens"] == 2_400
    assert requests[0]["extra_body"] == {
        "reasoning": {"effort": "minimal", "exclude": True}
    }
