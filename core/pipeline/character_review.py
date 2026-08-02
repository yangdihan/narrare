from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.models.character import CharacterRecord, CharacterRegistryArtifact
from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.voice import VoiceAssignment, VoiceAssignmentArtifact
from core.pipeline.script_assembly import COMPLETE_SCRIPT_CHUNK_ID
from core.pipeline.voice_assignment import build_voice_assignment_artifact
from storage.json_store import write_json
from storage.workspace import Workspace

RESERVED_SPEAKER_KEYS = {"narrator", "unknown_speaker"}


def add_character(
    project_id: str,
    name: str,
    *,
    workspace_root: str | Path = "data/interim",
) -> CharacterRegistryArtifact:
    canonical_name = name.strip()
    if not canonical_name:
        raise RuntimeError("character name is required")
    workspace = Workspace(project_id, root=workspace_root)
    registry = _read_registry(workspace)
    if _find_character(registry, canonical_name) is not None:
        raise RuntimeError(f"character already exists: {canonical_name}")

    registry.characters.append(
        CharacterRecord(
            character_id=_next_character_id(registry),
            canonical_name=canonical_name,
            stable_aliases=[canonical_name],
            confidence=1.0,
            review_notes=["Human-added character."],
        )
    )
    write_json(workspace.character_registry_path, registry)
    return registry


def rename_character(
    project_id: str,
    character_id: str,
    name: str,
    *,
    workspace_root: str | Path = "data/interim",
) -> CharacterRegistryArtifact:
    canonical_name = name.strip()
    if not canonical_name:
        raise RuntimeError("character name is required")

    workspace = Workspace(project_id, root=workspace_root)
    registry = _read_registry(workspace)
    character = _find_character(registry, character_id)
    if character is None:
        raise RuntimeError(f"character not found: {character_id}")
    existing = _find_character(registry, canonical_name)
    if existing is not None and existing.character_id != character.character_id:
        raise RuntimeError(f"character already exists: {canonical_name}")

    replacement_keys = _character_names(character)
    old_name = character.canonical_name
    renamed = character.model_copy(
        update={
            "canonical_name": canonical_name,
            "stable_aliases": _dedupe(
                [canonical_name, old_name, *character.stable_aliases]
            ),
            "aliases": _dedupe([canonical_name, old_name, *character.aliases]),
            "review_notes": _dedupe(
                [
                    *character.review_notes,
                    f"Human renamed {old_name} to {canonical_name}.",
                ]
            ),
        }
    )
    registry.characters = [
        renamed if item.character_id == character.character_id else item
        for item in registry.characters
    ]
    write_json(workspace.character_registry_path, registry)

    replacements = {key: canonical_name for key in replacement_keys}
    _rewrite_context_character_names(workspace, replacements)
    _rewrite_script_speakers(
        workspace,
        replacements,
        reason="manual_character_rename",
    )
    _refresh_voice_assignments(workspace, replacements)
    return registry


def merge_character(
    project_id: str,
    source_character_id: str,
    target_character_id: str,
    *,
    workspace_root: str | Path = "data/interim",
) -> CharacterRegistryArtifact:
    if source_character_id == target_character_id:
        raise RuntimeError("source and target characters must be different")

    workspace = Workspace(project_id, root=workspace_root)
    registry = _read_registry(workspace)
    source = _find_character(registry, source_character_id)
    target = _find_character(registry, target_character_id)
    if source is None:
        raise RuntimeError(f"source character not found: {source_character_id}")
    if target is None:
        raise RuntimeError(f"target character not found: {target_character_id}")

    replacement_keys = _character_names(source)
    merged_target = _merge_records(source, target)
    registry.characters = [
        merged_target if character.character_id == target.character_id else character
        for character in registry.characters
        if character.character_id != source.character_id
    ]
    write_json(workspace.character_registry_path, registry)

    replacements = {key: target.canonical_name for key in replacement_keys}
    _rewrite_context_character_names(workspace, replacements)
    _rewrite_script_speakers(
        workspace,
        replacements,
        reason="manual_character_merge",
    )
    _refresh_voice_assignments(workspace, replacements)
    return registry


def apply_character_edits(
    project_id: str,
    *,
    additions: list[str],
    renames: dict[str, str],
    merges: list[tuple[str, str]],
    workspace_root: str | Path = "data/interim",
) -> CharacterRegistryArtifact:
    for name in additions:
        add_character(project_id, name, workspace_root=workspace_root)
    for character_id, name in renames.items():
        rename_character(project_id, character_id, name, workspace_root=workspace_root)
    for source_character_id, target_character_id in merges:
        merge_character(
            project_id,
            source_character_id,
            target_character_id,
            workspace_root=workspace_root,
        )
    return _read_registry(Workspace(project_id, root=workspace_root))


