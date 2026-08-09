from __future__ import annotations

import os
from pathlib import Path

from openai import OpenAI

from config.models import LlmConfig
from llm.schemas import LlmCompletion


class OpenRouterAdapter:
    def __init__(self, config: LlmConfig) -> None:
        self.config = config

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> LlmCompletion:
        api_key = _openrouter_api_key()
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is required to run live LLM script conversion."
            )

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            timeout=self.config.timeout_seconds,
        )
        effective_max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else self.config.max_output_tokens
        )
        request_kwargs: dict[str, object] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": effective_max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if reasoning_effort is not None:
            request_kwargs["extra_body"] = {
                "reasoning": {"effort": reasoning_effort, "exclude": True}
            }
        response = client.chat.completions.create(**request_kwargs)
        choice = response.choices[0]
        usage = response.usage
        if choice.finish_reason == "length":
            raise RuntimeError(
                "LLM output exceeded max_output_tokens "
                f"(max_output_tokens={effective_max_output_tokens}, "
                f"prompt_tokens={usage.prompt_tokens if usage else None}, "
                f"completion_tokens={usage.completion_tokens if usage else None})"
            )

        content = choice.message.content
        if not content:
            raise RuntimeError(
                "LLM returned an empty response "
                f"(finish_reason={choice.finish_reason}, "
                f"prompt_tokens={usage.prompt_tokens if usage else None}, "
                f"completion_tokens={usage.completion_tokens if usage else None})"
            )
        return LlmCompletion(
            content=content,
            finish_reason=choice.finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
        )


def _openrouter_api_key() -> str | None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        return api_key

    env_path = Path(".env")
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "OPENROUTER_API_KEY":
            continue
        return value.strip().strip("\"'")

    return None
