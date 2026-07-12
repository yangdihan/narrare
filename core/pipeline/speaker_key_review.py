from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from config.loader import load_config
from config.models import AppConfig
from core.models.character import (
    AliasEvidence,
    CharacterRecord,
    CharacterRegistryArtifact,
    ChunkContextArtifact,
)
from core.models.chunk import ChunksArtifact, TextChunk
from core.models.ir import (
    ScriptArtifact,
    ScriptSegment,
    SpeakerKeyReviewResponse,
)
from core.pipeline.script_assembly import COMPLETE_SCRIPT_CHUNK_ID
from core.validation.script_integrity import validate_script_segments
from llm.json_utils import parse_json_object_response
from llm.prompts.speaker_key_reviewer import (
    SYSTEM_PROMPT,
    build_speaker_key_reviewer_user_prompt,
)
from llm.schemas import LlmCompletion
from llm.service import LlmService
from storage.json_store import write_json
from storage.workspace import Workspace


RESERVED_REPLACEMENT_KEYS = ["narrator", "unknown_speaker"]
DEFAULT_REVIEW_CONFIDENCE_THRESHOLD = 0.85


@dataclass(frozen=True)
class SpeakerKeyReviewCandidate:
    segment: ScriptSegment
    previous_segment: ScriptSegment | None
    next_segment: ScriptSegment | None


@dataclass(frozen=True)
class SpeakerKeyReviewResult:
    workspace: Workspace
    artifact: ScriptArtifact
    report_path: Path
    reviewed_count: int
    changed_count: int
    skipped_count: int
    exact_reconstruction_success: bool
    errors: list[str]


@dataclass(frozen=True)
class SpeakerKeyReviewProgress:
    segment_id: str | None
    current_key: str | None
    processed_candidates: int
    total_candidates: int
    changed_count: int
    candidate_elapsed_seconds: float | None
    total_elapsed_seconds: float
    status: str
    errors: list[str]


SpeakerKeyReviewProgressCallback = Callable[[SpeakerKeyReviewProgress], None]


