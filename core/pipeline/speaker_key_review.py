from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.loader import load_config
from config.models import AppConfig
from core.ir.script_revision import script_artifact_revision
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
from core.pipeline.speaker_key_normalization import (
    DEFAULT_ALIAS_CONFIDENCE_THRESHOLD,
    run_speaker_key_normalization_workflow,
)
from core.validation.script_integrity import validate_script_segments
from llm.json_utils import parse_json_object_response
from llm.prompts.speaker_key_reviewer import (
    SYSTEM_PROMPT,
    build_speaker_key_reviewer_batch_user_prompt,
)
from llm.schemas import LlmCompletion
from llm.service import LlmService
from storage.json_store import write_json
from storage.workspace import Workspace

RESERVED_REPLACEMENT_KEYS = ["narrator", "unknown_speaker"]
DEFAULT_REVIEW_CONFIDENCE_THRESHOLD = 0.85
DEFAULT_REVIEW_BATCH_SIZE = 16
DEFAULT_REVIEW_MAX_OUTPUT_TOKENS = 2_400
DEFAULT_REVIEW_REASONING_EFFORT = "minimal"


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
    deterministic_renamed_count: int = 0
    unresolved_count: int = 0
    llm_batch_count: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0


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
    batch_size: int = DEFAULT_REVIEW_BATCH_SIZE,
    max_output_tokens: int = DEFAULT_REVIEW_MAX_OUTPUT_TOKENS,
    reasoning_effort: str = DEFAULT_REVIEW_REASONING_EFFORT,
    progress_callback: SpeakerKeyReviewProgressCallback | None = None,
) -> SpeakerKeyReviewResult:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")
    if reasoning_effort not in {"none", "minimal", "low", "medium", "high"}:
        raise ValueError("reasoning_effort must be none, minimal, low, medium, or high")
    workflow_started_at = time.monotonic()
    app_config = config or load_config()
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()

    original_script_artifact = _read_script_artifact(
        workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID)
    )
    normalization_result = run_speaker_key_normalization_workflow(
        project_id,
        workspace_root=workspace_root,
        alias_confidence_threshold=DEFAULT_ALIAS_CONFIDENCE_THRESHOLD,
    )
    script_artifact = normalization_result.artifact
    normalization_report = json.loads(
        normalization_result.report_path.read_text(encoding="utf-8")
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

    batches = _batch_candidates_by_chunk(candidates, chunks_artifact.chunks, batch_size)
    prompt_tokens = 0
    output_tokens = 0
    if not batches and normalization_result.renamed_count:
        normalized = next(
            (
                segment
                for segment in script_artifact.segments
                if segment.speaker_key_normalization is not None
            ),
            None,
        )
        if normalized is not None:
            raw_key = str(normalized.speaker_key_normalization["from"])
            _emit_progress(
                progress_callback,
                SpeakerKeyReviewProgress(
                    segment_id=normalized.segment_id,
                    current_key=raw_key,
                    processed_candidates=0,
                    total_candidates=0,
                    changed_count=normalization_result.renamed_count,
                    candidate_elapsed_seconds=0.0,
                    total_elapsed_seconds=time.monotonic() - workflow_started_at,
                    status="candidate_started",
                    errors=[],
                ),
            )
            _emit_progress(
                progress_callback,
                SpeakerKeyReviewProgress(
                    segment_id=normalized.segment_id,
                    current_key=raw_key,
                    processed_candidates=1,
                    total_candidates=0,
                    changed_count=normalization_result.renamed_count,
                    candidate_elapsed_seconds=0.0,
                    total_elapsed_seconds=time.monotonic() - workflow_started_at,
                    status="candidate_complete",
                    errors=[],
                ),
            )
    for batch_index, batch in enumerate(batches):
        candidate_started_at = time.monotonic()
        first_candidate = batch[0]
        _emit_progress(
            progress_callback,
            SpeakerKeyReviewProgress(
                segment_id=first_candidate.segment.segment_id,
                current_key=first_candidate.segment.speaker,
                processed_candidates=sum(len(item) for item in batches[:batch_index]),
                total_candidates=len(candidates),
                changed_count=applied_count,
                candidate_elapsed_seconds=0.0,
                total_elapsed_seconds=candidate_started_at - workflow_started_at,
                status="candidate_started",
                errors=[],
            ),
        )
        prompt = build_speaker_key_reviewer_batch_user_prompt(
            candidates=[_batch_candidate_payload(candidate) for candidate in batch],
            scene_context=_batch_scene_context_payload(
                batch, chunks_artifact.chunks, contexts
            ),
            relevant_characters=_batch_relevant_character_payloads(
                batch, registry.characters, chunks_artifact.chunks, contexts
            ),
            allowed_replacement_keys=allowed_replacement_keys,
            confidence_threshold=confidence_threshold,
        )
        completion = _complete_key_review(
            batch_id=_batch_id(batch_index, batch, chunks_artifact.chunks),
            response_dir=response_dir,
            llm_service=service,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            candidate_segment_ids=[candidate.segment.segment_id for candidate in batch],
        )
        batch_id = _batch_id(batch_index, batch, chunks_artifact.chunks)
        raw_path = workspace.speaker_key_review_raw_response_path(batch_id)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(completion.content.strip() + "\n", encoding="utf-8")

        try:
            reviews = _parse_batch_reviews(completion.content, batch)
        except Exception as exc:
            errors = [f"invalid speaker key review JSON: {exc}"]
            review_events.append(
                {
                    "batch_id": batch_id,
                    "status": "invalid_response",
                    "error": str(exc),
                }
            )
            _emit_progress(
                progress_callback,
                SpeakerKeyReviewProgress(
                    segment_id=first_candidate.segment.segment_id,
                    current_key=first_candidate.segment.speaker,
                    processed_candidates=sum(
                        len(item) for item in batches[: batch_index + 1]
                    ),
                    total_candidates=len(candidates),
                    changed_count=applied_count,
                    candidate_elapsed_seconds=time.monotonic() - candidate_started_at,
                    total_elapsed_seconds=time.monotonic() - workflow_started_at,
                    status="candidate_failed",
                    errors=errors,
                ),
            )
            continue

        prompt_tokens += completion.prompt_tokens or _estimate_tokens(prompt)
        output_tokens += completion.completion_tokens or _estimate_tokens(
            completion.content
        )
        for candidate, review in zip(batch, reviews):
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
                segment_id=first_candidate.segment.segment_id,
                current_key=first_candidate.segment.speaker,
                processed_candidates=sum(
                    len(item) for item in batches[: batch_index + 1]
                ),
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
        str(event["segment_id"]): event
        for event in review_events
        if "segment_id" in event
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
                    "reason_code": review.reason_code,
                },
                confidence=segment.confidence,
                review_notes=segment.review_notes,
            )
        )

    final_segments, final_guard_events, final_guard_errors = (
        _enforce_final_speaker_keys(
            reviewed_segments,
            registry=registry,
        )
    )
    review_events.extend(final_guard_events)
    changed_count = sum(
        1
        for original, reviewed in zip(original_script_artifact.segments, final_segments)
        if original.speaker != reviewed.speaker
    )

    reviewed_artifact = ScriptArtifact(
        project_id=script_artifact.project_id,
        chunk_id=script_artifact.chunk_id,
        chunk_source_path=script_artifact.chunk_source_path,
        chunk_sha256=script_artifact.chunk_sha256,
        llm_provider=script_artifact.llm_provider,
        llm_model=script_artifact.llm_model,
        response_source="speaker_key_review",
        source_script_revision=script_artifact_revision(original_script_artifact),
        processed_chunk_count=script_artifact.processed_chunk_count,
        segments=final_segments,
    )
    validation_report = validate_script_segments(
        project_id=project_id,
        chunk_id=COMPLETE_SCRIPT_CHUNK_ID,
        source_text=complete_source,
        segments=final_segments,
    )
    report = {
        "project_id": project_id,
        "chunk_id": COMPLETE_SCRIPT_CHUNK_ID,
        "confidence_threshold": confidence_threshold,
        "batch_size": batch_size,
        "max_output_tokens": max_output_tokens,
        "reasoning_effort": reasoning_effort,
        "source_script_revision": reviewed_artifact.source_script_revision,
        "canonical_speaker_count": len(canonical_names),
        "deterministic_renamed_count": normalization_result.renamed_count,
        "deterministic_events": normalization_report.get("renamed", []),
        "deterministic_unresolved": normalization_report.get("unresolved", []),
        "unresolved_count": len(candidates),
        "llm_batch_count": len(batches),
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_output_tokens": output_tokens,
        "candidate_count": len(candidates),
        "reviewed_count": len(reviewed_by_segment_id),
        "changed_count": changed_count,
        "final_guard_changed_count": len(final_guard_events),
        "final_guard_errors": final_guard_errors,
        "skipped_count": len(script_artifact.segments) - len(candidates),
        "events": review_events,
        "validation": validation_report.model_dump(),
    }

    output_path = workspace.key_reviewed_script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID)
    report_path = workspace.speaker_key_review_report_path(COMPLETE_SCRIPT_CHUNK_ID)
    write_json(output_path, reviewed_artifact)
    write_json(report_path, report)

    if final_guard_errors:
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
                errors=final_guard_errors,
            ),
        )
        raise RuntimeError(
            "Speaker key reviewed script contains keys outside characters.json: "
            + "; ".join(final_guard_errors)
        )

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
        deterministic_renamed_count=normalization_result.renamed_count,
        unresolved_count=len(candidates),
        llm_batch_count=len(batches),
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
    )


