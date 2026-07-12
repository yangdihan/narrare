from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


ReferenceType = Literal[
    "stable_name",
    "honorific",
    "role_title",
    "relationship_title",
    "pronoun",
    "descriptive_phrase",
]


class AliasEvidence(BaseModel):
    alias: str
    reference_type: ReferenceType = "stable_name"
    evidence_text: str
    source: str = "current_chunk"
    confidence: float = Field(ge=0.0, le=1.0)
    review_notes: list[str] = Field(default_factory=list)


class CharacterRegistryUpdate(BaseModel):
    character_id: str | None = None
    proposed_character_id: str | None = None
    canonical_name: str
    stable_aliases: list[str] = Field(default_factory=list)
    contextual_references: list[AliasEvidence] = Field(default_factory=list)
    # Deprecated compatibility field. New Stage 1 output should use stable_aliases.
    aliases: list[str] = Field(default_factory=list)
    alias_evidence: list[AliasEvidence] = Field(default_factory=list)
    persona_summary: str | None = None
    speaking_style: str | None = None
    age_impression: str | None = None
    voice_variant_notes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    review_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_identity_hint(self) -> "CharacterRegistryUpdate":
        if not self.character_id and not self.proposed_character_id:
            raise ValueError("character_id or proposed_character_id is required")
        return self


class CharacterRecord(BaseModel):
    character_id: str
    canonical_name: str
    stable_aliases: list[str] = Field(default_factory=list)
    contextual_references: list[AliasEvidence] = Field(default_factory=list)
    # Deprecated compatibility field. Do not use for automatic normalization.
    aliases: list[str] = Field(default_factory=list)
    alias_evidence: list[AliasEvidence] = Field(default_factory=list)
    persona_summary: str | None = None
    speaking_style: str | None = None
    age_impression: str | None = None
    voice_variant_notes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    review_notes: list[str] = Field(default_factory=list)


class CharacterRegistryArtifact(BaseModel):
    project_id: str
    characters: list[CharacterRecord] = Field(default_factory=list)


class AliasObservation(BaseModel):
    text: str
    reference_type: ReferenceType = "stable_name"
    likely_character_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    review_notes: list[str] = Field(default_factory=list)


class UnresolvedPronoun(BaseModel):
    text: str
    candidates: list[str] = Field(default_factory=list)
    review_note: str | None = None


class ChunkContextProfile(BaseModel):
    scene_summary: str
    active_characters: list[str] = Field(default_factory=list)
    aliases_observed: list[AliasObservation] = Field(default_factory=list)
    current_emotional_state: dict[str, str] = Field(default_factory=dict)
    unresolved_pronouns: list[UnresolvedPronoun] = Field(default_factory=list)
    important_context: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    review_notes: list[str] = Field(default_factory=list)


class ChunkContextProfilerResponse(BaseModel):
    context: ChunkContextProfile
    character_registry_updates: list[CharacterRegistryUpdate] = Field(
        default_factory=list
    )


class ChunkContextArtifact(BaseModel):
    project_id: str
    chunk_id: str
    llm_provider: str
    llm_model: str
    response_source: Literal["llm", "response_path"]
    context: ChunkContextProfile
    character_registry_updates: list[CharacterRegistryUpdate] = Field(
        default_factory=list
    )


class Stage1ContextHint(BaseModel):
    scene_hint: str | None = None
    known_characters: list[dict[str, object]] = Field(default_factory=list)
    aliases_observed: list[dict[str, object]] = Field(default_factory=list)
    contextual_references: list[dict[str, object]] = Field(default_factory=list)
    unresolved_pronouns: list[dict[str, object]] = Field(default_factory=list)
    important_context: list[str] = Field(default_factory=list)
