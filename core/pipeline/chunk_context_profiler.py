from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable

from config.loader import load_config
from config.models import AppConfig
from core.models.character import (
    AliasEvidence,
    CharacterRecord,
    CharacterRegistryArtifact,
    CharacterRegistryUpdate,
    ChunkContextArtifact,
    ChunkContextProfilerResponse,
    Stage1ContextHint,
)
from core.models.chunk import ChunksArtifact, TextChunk
from llm.json_utils import parse_json_object_response
from llm.prompts.chunk_context_profiler import (
    SYSTEM_PROMPT,
    build_chunk_context_profiler_user_prompt,
)
from llm.schemas import LlmCompletion
from llm.service import LlmService
from storage.json_store import write_json
from storage.workspace import Workspace


@dataclass(frozen=True)
class ChunkContextProfilerResult:
    workspace: Workspace
    registry: CharacterRegistryArtifact
    artifacts: list[ChunkContextArtifact]


@dataclass(frozen=True)
class ContextProfileProgress:
    chunk_id: str | None
    processed_chunks: int
    total_chunks: int
    chunk_elapsed_seconds: float | None
    total_elapsed_seconds: float
    status: str
    errors: list[str]


ContextProfileProgressCallback = Callable[[ContextProfileProgress], None]


def run_chunk_context_profiler_workflow(
    project_id: str,
    *,
    response_dir: str | Path | None = None,
    config: AppConfig | None = None,
    workspace_root: str | Path = "data/interim",
    llm_service: LlmService | None = None,
    progress_callback: ContextProfileProgressCallback | None = None,
) -> ChunkContextProfilerResult:
    app_config = config or load_config()
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()

    chunks_artifact = _read_chunks_artifact(workspace.chunks_path)
    registry = _read_registry(workspace.character_registry_path, project_id)
    service = llm_service
    if service is None and response_dir is None:
        service = LlmService(app_config.llm)

    total_chunks = len(chunks_artifact.chunks)
    workflow_started_at = time.monotonic()
    _emit_progress(
        progress_callback,
        ContextProfileProgress(
            chunk_id=None,
            processed_chunks=0,
            total_chunks=total_chunks,
            chunk_elapsed_seconds=None,
            total_elapsed_seconds=0.0,
            status="running",
            errors=[],
        ),
    )

    previous_summary: str | None = None
    artifacts: list[ChunkContextArtifact] = []

    for chunk in chunks_artifact.chunks:
        chunk_started_at = time.monotonic()
        _emit_progress(
            progress_callback,
            ContextProfileProgress(
                chunk_id=chunk.chunk_id,
                processed_chunks=len(artifacts),
                total_chunks=total_chunks,
                chunk_elapsed_seconds=0.0,
                total_elapsed_seconds=chunk_started_at - workflow_started_at,
                status="chunk_started",
                errors=[],
            ),
        )
        response_source = "response_path" if response_dir else "llm"
        prompt = build_chunk_context_profiler_user_prompt(
            chunk_id=chunk.chunk_id,
            previous_summary=previous_summary,
            previous_context=chunk.previous_context,
            chunk_text=chunk.text,
            next_context=chunk.next_context,
            character_registry=_compact_registry_for_prompt(registry),
        )
        completion = _complete_chunk_profile(
            chunk=chunk,
            response_dir=response_dir,
            llm_service=service,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
        )

        raw_path = workspace.context_raw_response_path(chunk.chunk_id)
        raw_path.write_text(completion.content.strip() + "\n", encoding="utf-8")
        response_data = parse_json_object_response(completion.content)
        profiler_response = ChunkContextProfilerResponse.model_validate(response_data)
        registry = merge_character_registry(
            registry,
            profiler_response.character_registry_updates,
        )
        artifact = ChunkContextArtifact(
            project_id=project_id,
            chunk_id=chunk.chunk_id,
            llm_provider=app_config.llm.provider,
            llm_model=app_config.llm.model,
            response_source=response_source,
            context=profiler_response.context,
            character_registry_updates=profiler_response.character_registry_updates,
        )
        write_json(workspace.context_artifact_path(chunk.chunk_id), artifact)
        write_json(workspace.character_registry_path, registry)
        previous_summary = profiler_response.context.scene_summary
        artifacts.append(artifact)
        _emit_progress(
            progress_callback,
            ContextProfileProgress(
                chunk_id=chunk.chunk_id,
                processed_chunks=len(artifacts),
                total_chunks=total_chunks,
                chunk_elapsed_seconds=time.monotonic() - chunk_started_at,
                total_elapsed_seconds=time.monotonic() - workflow_started_at,
                status="chunk_complete",
                errors=[],
            ),
        )

    _emit_progress(
        progress_callback,
        ContextProfileProgress(
            chunk_id=None,
            processed_chunks=len(artifacts),
            total_chunks=total_chunks,
            chunk_elapsed_seconds=None,
            total_elapsed_seconds=time.monotonic() - workflow_started_at,
            status="complete",
            errors=[],
        ),
    )

    return ChunkContextProfilerResult(
        workspace=workspace,
        registry=registry,
        artifacts=artifacts,
    )


