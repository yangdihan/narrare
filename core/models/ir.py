from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from core.models.source import SourceSpan


class RawScriptSegment(BaseModel):
    script: dict[str, str]
    confidence: float = Field(ge=0.0, le=1.0)
    review_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_single_script_entry(self) -> "RawScriptSegment":
        if len(self.script) != 1:
            raise ValueError("script must contain exactly one speaker key")
        return self

    @property
    def speaker(self) -> str:
        return next(iter(self.script))

    @property
    def text(self) -> str:
        return next(iter(self.script.values()))


class ScriptSegment(BaseModel):
    segment_id: str
    source_span: SourceSpan
    script: dict[str, str]
    raw_script_key: str | None = None
    speaker_key_normalization: dict[str, object] | None = None
    speaker_key_review: dict[str, object] | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    review_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_single_script_entry(self) -> "ScriptSegment":
        if len(self.script) != 1:
            raise ValueError("script must contain exactly one speaker key")
        return self

    @property
    def speaker(self) -> str:
        return next(iter(self.script))

    @property
    def text(self) -> str:
        return next(iter(self.script.values()))


class ScriptArtifact(BaseModel):
    project_id: str
    chunk_id: str
    chunk_source_path: str
    chunk_sha256: str
    llm_provider: str
    llm_model: str
    response_source: Literal["llm", "response_path", "assembled", "speaker_key_review"]
    processed_chunk_count: int = Field(ge=0)
    segments: list[ScriptSegment]


class ScriptConverterResponse(BaseModel):
    segments: list[RawScriptSegment]


class ScriptValidationReport(BaseModel):
    project_id: str
    chunk_id: str
    exact_reconstruction_success: bool
    segment_count: int
    source_character_count: int
    reconstructed_character_count: int
    source_hash: str
    reconstructed_hash: str
    errors: list[str] = Field(default_factory=list)


class SpeakerKeyReviewResponse(BaseModel):
    segment_id: str
    current_key: str
    decision: Literal["keep", "replace", "uncertain"]
    replacement_key: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_replace_has_replacement(self) -> "SpeakerKeyReviewResponse":
        if self.decision == "replace" and not self.replacement_key:
            raise ValueError("replacement_key is required when decision=replace")
        return self