def update_script_segment_speaker(
    project_id: str,
    segment_id: str,
    speaker: str,
    *,
    chunk_id: str | None = None,
    workspace_root: str | Path = "data/interim",
) -> ScriptArtifact:
    speaker_key = speaker.strip()
    if not speaker_key:
        raise RuntimeError("speaker is required")

    workspace = Workspace(project_id, root=workspace_root)
    _ensure_known_speaker(workspace, speaker_key)
    artifact_path = _select_script_artifact_path(workspace, chunk_id)
    artifact = _read_script_artifact(artifact_path)
    updated_segments = []
    updated = False

    for segment in artifact.segments:
        if segment.segment_id != segment_id:
            updated_segments.append(segment)
            continue
        if segment.speaker == speaker_key:
            updated_segments.append(segment)
            updated = True
            continue
        updated_segments.append(
            _replace_segment_speaker(segment, speaker_key, "manual_script_reassign")
        )
        updated = True

    if not updated:
        raise RuntimeError(f"segment not found: {segment_id}")

    updated_artifact = artifact.model_copy(update={"segments": updated_segments})
    write_json(artifact_path, updated_artifact)
    _refresh_voice_assignments(workspace, {})
    return updated_artifact


def apply_script_speaker_edits(
    project_id: str,
    edits: list[tuple[str, str, str | None]],
    *,
    workspace_root: str | Path = "data/interim",
) -> ScriptArtifact:
    if not edits:
        workspace = Workspace(project_id, root=workspace_root)
        return _read_script_artifact(_select_script_artifact_path(workspace, None))

    artifact: ScriptArtifact | None = None
    for segment_id, speaker, chunk_id in edits:
        artifact = update_script_segment_speaker(
            project_id,
            segment_id,
            speaker,
            chunk_id=chunk_id,
            workspace_root=workspace_root,
        )
    if artifact is None:
        raise RuntimeError("no script speaker edits to apply")
    return artifact


def speaker_options(
    project_id: str,
    *,
    workspace_root: str | Path = "data/interim",
) -> list[str]:
    workspace = Workspace(project_id, root=workspace_root)
    registry = _read_registry(workspace)
    names = [character.canonical_name for character in registry.characters]
    return [*sorted(RESERVED_SPEAKER_KEYS), *names]


def _read_registry(workspace: Workspace) -> CharacterRegistryArtifact:
    if not workspace.character_registry_path.exists():
        raise RuntimeError("character registry not found")
    return CharacterRegistryArtifact.model_validate_json(
        workspace.character_registry_path.read_text(encoding="utf-8")
    )


def _find_character(
    registry: CharacterRegistryArtifact,
    identifier: str,
) -> CharacterRecord | None:
    for character in registry.characters:
        if identifier in {character.character_id, character.canonical_name}:
            return character
    return None


def _next_character_id(registry: CharacterRegistryArtifact) -> str:
    used_numbers = []
    for character in registry.characters:
        match = re.fullmatch(r"character_(\d+)", character.character_id)
        if match:
            used_numbers.append(int(match.group(1)))
    return f"character_{(max(used_numbers) if used_numbers else 0) + 1:03d}"


def _merge_records(source: CharacterRecord, target: CharacterRecord) -> CharacterRecord:
    return target.model_copy(
        update={
            "stable_aliases": _dedupe(
                [
                    *target.stable_aliases,
                    source.canonical_name,
                    *source.stable_aliases,
                ]
            ),
            "contextual_references": [
                *target.contextual_references,
                *source.contextual_references,
            ],
            "aliases": _dedupe(
                [*target.aliases, source.canonical_name, *source.aliases]
            ),
            "alias_evidence": [*target.alias_evidence, *source.alias_evidence],
            "persona_summary": _join_notes(
                target.persona_summary, source.persona_summary
            ),
            "speaking_style": _join_notes(target.speaking_style, source.speaking_style),
            "age_impression": _join_notes(target.age_impression, source.age_impression),
            "voice_variant_notes": _dedupe(
                [*target.voice_variant_notes, *source.voice_variant_notes]
            ),
            "confidence": max(target.confidence, source.confidence),
            "review_notes": _dedupe(
                [
                    *target.review_notes,
                    *source.review_notes,
                    f"Human merged {source.canonical_name} into {target.canonical_name}.",
                ]
            ),
        }
    )


def _character_names(character: CharacterRecord) -> list[str]:
    return _dedupe(
        [character.canonical_name, *character.stable_aliases, *character.aliases]
    )


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            output.append(cleaned)
    return output


def _join_notes(left: str | None, right: str | None) -> str | None:
    values = _dedupe([value for value in [left, right] if value])
    return "；".join(values) if values else None


def _ensure_known_speaker(workspace: Workspace, speaker: str) -> None:
    if speaker in RESERVED_SPEAKER_KEYS:
        return
    registry = _read_registry(workspace)
    allowed = {character.canonical_name for character in registry.characters}
    if speaker not in allowed:
        raise RuntimeError(f"speaker must be a canonical character name: {speaker}")