def merge_character_registry(
    registry: CharacterRegistryArtifact,
    updates: list[CharacterRegistryUpdate],
) -> CharacterRegistryArtifact:
    records = [record.model_copy(deep=True) for record in registry.characters]
    for update in updates:
        record = _find_target_record(records, update)
        if record is None:
            record = CharacterRecord(
                character_id=_next_character_id(records),
                canonical_name=update.canonical_name.strip(),
                stable_aliases=[],
                contextual_references=[],
                aliases=[],
                alias_evidence=[],
                persona_summary=update.persona_summary,
                speaking_style=update.speaking_style,
                age_impression=update.age_impression,
                voice_variant_notes=_dedupe_strings(update.voice_variant_notes),
                confidence=update.confidence,
                review_notes=_dedupe_strings(update.review_notes),
            )
            records.append(record)
        else:
            record.canonical_name = update.canonical_name.strip() or record.canonical_name
            record.confidence = max(record.confidence, update.confidence)
            record.persona_summary = update.persona_summary or record.persona_summary
            record.speaking_style = update.speaking_style or record.speaking_style
            record.age_impression = update.age_impression or record.age_impression
            record.voice_variant_notes = _dedupe_strings(
                [*record.voice_variant_notes, *update.voice_variant_notes]
            )
            record.contextual_references = _merge_reference_evidence(
                record.contextual_references,
                update.contextual_references,
            )
            record.review_notes = _dedupe_strings(
                [*record.review_notes, *update.review_notes]
            )

        _merge_stable_aliases(records, record, update)
        record.contextual_references = _merge_reference_evidence(
            record.contextual_references,
            update.contextual_references,
        )

    return CharacterRegistryArtifact(project_id=registry.project_id, characters=records)


def build_stage1_context_hint(
    project_id: str,
    chunk_id: str,
    *,
    workspace_root: str | Path = "data/interim",
    max_characters: int = 12,
    max_context_items: int = 6,
) -> Stage1ContextHint | None:
    workspace = Workspace(project_id, root=workspace_root)
    context_path = workspace.context_artifact_path(chunk_id)
    registry_path = workspace.character_registry_path
    if not context_path.exists() or not registry_path.exists():
        return None

    artifact = ChunkContextArtifact.model_validate_json(
        context_path.read_text(encoding="utf-8")
    )
    registry = _read_registry(registry_path, project_id)
    relevant_names = {name.strip() for name in artifact.context.active_characters if name}
    relevant_names.update(
        observation.text.strip()
        for observation in artifact.context.aliases_observed
        if observation.text.strip()
    )
    relevant_character_ids = {
        observation.likely_character_id
        for observation in artifact.context.aliases_observed
        if observation.likely_character_id
    }
    known_characters: list[dict[str, object]] = []
    for record in registry.characters:
        stable_aliases = _stable_aliases(record)
        contextual_references = _contextual_reference_names(record)
        searchable_names = _dedupe_strings([*stable_aliases, *contextual_references])
        if (
            relevant_names
            and not relevant_names.intersection(searchable_names)
            and record.character_id not in relevant_character_ids
        ):
            continue
        known_characters.append(
            {
                "character_id": record.character_id,
                "canonical_name": record.canonical_name,
                "stable_aliases": stable_aliases[:8],
            }
        )
        if len(known_characters) >= max_characters:
            break

    contextual_references = []
    for observation in artifact.context.aliases_observed[:max_context_items]:
        if observation.reference_type == "stable_name":
            continue
        contextual_references.append(
            {
                **observation.model_dump(),
                "canonical_name": _canonical_name_for_id(
                    registry, observation.likely_character_id
                ),
            }
        )

    return Stage1ContextHint(
        scene_hint=artifact.context.scene_summary,
        known_characters=known_characters,
        aliases_observed=[
            observation.model_dump()
            for observation in artifact.context.aliases_observed[:max_context_items]
        ],
        contextual_references=contextual_references,
        unresolved_pronouns=[
            pronoun.model_dump()
            for pronoun in artifact.context.unresolved_pronouns[:max_context_items]
        ],
        important_context=artifact.context.important_context[:max_context_items],
    )