def extract_speaker_key_review_candidates(
    segments: list[ScriptSegment],
    *,
    canonical_names: set[str],
) -> list[SpeakerKeyReviewCandidate]:
    skip_keys = {*canonical_names, *RESERVED_REPLACEMENT_KEYS}
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


def _batch_candidates_by_chunk(
    candidates: list[SpeakerKeyReviewCandidate],
    chunks: list[TextChunk],
    batch_size: int,
) -> list[list[SpeakerKeyReviewCandidate]]:
    by_chunk: dict[str, list[SpeakerKeyReviewCandidate]] = {}
    for candidate in candidates:
        chunk_id = _chunks_for_segment(candidate.segment, chunks)[0].chunk_id
        by_chunk.setdefault(chunk_id, []).append(candidate)
    return [
        group[index : index + batch_size]
        for group in by_chunk.values()
        for index in range(0, len(group), batch_size)
    ]


def _batch_id(
    batch_index: int,
    batch: list[SpeakerKeyReviewCandidate],
    chunks: list[TextChunk],
) -> str:
    chunk_id = _chunks_for_segment(batch[0].segment, chunks)[0].chunk_id
    return f"batch_{chunk_id}_{batch_index + 1:04d}"


def _batch_candidate_payload(candidate: SpeakerKeyReviewCandidate) -> dict[str, Any]:
    return {
        "segment_id": candidate.segment.segment_id,
        "speaker_key": candidate.segment.speaker,
        "text": candidate.segment.text,
        "previous_speaker": (
            candidate.previous_segment.speaker if candidate.previous_segment else None
        ),
        "next_speaker": (
            candidate.next_segment.speaker if candidate.next_segment else None
        ),
    }