def run_speaker_key_review_workflow(
    project_id: str,
    *,
    response_dir: str | Path | None = None,
    config: AppConfig | None = None,
    workspace_root: str | Path = "data/interim",
    llm_service: LlmService | None = None,
    confidence_threshold: float = DEFAULT_REVIEW_CONFIDENCE_THRESHOLD,
    progress_callback: SpeakerKeyReviewProgressCallback | None = None,
) -> SpeakerKeyReviewResult:
    workflow_started_at = time.monotonic()
    app_config = config or load_config()
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()

    script_artifact = _read_script_artifact(
        workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID)
    )
    chunks_artifact = _read_chunks_artifact(workspace.chunks_path)
    registry = _read_registry(workspace.character_registry_path, project_id)
    contexts = _read_context_artifacts(workspace, chunks_artifact)
    complete_source = "".join(chunk.text for chunk in chunks_artifact.chunks)
    allowed_replacement_keys = _allowed_replacement_keys(registry)
    canonical_names = {record.canonical_name for record in registry.characters}
    candidates = extract_speaker_key_review_candidates(
        script_artifact.segments,
        canonical_names=canonical_names,
    )

    service = llm_service
    if service is None and response_dir is None:
        service = LlmService(app_config.llm)

    reviewed_by_segment_id: dict[str, SpeakerKeyReviewResponse] = {}
    review_events: list[dict[str, Any]] = []
    applied_count = 0
    _emit_progress(
        progress_callback,
        SpeakerKeyReviewProgress(
            segment_id=None,
            current_key=None,
            processed_candidates=0,
            total_candidates=len(candidates),
            changed_count=0,
            candidate_elapsed_seconds=None,
            total_elapsed_seconds=0.0,
            status="running",
            errors=[],
        ),
    )

    for index, candidate in enumerate(candidates):
        candidate_started_at = time.monotonic()
        _emit_progress(
            progress_callback,
            SpeakerKeyReviewProgress(
                segment_id=candidate.segment.segment_id,
                current_key=candidate.segment.speaker,
                processed_candidates=index,
                total_candidates=len(candidates),
                changed_count=applied_count,
                candidate_elapsed_seconds=0.0,
                total_elapsed_seconds=candidate_started_at - workflow_started_at,
                status="candidate_started",
                errors=[],
            ),
        )
        prompt = build_speaker_key_reviewer_user_prompt(
            segment=_segment_prompt_payload(candidate.segment),
            previous_segment=_optional_segment_prompt_payload(
                candidate.previous_segment
            ),
            next_segment=_optional_segment_prompt_payload(candidate.next_segment),
            scene_context=_scene_context_payload(
                candidate.segment,
                chunks_artifact.chunks,
                contexts,
            ),
            relevant_characters=_relevant_character_payloads(
                candidate,
                registry.characters,
                _context_for_segment(candidate.segment, chunks_artifact.chunks, contexts),
            ),
            allowed_replacement_keys=allowed_replacement_keys,
            confidence_threshold=confidence_threshold,
        )
        completion = _complete_key_review(
            segment_id=candidate.segment.segment_id,
            response_dir=response_dir,
            llm_service=service,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
        )
        raw_path = workspace.speaker_key_review_raw_response_path(
            candidate.segment.segment_id
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(completion.content.strip() + "\n", encoding="utf-8")

        try:
            response_data = parse_json_object_response(completion.content)
            review = SpeakerKeyReviewResponse.model_validate(response_data)
        except Exception as exc:
            errors = [f"invalid speaker key review JSON: {exc}"]
            review_events.append(
                {
                    "segment_id": candidate.segment.segment_id,
                    "current_key": candidate.segment.speaker,
                    "status": "invalid_response",
                    "error": str(exc),
                }
            )
            _emit_progress(
                progress_callback,
                SpeakerKeyReviewProgress(
                    segment_id=candidate.segment.segment_id,
                    current_key=candidate.segment.speaker,
                    processed_candidates=index + 1,
                    total_candidates=len(candidates),
                    changed_count=applied_count,
                    candidate_elapsed_seconds=time.monotonic() - candidate_started_at,
                    total_elapsed_seconds=time.monotonic() - workflow_started_at,
                    status="candidate_failed",
                    errors=errors,
                ),
            )
            continue

        reviewed_by_segment_id[candidate.segment.segment_id] = review
        event = _review_event(
            candidate.segment,
            review,
            allowed_replacement_keys=set(allowed_replacement_keys),
            confidence_threshold=confidence_threshold,
        )
        review_events.append(event)
        if event["status"] == "applied":
            applied_count += 1
        _emit_progress(
            progress_callback,
            SpeakerKeyReviewProgress(
                segment_id=candidate.segment.segment_id,
                current_key=candidate.segment.speaker,
                processed_candidates=index + 1,
                total_candidates=len(candidates),
                changed_count=applied_count,
                candidate_elapsed_seconds=time.monotonic() - candidate_started_at,
                total_elapsed_seconds=time.monotonic() - workflow_started_at,
                status="candidate_complete",
                errors=[],
            ),
        )

    reviewed_segments: list[ScriptSegment] = []
    changed_count = 0
    event_by_segment_id = {
        str(event["segment_id"]): event for event in review_events if "segment_id" in event
    }
    for segment in script_artifact.segments:
        review = reviewed_by_segment_id.get(segment.segment_id)
        event = event_by_segment_id.get(segment.segment_id)
        if review is None or event is None or event["status"] != "applied":
            reviewed_segments.append(segment)
            continue

        changed_count += 1
        replacement_key = str(review.replacement_key)
        reviewed_segments.append(
            ScriptSegment(
                segment_id=segment.segment_id,
                source_span=segment.source_span,
                script={replacement_key: segment.text},
                raw_script_key=segment.speaker,
                speaker_key_normalization=segment.speaker_key_normalization,
                speaker_key_review={
                    "from": segment.speaker,
                    "to": replacement_key,
                    "decision": review.decision,
                    "confidence": review.confidence,
                    "evidence": review.evidence,
                    "review_notes": review.review_notes,
                },
                confidence=segment.confidence,
                review_notes=segment.review_notes,
            )
        )

    reviewed_artifact = ScriptArtifact(
        project_id=script_artifact.project_id,
        chunk_id=script_artifact.chunk_id,
        chunk_source_path=script_artifact.chunk_source_path,
        chunk_sha256=script_artifact.chunk_sha256,
        llm_provider=script_artifact.llm_provider,
        llm_model=script_artifact.llm_model,
        response_source="speaker_key_review",
        processed_chunk_count=script_artifact.processed_chunk_count,
        segments=reviewed_segments,
    )
    validation_report = validate_script_segments(
        project_id=project_id,
        chunk_id=COMPLETE_SCRIPT_CHUNK_ID,
        source_text=complete_source,
        segments=reviewed_segments,
    )
    report = {
        "project_id": project_id,
        "chunk_id": COMPLETE_SCRIPT_CHUNK_ID,
        "confidence_threshold": confidence_threshold,
        "canonical_speaker_count": len(canonical_names),
        "candidate_count": len(candidates),
        "reviewed_count": len(reviewed_by_segment_id),
        "changed_count": changed_count,
        "skipped_count": len(script_artifact.segments) - len(candidates),
        "events": review_events,
        "validation": validation_report.model_dump(),
    }

    output_path = workspace.key_reviewed_script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID)
    report_path = workspace.speaker_key_review_report_path(COMPLETE_SCRIPT_CHUNK_ID)
    write_json(output_path, reviewed_artifact)
    write_json(report_path, report)

    if not validation_report.exact_reconstruction_success:
        _emit_progress(
            progress_callback,
            SpeakerKeyReviewProgress(
                segment_id=None,
                current_key=None,
                processed_candidates=len(candidates),
                total_candidates=len(candidates),
                changed_count=changed_count,
                candidate_elapsed_seconds=None,
                total_elapsed_seconds=time.monotonic() - workflow_started_at,
                status="failed",
                errors=validation_report.errors,
            ),
        )
        raise RuntimeError(
            "Speaker key reviewed script validation failed: "
            + "; ".join(validation_report.errors)
        )

    _emit_progress(
        progress_callback,
        SpeakerKeyReviewProgress(
            segment_id=None,
            current_key=None,
            processed_candidates=len(candidates),
            total_candidates=len(candidates),
            changed_count=changed_count,
            candidate_elapsed_seconds=None,
            total_elapsed_seconds=time.monotonic() - workflow_started_at,
            status="complete",
            errors=[],
        ),
    )
    return SpeakerKeyReviewResult(
        workspace=workspace,
        artifact=reviewed_artifact,
        report_path=report_path,
        reviewed_count=len(reviewed_by_segment_id),
        changed_count=changed_count,
        skipped_count=len(script_artifact.segments) - len(candidates),
        exact_reconstruction_success=validation_report.exact_reconstruction_success,
        errors=validation_report.errors,
    )