def _complete_chunk_profile(
    *,
    chunk: TextChunk,
    response_dir: str | Path | None,
    llm_service: LlmService | None,
    system_prompt: str,
    user_prompt: str,
) -> LlmCompletion:
    if response_dir is not None:
        response_path = Path(response_dir) / f"{chunk.chunk_id}_response.json"
        if not response_path.exists():
            raise RuntimeError(f"Missing Stage 1 response fixture: {response_path}")
        return LlmCompletion(content=response_path.read_text(encoding="utf-8"))
    if llm_service is None:
        raise RuntimeError("llm_service is required for live Stage 1 profiling")
    return llm_service.complete_json(system_prompt, user_prompt)


def _read_chunks_artifact(path: Path) -> ChunksArtifact:
    return ChunksArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _read_registry(path: Path, project_id: str) -> CharacterRegistryArtifact:
    if not path.exists():
        return CharacterRegistryArtifact(project_id=project_id, characters=[])
    registry = CharacterRegistryArtifact.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if registry.project_id != project_id:
        raise RuntimeError(
            f"{path} project_id={registry.project_id!r}, expected {project_id!r}"
        )
    return registry


def _compact_registry_for_prompt(
    registry: CharacterRegistryArtifact,
) -> list[dict[str, object]]:
    return [
        {
            "character_id": record.character_id,
            "canonical_name": record.canonical_name,
            "stable_aliases": _stable_aliases(record)[:8],
            "persona_summary": record.persona_summary,
            "speaking_style": record.speaking_style,
            "age_impression": record.age_impression,
            "voice_variant_notes": record.voice_variant_notes[:4],
        }
        for record in registry.characters
    ]


