from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.models.character import CharacterRecord, CharacterRegistryArtifact
from core.models.chunk import ChunksArtifact
from core.models.ir import ScriptArtifact, ScriptSegment
from core.pipeline.script_assembly import COMPLETE_SCRIPT_CHUNK_ID
from core.validation.script_integrity import validate_script_segments
from storage.json_store import write_json
from storage.workspace import Workspace


RESERVED_SPEAKERS = {"narrator", "unknown_speaker"}
DEFAULT_ALIAS_CONFIDENCE_THRESHOLD = 0.85


@dataclass(frozen=True)
class SpeakerKeyNormalizationResult:
    workspace: Workspace
    artifact: ScriptArtifact
    report_path: Path
    renamed_count: int
    unresolved_count: int
    exact_reconstruction_success: bool
    errors: list[str]


def run_speaker_key_normalization_workflow(
    project_id: str,
    *,
    chunk_id: str = COMPLETE_SCRIPT_CHUNK_ID,
    workspace_root: str | Path = "data/interim",
    alias_confidence_threshold: float = DEFAULT_ALIAS_CONFIDENCE_THRESHOLD,
) -> SpeakerKeyNormalizationResult:
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()

    script_artifact = _read_script_artifact(workspace.script_artifact_path(chunk_id))
    registry = _read_registry(workspace.character_registry_path, project_id)
    alias_index, ambiguous_aliases = _build_alias_index(
        registry.characters,
        alias_confidence_threshold=alias_confidence_threshold,
    )

    normalized_segments: list[ScriptSegment] = []
    renamed: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []

    for segment in script_artifact.segments:
        normalized, event = _normalize_segment(
            segment,
            alias_index=alias_index,
            ambiguous_aliases=ambiguous_aliases,
        )
        normalized_segments.append(normalized)
        if event is None:
            continue
        if event["status"] == "renamed":
            renamed.append(event)
        else:
            unresolved.append(event)

    normalized_artifact = ScriptArtifact(
        project_id=script_artifact.project_id,
        chunk_id=script_artifact.chunk_id,
        chunk_source_path=script_artifact.chunk_source_path,
        chunk_sha256=script_artifact.chunk_sha256,
        llm_provider=script_artifact.llm_provider,
        llm_model=script_artifact.llm_model,
        response_source=script_artifact.response_source,
        processed_chunk_count=script_artifact.processed_chunk_count,
        segments=normalized_segments,
    )
    source_text = _complete_source_text(workspace)
    validation_report = validate_script_segments(
        project_id=project_id,
        chunk_id=chunk_id,
        source_text=source_text,
        segments=normalized_segments,
    )
    report = {
        "project_id": project_id,
        "chunk_id": chunk_id,
        "alias_confidence_threshold": alias_confidence_threshold,
        "renamed_count": len(renamed),
        "unresolved_count": len(unresolved),
        "renamed": renamed,
        "unresolved": unresolved,
        "validation": validation_report.model_dump(),
    }

    output_path = workspace.normalized_script_artifact_path(chunk_id)
    report_path = workspace.speaker_key_normalization_report_path(chunk_id)
    write_json(output_path, normalized_artifact)
    write_json(report_path, report)

    if not validation_report.exact_reconstruction_success:
        raise RuntimeError(
            "Normalized script validation failed: "
            + "; ".join(validation_report.errors)
        )

    return SpeakerKeyNormalizationResult(
        workspace=workspace,
        artifact=normalized_artifact,
        report_path=report_path,
        renamed_count=len(renamed),
        unresolved_count=len(unresolved),
        exact_reconstruction_success=validation_report.exact_reconstruction_success,
        errors=validation_report.errors,
    )


def _normalize_segment(
    segment: ScriptSegment,
    *,
    alias_index: dict[str, dict[str, object]],
    ambiguous_aliases: set[str],
) -> tuple[ScriptSegment, dict[str, object] | None]:
    raw_key = segment.speaker
    lookup_key = raw_key.strip()
    if lookup_key in RESERVED_SPEAKERS:
        return segment, None

    if lookup_key in ambiguous_aliases:
        return segment, _event(segment, "ambiguous_alias", raw_key, None)

    match = alias_index.get(lookup_key)
    if match is None:
        return segment, _event(segment, "unknown_alias", raw_key, None)

    canonical_name = str(match["canonical_name"])
    if raw_key == canonical_name:
        return segment, None

    normalization = {
        "from": raw_key,
        "to": canonical_name,
        "source": "character_registry",
        "character_id": match["character_id"],
        "confidence": match["confidence"],
    }
    return (
        ScriptSegment(
            segment_id=segment.segment_id,
            source_span=segment.source_span,
            script={canonical_name: segment.text},
            raw_script_key=raw_key,
            speaker_key_normalization=normalization,
            speaker_key_review=segment.speaker_key_review,
            confidence=segment.confidence,
            review_notes=segment.review_notes,
        ),
        _event(segment, "renamed", raw_key, canonical_name, normalization),
    )


def _event(
    segment: ScriptSegment,
    status: str,
    raw_key: str,
    normalized_key: str | None,
    normalization: dict[str, object] | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "status": status,
        "segment_id": segment.segment_id,
        "raw_script_key": raw_key,
    }
    if normalized_key is not None:
        event["normalized_script_key"] = normalized_key
    if normalization is not None:
        event["normalization"] = normalization
    return event


def _build_alias_index(
    records: list[CharacterRecord],
    *,
    alias_confidence_threshold: float,
) -> tuple[dict[str, dict[str, object]], set[str]]:
    candidates: dict[str, list[dict[str, object]]] = {}
    for record in records:
        for alias in _record_aliases(record):
            if not _is_global_stable_reference(alias):
                continue
            confidence = _alias_confidence(record, alias)
            if confidence < alias_confidence_threshold:
                continue
            candidates.setdefault(alias, []).append(
                {
                    "character_id": record.character_id,
                    "canonical_name": record.canonical_name,
                    "confidence": confidence,
                }
            )

    alias_index: dict[str, dict[str, object]] = {}
    ambiguous_aliases: set[str] = set()
    for alias, matches in candidates.items():
        character_ids = {str(match["character_id"]) for match in matches}
        if len(character_ids) > 1:
            ambiguous_aliases.add(alias)
            continue
        alias_index[alias] = matches[0]
    return alias_index, ambiguous_aliases


def _record_aliases(record: CharacterRecord) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for alias in [record.canonical_name, *record.stable_aliases]:
        cleaned = alias.strip()
        if not cleaned or cleaned in seen:
            continue
        aliases.append(cleaned)
        seen.add(cleaned)
    return aliases


def _alias_confidence(record: CharacterRecord, alias: str) -> float:
    scores = [
        evidence.confidence
        for evidence in record.alias_evidence
        if evidence.alias.strip() == alias and evidence.reference_type == "stable_name"
    ]
    if alias == record.canonical_name:
        scores.append(record.confidence)
    return max(scores) if scores else record.confidence


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


def _read_script_artifact(path: Path) -> ScriptArtifact:
    if not path.exists():
        raise RuntimeError(f"Missing script artifact: {path}")
    return ScriptArtifact.model_validate_json(path.read_text(encoding="utf-8"))


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


def _complete_source_text(workspace: Workspace) -> str:
    chunks = ChunksArtifact.model_validate_json(
        workspace.chunks_path.read_text(encoding="utf-8")
    )
    return "".join(chunk.text for chunk in chunks.chunks)
