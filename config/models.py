from __future__ import annotations

from pydantic import BaseModel, Field

from core.models.chunk import ChunkingConfig


class LlmConfig(BaseModel):
    provider: str = "openrouter"
    model: str = "openai/gpt-5-mini"
    context_window_tokens: int = Field(default=128_000, gt=0)
    temperature: float = Field(default=0, ge=0)
    max_output_tokens: int = Field(default=24_000, gt=0)
    timeout_seconds: float = Field(default=180.0, gt=0)


class AppConfig(BaseModel):
    llm: LlmConfig = Field(default_factory=LlmConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