def _find_target_record(
    records: list[CharacterRecord],
    update: CharacterRegistryUpdate,
) -> CharacterRecord | None:
    if update.character_id:
        for record in records:
            if record.character_id == update.character_id:
                return record
        return None

    update_names = _dedupe_strings(
        [
            update.canonical_name,
            *update.stable_aliases,
            *update.aliases,
            *[
                evidence.alias
                for evidence in update.alias_evidence
                if evidence.reference_type == "stable_name"
            ],
        ]
    )
    matches = [
        record
        for record in records
        if set(_stable_aliases(record)).intersection(update_names)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _merge_stable_aliases(
    records: list[CharacterRecord],
    target: CharacterRecord,
    update: CharacterRegistryUpdate,
) -> None:
    candidate_aliases = _dedupe_strings(
        [
            update.canonical_name,
            *update.stable_aliases,
            *update.aliases,
        ]
    )
    for evidence in update.alias_evidence:
        if evidence.reference_type == "stable_name":
            candidate_aliases = _dedupe_strings([*candidate_aliases, evidence.alias])

    for alias in candidate_aliases:
        if not _is_global_stable_reference(alias):
            target.contextual_references = _merge_reference_evidence(
                target.contextual_references,
                [
                    AliasEvidence(
                        alias=alias,
                        reference_type="honorific",
                        evidence_text=alias,
                        source="stable_alias_guard",
                        confidence=update.confidence,
                        review_notes=[
                            "Moved from stable aliases to contextual references."
                        ],
                    )
                ],
            )
            continue
        owner = _alias_owner(records, alias)
        if owner is not None and owner.character_id != target.character_id:
            note = (
                f"Alias {alias!r} also appears on {owner.character_id}; "
                "left unresolved."
            )
            target.review_notes = _dedupe_strings([*target.review_notes, note])
            continue
        target.stable_aliases = _dedupe_strings([*target.stable_aliases, alias])
        target.aliases = _dedupe_strings([*target.aliases, alias])

    existing_evidence = {
        (evidence.alias, evidence.evidence_text) for evidence in target.alias_evidence
    }
    for evidence in update.alias_evidence:
        if evidence.reference_type != "stable_name":
            target.contextual_references = _merge_reference_evidence(
                target.contextual_references,
                [evidence],
            )
            continue
        cleaned = AliasEvidence(
            alias=evidence.alias.strip(),
            reference_type=evidence.reference_type,
            evidence_text=evidence.evidence_text,
            source=evidence.source,
            confidence=evidence.confidence,
            review_notes=evidence.review_notes,
        )
        key = (cleaned.alias, cleaned.evidence_text)
        if cleaned.alias and key not in existing_evidence:
            target.alias_evidence.append(cleaned)
            existing_evidence.add(key)


def _alias_owner(records: list[CharacterRecord], alias: str) -> CharacterRecord | None:
    cleaned = alias.strip()
    for record in records:
        if cleaned in _stable_aliases(record):
            return record
    return None


def _next_character_id(records: list[CharacterRecord]) -> str:
    highest = 0
    for record in records:
        prefix, _, suffix = record.character_id.partition("_")
        if prefix != "character":
            continue
        try:
            highest = max(highest, int(suffix))
        except ValueError:
            continue
    return f"character_{highest + 1:03d}"


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        result.append(cleaned)
        seen.add(cleaned)
    return result


def _emit_progress(
    progress_callback: ContextProfileProgressCallback | None,
    progress: ContextProfileProgress,
) -> None:
    if progress_callback is not None:
        progress_callback(progress)


def _stable_aliases(record: CharacterRecord) -> list[str]:
    return [
        alias
        for alias in _dedupe_strings([record.canonical_name, *record.stable_aliases])
        if _is_global_stable_reference(alias) or alias == record.canonical_name
    ]


def _contextual_reference_names(record: CharacterRecord) -> list[str]:
    return _dedupe_strings(
        [reference.alias for reference in record.contextual_references]
    )


def _merge_reference_evidence(
    existing: list[AliasEvidence],
    incoming: list[AliasEvidence],
) -> list[AliasEvidence]:
    merged = [reference.model_copy(deep=True) for reference in existing]
    seen = {
        (reference.alias, reference.reference_type, reference.evidence_text)
        for reference in merged
    }
    for reference in incoming:
        cleaned = AliasEvidence(
            alias=reference.alias.strip(),
            reference_type=reference.reference_type,
            evidence_text=reference.evidence_text,
            source=reference.source,
            confidence=reference.confidence,
            review_notes=reference.review_notes,
        )
        key = (cleaned.alias, cleaned.reference_type, cleaned.evidence_text)
        if cleaned.alias and key not in seen:
            merged.append(cleaned)
            seen.add(key)
    return merged


def _canonical_name_for_id(
    registry: CharacterRegistryArtifact,
    character_id: str | None,
) -> str | None:
    if character_id is None:
        return None
    for record in registry.characters:
        if record.character_id == character_id:
            return record.canonical_name
    return None


def _is_global_stable_reference(alias: str) -> bool:
    cleaned = alias.strip()
    if cleaned in {
        "先生",
        "小先生",
        "大先生",
        "马丁先生",
        "我的当事人",
        "那个自由的机器人",
        "自由的机器人",
    }:
        return False
    if cleaned.endswith("先生") and "·" not in cleaned:
        return False
    return True