def _batch_scene_context_payload(
    batch: list[SpeakerKeyReviewCandidate],
    chunks: list[TextChunk],
    contexts: dict[str, ChunkContextArtifact],
) -> dict[str, Any]:
    chunk = _chunks_for_segment(batch[0].segment, chunks)[0]
    context = contexts[chunk.chunk_id].context
    return {
        "chunk_id": chunk.chunk_id,
        "scene_summary": context.scene_summary,
        "active_characters": context.active_characters,
        "aliases_observed": [
            {
                "text": item.text,
                "likely_character_id": item.likely_character_id,
                "confidence": item.confidence,
            }
            for item in context.aliases_observed
        ],
        "important_context": context.important_context,
    }


def _batch_relevant_character_payloads(
    batch: list[SpeakerKeyReviewCandidate],
    records: list[CharacterRecord],
    chunks: list[TextChunk],
    contexts: dict[str, ChunkContextArtifact],
) -> list[dict[str, Any]]:
    context = _context_for_segment(batch[0].segment, chunks, contexts)
    selected: dict[str, dict[str, Any]] = {}
    for candidate in batch:
        for payload in _relevant_character_payloads(candidate, records, context):
            selected[str(payload["character_id"])] = {
                "character_id": payload["character_id"],
                "canonical_name": payload["canonical_name"],
                "stable_aliases": payload["stable_aliases"],
                "contextual_references": payload["contextual_references"],
            }
    return list(selected.values())[:12]


def _parse_batch_reviews(
    content: str,
    batch: list[SpeakerKeyReviewCandidate],
) -> list[SpeakerKeyReviewResponse]:
    data = parse_json_object_response(content)
    items = data.get("reviews")
    if not isinstance(items, list):
        raise ValueError("batch response must contain a reviews list")
    reviews = [SpeakerKeyReviewResponse.model_validate(item) for item in items]
    expected = [candidate.segment.segment_id for candidate in batch]
    actual = [review.segment_id for review in reviews]
    if actual != expected:
        raise ValueError(
            f"batch review segment_ids must match request order: {expected}"
        )
    for candidate, review in zip(batch, reviews):
        if review.current_key is None:
            review.current_key = candidate.segment.speaker
    return reviews


