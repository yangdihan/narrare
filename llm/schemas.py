from __future__ import annotations

from pydantic import BaseModel


class LlmCompletion(BaseModel):
    content: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
