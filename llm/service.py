from __future__ import annotations

from config.models import LlmConfig
from llm.adapters.openrouter import OpenRouterAdapter
from llm.schemas import LlmCompletion


class LlmService:
    def __init__(self, config: LlmConfig) -> None:
        self.config = config
        if config.provider != "openrouter":
            raise ValueError(f"Unsupported LLM provider: {config.provider}")
        self.adapter = OpenRouterAdapter(config)

    def complete_json(self, system_prompt: str, user_prompt: str) -> LlmCompletion:
        return self.adapter.complete_json(system_prompt, user_prompt)
