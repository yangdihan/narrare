from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from core.ir.script_builder import split_long_script_segments
from core.models.chunk import ChunksArtifact
from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.source import SourceSpan
from core.validation.script_integrity import sha256_text, validate_script_segments
from storage.json_store import write_json
from storage.workspace import Workspace

COMPLETE_SCRIPT_CHUNK_ID = "complete"


@dataclass(frozen=True)
class ScriptAssemblyResult:
    workspace: Workspace
    artifact: ScriptArtifact
    validation_report_path: Path
    boundary_merge_count: int
    preserved_segment_id_count: int
    new_segment_id_count: int
    exact_reconstruction_success: bool
    errors: list[str]


def run_script_assembly_workflow(
    project_id: str,
    *,
    workspace_root: str | Path = "data/interim",
) -> ScriptAssemblyResult:
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()

    chunks_artifact = _read_chunks_artifact(workspace.chunks_path)
    previous_complete_artifact = _read_optional_script_artifact(
        workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID)
    )
    source_parts: list[str] = []
    assembled_segments: list[ScriptSegment] = []
    boundary_merge_count = 0
    source_offset = 0
    provider: str | None = None
    model: str | None = None

    for chunk in chunks_artifact.chunks:
        artifact_path = workspace.script_artifact_path(chunk.chunk_id)
        if not artifact_path.exists():
            raise RuntimeError(
                f"Missing Stage 2 script artifact for {chunk.chunk_id}: {artifact_path}"
            )

        chunk_artifact = _read_script_artifact(artifact_path)
        if chunk_artifact.project_id != project_id:
            raise RuntimeError(
                f"{artifact_path} project_id={chunk_artifact.project_id!r}, "
                f"expected {project_id!r}"
            )
        if chunk_artifact.chunk_id != chunk.chunk_id:
            raise RuntimeError(
                f"{artifact_path} chunk_id={chunk_artifact.chunk_id!r}, "
                f"expected {chunk.chunk_id!r}"
            )
        if chunk_artifact.chunk_sha256 != sha256_text(chunk.text):
            raise RuntimeError(f"{artifact_path} does not match current chunk text")

        provider = provider or chunk_artifact.llm_provider
        model = model or chunk_artifact.llm_model
        shifted_segments = [
            _shift_segment(segment, source_offset)
            for segment in chunk_artifact.segments
        ]
        if assembled_segments and shifted_segments:
            last_segment = assembled_segments[-1]
            first_segment = shifted_segments[0]
            if last_segment.speaker == first_segment.speaker:
                assembled_segments[-1] = _merge_boundary_segments(
                    last_segment,
                    first_segment,
                )
                boundary_merge_count += 1
                shifted_segments = shifted_segments[1:]

        assembled_segments.extend(shifted_segments)
        source_parts.append(chunk.text)
        source_offset += len(chunk.text)

    complete_source = "".join(source_parts)
    previous_segments = (
        previous_complete_artifact.segments if previous_complete_artifact else []
    )
    previous_segment_ids = {segment.segment_id for segment in previous_segments}
    assembled_segments = _assign_stable_segment_ids(
        split_long_script_segments(assembled_segments, source_text=complete_source),
        previous_segments=previous_segments,
    )
    preserved_segment_id_count = sum(
        segment.segment_id in previous_segment_ids for segment in assembled_segments
    )
    complete_artifact = ScriptArtifact(
        project_id=project_id,
        chunk_id=COMPLETE_SCRIPT_CHUNK_ID,
        chunk_source_path=str(workspace.chunks_path),
        chunk_sha256=sha256_text(complete_source),
        llm_provider=provider or "unknown",
        llm_model=model or "unknown",
        response_source="assembled",
        processed_chunk_count=len(chunks_artifact.chunks),
        segments=assembled_segments,
    )
    validation_report = validate_script_segments(
        project_id=project_id,
        chunk_id=COMPLETE_SCRIPT_CHUNK_ID,
        source_text=complete_source,
        segments=complete_artifact.segments,
    )

    write_json(workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID), complete_artifact)
    validation_report_path = workspace.script_validation_report_path(
        COMPLETE_SCRIPT_CHUNK_ID
    )
    write_json(validation_report_path, validation_report)

    if not validation_report.exact_reconstruction_success:
        raise RuntimeError(
            "Complete script validation failed: "
            + "; ".join(validation_report.errors)
        )

    return ScriptAssemblyResult(
        workspace=workspace,
        artifact=complete_artifact,
        validation_report_path=validation_report_path,
        boundary_merge_count=boundary_merge_count,
        preserved_segment_id_count=preserved_segment_id_count,
        new_segment_id_count=len(assembled_segments) - preserved_segment_id_count,
        exact_reconstruction_success=validation_report.exact_reconstruction_success,
        errors=validation_report.errors,
    )


def _read_chunks_artifact(path: Path) -> ChunksArtifact:
    return ChunksArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _read_script_artifact(path: Path) -> ScriptArtifact:
    return ScriptArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _read_optional_script_artifact(path: Path) -> ScriptArtifact | None:
    return _read_script_artifact(path) if path.exists() else None


def _shift_segment(segment: ScriptSegment, offset: int) -> ScriptSegment:
    return ScriptSegment(
        segment_id=segment.segment_id,
        source_span=SourceSpan(
            start=segment.source_span.start + offset,
            end=segment.source_span.end + offset,
        ),
        script=segment.script,
        confidence=segment.confidence,
        review_notes=segment.review_notes,
    )


def _merge_boundary_segments(
    left: ScriptSegment,
    right: ScriptSegment,
) -> ScriptSegment:
    return ScriptSegment(
        segment_id=left.segment_id,
        source_span=SourceSpan(
            start=left.source_span.start,
            end=right.source_span.end,
        ),
        script={left.speaker: left.text + right.text},
        confidence=min(left.confidence, right.confidence),
        review_notes=[
            *left.review_notes,
            *right.review_notes,
            "Merged deterministic chunk-boundary same-speaker segments.",
        ],
    )


def _assign_stable_segment_ids(
    segments: list[ScriptSegment],
    *,
    previous_segments: list[ScriptSegment],
) -> list[ScriptSegment]:
    previous_ids_by_fingerprint: dict[str, list[str]] = {}
    for segment in previous_segments:
        previous_ids_by_fingerprint.setdefault(_segment_fingerprint(segment), []).append(
            segment.segment_id
        )

    assigned: list[ScriptSegment] = []
    used_ids: set[str] = set()
    for segment in segments:
        matching_ids = previous_ids_by_fingerprint.get(_segment_fingerprint(segment), [])
        segment_id = next(
            (candidate for candidate in matching_ids if candidate not in used_ids),
            _new_segment_id(segment, used_ids),
        )
        used_ids.add(segment_id)
        assigned.append(segment.model_copy(update={"segment_id": segment_id}))
    return assigned


def _segment_fingerprint(segment: ScriptSegment) -> str:
    payload = {
        "source_span": segment.source_span.model_dump(),
        "speaker": segment.speaker,
        "text": segment.text,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _new_segment_id(segment: ScriptSegment, used_ids: set[str]) -> str:
    digest = hashlib.sha256(_segment_fingerprint(segment).encode("utf-8")).hexdigest()[:12]
    base = f"seg_{segment.source_span.start:08d}_{digest}"
    segment_id = base
    collision = 2
    while segment_id in used_ids:
        segment_id = f"{base}_{collision}"
        collision += 1
    return segment_id