def extract_speaker_key_review_candidates(
    segments: list[ScriptSegment],
    *,
    canonical_names: set[str],
) -> list[SpeakerKeyReviewCandidate]:
    skip_keys = {*canonical_names, "narrator"}
    candidates: list[SpeakerKeyReviewCandidate] = []
    for index, segment in enumerate(segments):
        if segment.speaker in skip_keys:
            continue
        candidates.append(
            SpeakerKeyReviewCandidate(
                segment=segment,
                previous_segment=segments[index - 1] if index > 0 else None,
                next_segment=segments[index + 1] if index + 1 < len(segments) else None,
            )
        )
    return candidates


def _complete_key_review(
    *,
    segment_id: str,
    response_dir: str | Path | None,
    llm_service: LlmService | None,
    system_prompt: str,
    user_prompt: str,
) -> LlmCompletion:
    if response_dir is not None:
        response_path = Path(response_dir) / f"{segment_id}_response.json"
        if not response_path.exists():
            raise RuntimeError(f"Missing Stage 3 response fixture: {response_path}")
        return LlmCompletion(content=response_path.read_text(encoding="utf-8"))
    if llm_service is None:
        raise RuntimeError("llm_service is required for live Stage 3 key review")
    return llm_service.complete_json(system_prompt, user_prompt)


