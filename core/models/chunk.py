from pydantic import BaseModel, Field, model_validator

from core.models.source import SourceSpan


class ChunkingConfig(BaseModel):
    provider: str = "openrouter"
    model: str = "openai/gpt-5-mini"
    context_window_tokens: int = Field(default=128_000, gt=0)
    target_chunk_tokens: int = Field(default=3_000, gt=0)
    min_chunk_chars: int = Field(default=750, gt=0)
    target_chunk_chars: int = Field(default=1_500, gt=0)
    max_chunk_chars: int = Field(default=2_000, gt=0)
    overlap_tokens: int = Field(default=500, ge=0)
    reserved_prompt_tokens: int = Field(default=12_000, ge=0)
    reserved_output_tokens: int = Field(default=12_000, ge=0)
    reserved_registry_tokens: int = Field(default=8_000, ge=0)
    contingency_ratio: float = Field(default=0.25, ge=0.0, lt=1.0)

    @model_validator(mode="after")
    def ensure_budget_fits(self) -> "ChunkingConfig":
        if not self.min_chunk_chars <= self.target_chunk_chars <= self.max_chunk_chars:
            raise ValueError(
                "chunk character bounds must satisfy "
                "min_chunk_chars <= target_chunk_chars <= max_chunk_chars"
            )
        usable_window = int(self.context_window_tokens * (1 - self.contingency_ratio))
        chunk_budget_tokens = max(self.target_chunk_tokens, self.max_chunk_chars)
        required = (
            chunk_budget_tokens
            + (2 * self.overlap_tokens)
            + self.reserved_prompt_tokens
            + self.reserved_output_tokens
            + self.reserved_registry_tokens
        )
        if required > usable_window:
            raise ValueError(
                "chunking budget exceeds usable context window: "
                f"required={required}, usable={usable_window}"
            )
        return self


class TextChunk(BaseModel):
    chunk_id: str
    index: int = Field(ge=0)
    source_span: SourceSpan
    text: str
    estimated_tokens: int = Field(ge=0)
    previous_context_span: SourceSpan
    previous_context: str
    previous_context_estimated_tokens: int = Field(ge=0)
    next_context_span: SourceSpan
    next_context: str
    next_context_estimated_tokens: int = Field(ge=0)


class ChunksArtifact(BaseModel):
    project_id: str
    source_sha256: str
    chunking_config: ChunkingConfig
    chunks: list[TextChunk]