def _estimate_tokens(text: str) -> int:
    # A conservative provider-independent estimate for cost visibility.
    return max(1, (len(text) + 3) // 4)


def _complete_key_review(
    *,
    batch_id: str,
    response_dir: str | Path | None,
    llm_service: LlmService | None,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
    reasoning_effort: str,
    candidate_segment_ids: list[str],
) -> LlmCompletion:
    if response_dir is not None:
        response_path = Path(response_dir) / f"{batch_id}_response.json"
        if response_path.exists():
            return LlmCompletion(content=response_path.read_text(encoding="utf-8"))
        legacy_responses = []
        for segment_id in candidate_segment_ids:
            legacy_path = Path(response_dir) / f"{segment_id}_response.json"
            if not legacy_path.exists():
                raise RuntimeError(
                    f"Missing Stage 3 batch response fixture: {response_path}"
                )
            legacy_responses.append(
                parse_json_object_response(legacy_path.read_text(encoding="utf-8"))
            )
        return LlmCompletion(
            content=json.dumps({"reviews": legacy_responses}, ensure_ascii=False)
        )
    if llm_service is None:
        raise RuntimeError("llm_service is required for live Stage 3 key review")
    if isinstance(llm_service, LlmService):
        return llm_service.complete_json_with_output_limit(
            system_prompt,
            user_prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        )
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
        "reason_code": review.reason_code,
    }
    if review.current_key is not None and review.current_key != segment.speaker:
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


def _enforce_final_speaker_keys(
    segments: list[ScriptSegment],
    *,
    registry: CharacterRegistryArtifact,
) -> tuple[list[ScriptSegment], list[dict[str, Any]], list[str]]:
    canonical_names = {record.canonical_name for record in registry.characters}
    allowed_keys = {*canonical_names, *RESERVED_REPLACEMENT_KEYS}
    alias_to_canonical = _stable_alias_map(registry)
    output: list[ScriptSegment] = []
    events: list[dict[str, Any]] = []
    errors: list[str] = []

    for segment in segments:
        speaker = segment.speaker
        if speaker in allowed_keys:
            output.append(segment)
            continue
        replacement = alias_to_canonical.get(speaker)
        if replacement is None:
            errors.append(f"{segment.segment_id}: {speaker}")
            output.append(segment)
            continue

        event = {
            "segment_id": segment.segment_id,
            "current_key": speaker,
            "decision": "replace",
            "replacement_key": replacement,
            "confidence": 1.0,
            "evidence": [
                f"characters.json lists {speaker!r} as a stable alias of {replacement!r}."
            ],
            "review_notes": ["Applied deterministic final speaker-key guard."],
            "status": "deterministic_alias_applied",
        }
        events.append(event)
        output.append(
            ScriptSegment(
                segment_id=segment.segment_id,
                source_span=segment.source_span,
                script={replacement: segment.text},
                raw_script_key=segment.raw_script_key or speaker,
                speaker_key_normalization=segment.speaker_key_normalization,
                speaker_key_review={
                    "from": speaker,
                    "to": replacement,
                    "decision": "deterministic_alias",
                    "confidence": 1.0,
                    "evidence": event["evidence"],
                    "review_notes": event["review_notes"],
                },
                confidence=segment.confidence,
                review_notes=segment.review_notes,
            )
        )
    return output, events, errors


def _stable_alias_map(registry: CharacterRegistryArtifact) -> dict[str, str]:
    alias_to_canonical: dict[str, str] = {}
    for record in registry.characters:
        for alias in record.stable_aliases:
            cleaned = alias.strip()
            if cleaned and cleaned != record.canonical_name:
                alias_to_canonical[cleaned] = record.canonical_name
    return alias_to_canonical


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
                "active_characters": contexts[chunk.chunk_id].context.active_characters,
                "aliases_observed": [
                    observation.model_dump()
                    for observation in contexts[chunk.chunk_id].context.aliases_observed
                ],
                "important_context": contexts[chunk.chunk_id].context.important_context,
                "unresolved_pronouns": [
                    pronoun.model_dump()
                    for pronoun in contexts[chunk.chunk_id].context.unresolved_pronouns
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


def _chunks_for_segment(
    segment: ScriptSegment, chunks: list[TextChunk]
) -> list[TextChunk]:
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