def _review_event(
    segment: ScriptSegment,
    review: SpeakerKeyReviewResponse,
    *,
    allowed_replacement_keys: set[str],
    confidence_threshold: float,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "segment_id": segment.segment_id,
        "current_key": segment.speaker,
        "decision": review.decision,
        "replacement_key": review.replacement_key,
        "confidence": review.confidence,
        "evidence": review.evidence,
        "review_notes": review.review_notes,
    }
    if review.current_key != segment.speaker:
        event["status"] = "current_key_mismatch"
        return event
    if review.decision != "replace":
        event["status"] = review.decision
        return event
    if review.replacement_key not in allowed_replacement_keys:
        event["status"] = "invalid_replacement_key"
        return event
    if review.confidence < confidence_threshold:
        event["status"] = "low_confidence"
        return event

    event["status"] = "applied"
    return event


def _allowed_replacement_keys(registry: CharacterRegistryArtifact) -> list[str]:
    return _dedupe_strings(
        [
            *(record.canonical_name for record in registry.characters),
            *RESERVED_REPLACEMENT_KEYS,
        ]
    )


def _segment_prompt_payload(segment: ScriptSegment) -> dict[str, Any]:
    return {
        "segment_id": segment.segment_id,
        "source_span": segment.source_span.model_dump(),
        "script": segment.script,
        "confidence": segment.confidence,
        "review_notes": segment.review_notes,
    }


def _optional_segment_prompt_payload(
    segment: ScriptSegment | None,
) -> dict[str, Any] | None:
    if segment is None:
        return None
    return _segment_prompt_payload(segment)


def _scene_context_payload(
    segment: ScriptSegment,
    chunks: list[TextChunk],
    contexts: dict[str, ChunkContextArtifact],
) -> dict[str, Any]:
    covered_chunks = _chunks_for_segment(segment, chunks)
    return {
        "covered_chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "scene_summary": contexts[chunk.chunk_id].context.scene_summary,
                "active_characters": contexts[
                    chunk.chunk_id
                ].context.active_characters,
                "aliases_observed": [
                    observation.model_dump()
                    for observation in contexts[
                        chunk.chunk_id
                    ].context.aliases_observed
                ],
                "important_context": contexts[
                    chunk.chunk_id
                ].context.important_context,
                "unresolved_pronouns": [
                    pronoun.model_dump()
                    for pronoun in contexts[
                        chunk.chunk_id
                    ].context.unresolved_pronouns
                ],
                "review_notes": contexts[chunk.chunk_id].context.review_notes,
            }
            for chunk in covered_chunks
        ]
    }


def _context_for_segment(
    segment: ScriptSegment,
    chunks: list[TextChunk],
    contexts: dict[str, ChunkContextArtifact],
) -> ChunkContextArtifact:
    chunk = _chunks_for_segment(segment, chunks)[0]
    return contexts[chunk.chunk_id]