def _rewrite_script_speakers(
    workspace: Workspace,
    replacements: dict[str, str],
    *,
    reason: str,
) -> None:
    if not replacements or not workspace.script_ir_dir.exists():
        return
    for path in sorted(workspace.script_ir_dir.rglob("*_script.json")):
        artifact = _try_read_script_artifact(path)
        if artifact is None:
            continue
        updated_segments = [
            _replace_segment_speaker(segment, replacements[segment.speaker], reason)
            if segment.speaker in replacements
            else segment
            for segment in artifact.segments
        ]
        if updated_segments != artifact.segments:
            write_json(path, artifact.model_copy(update={"segments": updated_segments}))


def _rewrite_context_character_names(
    workspace: Workspace,
    replacements: dict[str, str],
) -> None:
    if not replacements or not workspace.context_ir_dir.exists():
        return
    for path in sorted(workspace.context_ir_dir.glob("*_context.json")):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        context = artifact.get("context", {})
        active_characters = context.get("active_characters")
        if isinstance(active_characters, list):
            context["active_characters"] = [
                replacements.get(value, value) if isinstance(value, str) else value
                for value in active_characters
            ]
        for update in artifact.get("character_registry_updates", []):
            if not isinstance(update, dict):
                continue
            canonical_name = update.get("canonical_name")
            if isinstance(canonical_name, str) and canonical_name in replacements:
                update["canonical_name"] = replacements[canonical_name]
        write_json(path, artifact)


def _replace_segment_speaker(
    segment: ScriptSegment,
    speaker: str,
    reason: str,
) -> ScriptSegment:
    return segment.model_copy(
        update={
            "script": {speaker: segment.text},
            "raw_script_key": segment.raw_script_key or segment.speaker,
            "speaker_key_review": {
                "current_key": segment.speaker,
                "decision": "replace",
                "replacement_key": speaker,
                "confidence": 1.0,
                "evidence": ["Human review edit."],
                "review_notes": [reason],
            },
        }
    )


def _select_script_artifact_path(workspace: Workspace, chunk_id: str | None) -> Path:
    if chunk_id and chunk_id != "stitched_available_chunks":
        candidates = [
            workspace.key_reviewed_script_artifact_path(chunk_id),
            workspace.normalized_script_artifact_path(chunk_id),
            workspace.script_artifact_path(chunk_id),
        ]
    else:
        candidates = [
            workspace.key_reviewed_script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
            workspace.normalized_script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
            workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
        ]
    for path in candidates:
        if path.exists():
            return path
    if chunk_id in {None, "stitched_available_chunks"}:
        for path in sorted(workspace.script_ir_dir.glob("*_script.json")):
            if path.exists():
                return path
    raise RuntimeError("script artifact not found")


def _try_read_script_artifact(path: Path) -> ScriptArtifact | None:
    try:
        return _read_script_artifact(path)
    except (json.JSONDecodeError, ValueError, RuntimeError):
        return None


def _read_script_artifact(path: Path) -> ScriptArtifact:
    if not path.exists():
        raise RuntimeError(f"script artifact not found: {path}")
    return ScriptArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _refresh_voice_assignments(
    workspace: Workspace,
    replacements: dict[str, str],
) -> None:
    if not workspace.voice_assignments_path.exists():
        return
    previous = VoiceAssignmentArtifact.model_validate_json(
        workspace.voice_assignments_path.read_text(encoding="utf-8")
    )
    previous_by_speaker = _merged_previous_assignments(previous, replacements)
    refreshed = build_voice_assignment_artifact(
        workspace.project_id,
        workspace_root=workspace.root,
    )
    assignments = [
        _restore_assignment_state(
            assignment, previous_by_speaker.get(assignment.speaker)
        )
        for assignment in refreshed.assignments
    ]
    write_json(
        workspace.voice_assignments_path,
        refreshed.model_copy(
            update={
                "updated_at": datetime.now(timezone.utc),
                "assignments": assignments,
            }
        ),
    )


def _merged_previous_assignments(
    previous: VoiceAssignmentArtifact,
    replacements: dict[str, str],
) -> dict[str, VoiceAssignment]:
    merged: dict[str, VoiceAssignment] = {}
    for assignment in previous.assignments:
        speaker = replacements.get(assignment.speaker, assignment.speaker)
        existing = merged.get(speaker)
        if existing is None or (
            not existing.voice_profile_id and assignment.voice_profile_id
        ):
            merged[speaker] = assignment.model_copy(update={"speaker": speaker})
    return merged


def _restore_assignment_state(
    assignment: VoiceAssignment,
    previous: VoiceAssignment | None,
) -> VoiceAssignment:
    if previous is None:
        return assignment
    return assignment.model_copy(
        update={
            "voice_profile_id": previous.voice_profile_id,
            "sample_take_path": previous.sample_take_path,
            "confirmed": previous.confirmed,
        }
    )