def _relevant_character_payloads(
    candidate: SpeakerKeyReviewCandidate,
    records: list[CharacterRecord],
    context: ChunkContextArtifact,
    *,
    max_records: int = 12,
) -> list[dict[str, Any]]:
    names = {
        candidate.segment.speaker,
        *(segment.speaker for segment in [candidate.previous_segment] if segment),
        *(segment.speaker for segment in [candidate.next_segment] if segment),
        *context.context.active_characters,
        *(observation.text for observation in context.context.aliases_observed),
    }
    likely_ids = {
        observation.likely_character_id
        for observation in context.context.aliases_observed
        if observation.likely_character_id
    }
    relevant: list[CharacterRecord] = []
    for record in records:
        record_names = {
            record.canonical_name,
            *record.stable_aliases,
            *[reference.alias for reference in record.contextual_references],
        }
        if record.character_id in likely_ids or names.intersection(record_names):
            relevant.append(record)
    if not relevant:
        relevant = records[:max_records]
    return [_character_prompt_payload(record) for record in relevant[:max_records]]


def _character_prompt_payload(record: CharacterRecord) -> dict[str, Any]:
    return {
        "character_id": record.character_id,
        "canonical_name": record.canonical_name,
        "stable_aliases": record.stable_aliases,
        "contextual_references": [
            _reference_payload(reference)
            for reference in record.contextual_references[:12]
        ],
        "persona_summary": record.persona_summary,
        "speaking_style": record.speaking_style,
        "age_impression": record.age_impression,
        "voice_variant_notes": record.voice_variant_notes,
        "confidence": record.confidence,
        "review_notes": record.review_notes,
    }


def _reference_payload(reference: AliasEvidence) -> dict[str, Any]:
    return {
        "alias": reference.alias,
        "reference_type": reference.reference_type,
        "evidence_text": reference.evidence_text,
        "confidence": reference.confidence,
        "review_notes": reference.review_notes,
    }


def _chunks_for_segment(segment: ScriptSegment, chunks: list[TextChunk]) -> list[TextChunk]:
    matched: list[TextChunk] = []
    cursor = 0
    for chunk in chunks:
        start = cursor
        end = start + len(chunk.text)
        cursor = end
        if segment.source_span.start < end and segment.source_span.end > start:
            matched.append(chunk)
    if not matched:
        raise RuntimeError(
            f"No chunk context matches {segment.segment_id} "
            f"span={segment.source_span.start}:{segment.source_span.end}"
        )
    return matched


def _read_script_artifact(path: Path) -> ScriptArtifact:
    if not path.exists():
        raise RuntimeError(f"Missing complete script artifact: {path}")
    return ScriptArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _read_chunks_artifact(path: Path) -> ChunksArtifact:
    if not path.exists():
        raise RuntimeError(f"Missing chunks artifact: {path}")
    return ChunksArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _read_registry(path: Path, project_id: str) -> CharacterRegistryArtifact:
    if not path.exists():
        raise RuntimeError(f"Missing character registry: {path}")
    registry = CharacterRegistryArtifact.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if registry.project_id != project_id:
        raise RuntimeError(
            f"{path} project_id={registry.project_id!r}, expected {project_id!r}"
        )
    return registry


def _read_context_artifacts(
    workspace: Workspace,
    chunks_artifact: ChunksArtifact,
) -> dict[str, ChunkContextArtifact]:
    contexts: dict[str, ChunkContextArtifact] = {}
    for chunk in chunks_artifact.chunks:
        context_path = workspace.context_artifact_path(chunk.chunk_id)
        if not context_path.exists():
            raise RuntimeError(f"Missing Stage 1 context artifact: {context_path}")
        contexts[chunk.chunk_id] = ChunkContextArtifact.model_validate_json(
            context_path.read_text(encoding="utf-8")
        )
    return contexts


def _dedupe_strings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        deduped.append(cleaned)
        seen.add(cleaned)
    return deduped


def _emit_progress(
    progress_callback: SpeakerKeyReviewProgressCallback | None,
    progress: SpeakerKeyReviewProgress,
) -> None:
    if progress_callback is not None:
        progress_callback(progress)
