from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.models.character import (
    CharacterCurationAddition,
    CharacterCurationUpdate,
    CharacterRecord,
    CharacterRegistryArtifact,
)
from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.source import SourceSpan
from core.models.voice import VoiceAssignment, VoiceAssignmentArtifact
from core.pipeline.script_artifact_selection import preferred_script_artifact_paths
from core.pipeline.script_assembly import COMPLETE_SCRIPT_CHUNK_ID
from core.pipeline.voice_assignment import (
    build_voice_assignment_artifact,
    save_voice_assignments,
)
from core.validation.script_integrity import (
    normalize_content_text,
    validate_script_segments,
)
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


def merge_script_speaker_key(
    project_id: str,
    source_speaker: str,
    target_character_id: str,
    *,
    workspace_root: str | Path = "data/interim",
) -> CharacterRegistryArtifact:
    """Merge an unregistered Script speaker key into a curated character."""
    source = source_speaker.strip()
    if not source:
        raise RuntimeError("script speaker key is required")
    if source in RESERVED_SPEAKER_KEYS:
        raise RuntimeError("a system speaker key cannot be merged")

    workspace = Workspace(project_id, root=workspace_root)
    registry = _read_registry(workspace)
    target = _find_character(registry, target_character_id)
    if target is None:
        raise RuntimeError(f"character not found: {target_character_id}")
    if source == target.canonical_name:
        return registry

    merged_target = target.model_copy(
        update={
            "stable_aliases": _dedupe(
                [target.canonical_name, source, *target.stable_aliases]
            ),
            "review_notes": _dedupe(
                [
                    *target.review_notes,
                    f"Human merged Script speaker key {source} into this character.",
                ]
            ),
        }
    )
    registry.characters = [
        merged_target if character.character_id == target.character_id else character
        for character in registry.characters
    ]
    write_json(workspace.character_registry_path, registry)

    replacements = {source: target.canonical_name}
    _rewrite_context_character_names(workspace, replacements)
    _rewrite_script_speakers(
        workspace,
        replacements,
        reason="manual_script_speaker_merge",
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


def apply_character_curation(
    project_id: str,
    *,
    additions: list[CharacterCurationAddition],
    updates: list[CharacterCurationUpdate],
    removals: list[str],
    merges: list[tuple[str, str]],
    script_speaker_merges: list[tuple[str, str]],
    voice_profile_by_character_id: dict[str, str],
    system_voice_assignments: dict[str, str],
    workspace_root: str | Path = "data/interim",
) -> CharacterRegistryArtifact:
    """Apply the human-approved registry and synchronize its dependent artifacts."""
    workspace = Workspace(project_id, root=workspace_root)
    registry = _read_registry(workspace)
    _validate_character_curation(
        registry,
        additions=additions,
        updates=updates,
        removals=removals,
        merges=merges,
        script_speaker_merges=script_speaker_merges,
        voice_profile_by_character_id=voice_profile_by_character_id,
        system_voice_assignments=system_voice_assignments,
    )
    removal_ids = set(removals)

    addition_updates: list[CharacterCurationUpdate] = []
    for addition in additions:
        canonical_name = addition.canonical_name.strip()
        registry = add_character(
            project_id,
            canonical_name,
            workspace_root=workspace_root,
        )
        character = _find_character(registry, canonical_name)
        if character is None:  # Defensive: add_character must return the newly saved record.
            raise RuntimeError(f"character not found after creation: {canonical_name}")
        addition_updates.append(
            CharacterCurationUpdate(
                character_id=character.character_id,
                canonical_name=canonical_name,
                stable_aliases=addition.stable_aliases,
                persona_summary=addition.persona_summary,
                speaking_style=addition.speaking_style,
                age_impression=addition.age_impression,
                voice_variant_notes=addition.voice_variant_notes,
            )
        )

    for update in updates:
        if update.character_id in removal_ids:
            raise RuntimeError("a removed character cannot also be edited")
        current = _read_registry(workspace)
        existing = _find_character(current, update.character_id)
        if existing is None:
            raise RuntimeError(f"character not found: {update.character_id}")
        if existing.canonical_name != update.canonical_name.strip():
            rename_character(
                project_id,
                update.character_id,
                update.canonical_name,
                workspace_root=workspace_root,
            )

    _apply_character_metadata(
        project_id,
        [*addition_updates, *updates],
        workspace_root=workspace_root,
    )

    for source_character_id, target_character_id in merges:
        if source_character_id in removal_ids or target_character_id in removal_ids:
            raise RuntimeError("a removed character cannot be part of a merge")
        merge_character(
            project_id,
            source_character_id,
            target_character_id,
            workspace_root=workspace_root,
        )
    for character_id in removals:
        remove_character(project_id, character_id, workspace_root=workspace_root)
    for source_speaker, target_character_id in script_speaker_merges:
        merge_script_speaker_key(
            project_id,
            source_speaker,
            target_character_id,
            workspace_root=workspace_root,
        )

    registry = _read_registry(workspace)
    _save_curated_voice_assignments(
        project_id,
        registry,
        voice_profile_by_character_id,
        system_voice_assignments,
        workspace_root=workspace_root,
    )
    return registry


def _validate_character_curation(
    registry: CharacterRegistryArtifact,
    *,
    additions: list[CharacterCurationAddition],
    updates: list[CharacterCurationUpdate],
    removals: list[str],
    merges: list[tuple[str, str]],
    script_speaker_merges: list[tuple[str, str]],
    voice_profile_by_character_id: dict[str, str],
    system_voice_assignments: dict[str, str],
) -> None:
    """Reject a conflicting batch before any registry-dependent artifact is written."""
    characters_by_id = {character.character_id: character for character in registry.characters}
    if len(characters_by_id) != len(registry.characters):
        raise RuntimeError("character registry contains duplicate character IDs")

    removal_ids = set(removals)
    if len(removal_ids) != len(removals):
        raise RuntimeError("a character can only be removed once")
    missing_removals = removal_ids.difference(characters_by_id)
    if missing_removals:
        raise RuntimeError(f"character not found: {min(missing_removals)}")

    updates_by_id = {update.character_id: update for update in updates}
    if len(updates_by_id) != len(updates):
        raise RuntimeError("a character can only be edited once")
    for character_id, update in updates_by_id.items():
        if character_id not in characters_by_id:
            raise RuntimeError(f"character not found: {character_id}")
        if character_id in removal_ids:
            raise RuntimeError("a removed character cannot also be edited")
        if not update.canonical_name.strip():
            raise RuntimeError("character name is required")

    current_names = {
        character.canonical_name: character.character_id for character in registry.characters
    }
    updated_names: set[str] = set()
    for character_id, update in updates_by_id.items():
        canonical_name = update.canonical_name.strip()
        existing_id = current_names.get(canonical_name)
        if existing_id is not None and existing_id != character_id:
            raise RuntimeError(f"character already exists: {canonical_name}")
        if canonical_name in updated_names:
            raise RuntimeError(f"character already exists: {canonical_name}")
        updated_names.add(canonical_name)

    addition_names: set[str] = set()
    for addition in additions:
        canonical_name = addition.canonical_name.strip()
        if not canonical_name:
            raise RuntimeError("character name is required")
        if canonical_name in current_names or canonical_name in addition_names:
            raise RuntimeError(f"character already exists: {canonical_name}")
        addition_names.add(canonical_name)

    merge_sources: set[str] = set()
    merge_targets: set[str] = set()
    for source_character_id, target_character_id in merges:
        if source_character_id == target_character_id:
            raise RuntimeError("a character cannot be merged into itself")
        if source_character_id not in characters_by_id:
            raise RuntimeError(f"character not found: {source_character_id}")
        if target_character_id not in characters_by_id:
            raise RuntimeError(f"character not found: {target_character_id}")
        if source_character_id in removal_ids or target_character_id in removal_ids:
            raise RuntimeError("a removed character cannot be part of a merge")
        if source_character_id in merge_sources:
            raise RuntimeError("a character can only be merged once")
        if (
            source_character_id in merge_targets
            or target_character_id in merge_sources
        ):
            raise RuntimeError("a merge target cannot be merged in the same decision set")
        merge_sources.add(source_character_id)
        merge_targets.add(target_character_id)

    final_character_ids = set(characters_by_id).difference(removal_ids, merge_sources)
    script_speaker_sources: set[str] = set()
    for source_speaker, target_character_id in script_speaker_merges:
        normalized_source = source_speaker.strip()
        if not normalized_source:
            raise RuntimeError("script speaker key is required")
        if normalized_source in RESERVED_SPEAKER_KEYS:
            raise RuntimeError("a system speaker key cannot be merged")
        if normalized_source in script_speaker_sources:
            raise RuntimeError("a script speaker key can only be merged once")
        if target_character_id not in final_character_ids:
            raise RuntimeError(f"character not found: {target_character_id}")
        script_speaker_sources.add(normalized_source)
    for character_id in voice_profile_by_character_id:
        if character_id not in final_character_ids:
            raise RuntimeError(f"character not found: {character_id}")
    for speaker in system_voice_assignments:
        if speaker not in RESERVED_SPEAKER_KEYS:
            raise RuntimeError(f"not a system speaker: {speaker}")


def _apply_character_metadata(
    project_id: str,
    updates: list[CharacterCurationUpdate],
    *,
    workspace_root: str | Path,
) -> None:
    if not updates:
        return
    workspace = Workspace(project_id, root=workspace_root)
    registry = _read_registry(workspace)
    updates_by_id = {update.character_id: update for update in updates}
    if len(updates_by_id) != len(updates):
        raise RuntimeError("a character can only be edited once")
    characters = []
    for character in registry.characters:
        update = updates_by_id.get(character.character_id)
        if update is None:
            characters.append(character)
            continue
        canonical_name = update.canonical_name.strip()
        if not canonical_name:
            raise RuntimeError("character name is required")
        characters.append(
            character.model_copy(
                update={
                    "stable_aliases": _dedupe(
                        [canonical_name, *update.stable_aliases]
                    ),
                    "persona_summary": _optional_text(update.persona_summary),
                    "speaking_style": _optional_text(update.speaking_style),
                    "age_impression": _optional_text(update.age_impression),
                    "voice_variant_notes": _dedupe(update.voice_variant_notes),
                    "review_notes": _dedupe(
                        [
                            *character.review_notes,
                            "Human reviewed in Characters panel.",
                        ]
                    ),
                }
            )
        )
    write_json(
        workspace.character_registry_path,
        registry.model_copy(update={"characters": characters}),
    )


def remove_character(
    project_id: str,
    character_id: str,
    *,
    workspace_root: str | Path = "data/interim",
) -> CharacterRegistryArtifact:
    workspace = Workspace(project_id, root=workspace_root)
    registry = _read_registry(workspace)
    character = _find_character(registry, character_id)
    if character is None:
        raise RuntimeError(f"character not found: {character_id}")
    replacements = {key: "unknown_speaker" for key in _character_names(character)}
    updated = registry.model_copy(
        update={
            "characters": [
                item for item in registry.characters if item.character_id != character_id
            ]
        }
    )
    write_json(workspace.character_registry_path, updated)
    _rewrite_context_character_names(workspace, replacements)
    _rewrite_script_speakers(
        workspace,
        replacements,
        reason="manual_character_removal",
    )
    _refresh_voice_assignments(workspace, {})
    return updated


def _save_curated_voice_assignments(
    project_id: str,
    registry: CharacterRegistryArtifact,
    voice_profile_by_character_id: dict[str, str],
    system_voice_assignments: dict[str, str],
    *,
    workspace_root: str | Path,
) -> None:
    workspace = Workspace(project_id, root=workspace_root)
    if not workspace.script_ir_dir.exists():
        return
    try:
        assignments = build_voice_assignment_artifact(
            project_id,
            workspace_root=workspace_root,
        )
    except RuntimeError:
        return
    profile_by_speaker = {
        assignment.speaker: assignment.voice_profile_id or ""
        for assignment in assignments.assignments
    }
    character_by_id = {character.character_id: character for character in registry.characters}
    for character_id, profile_id in voice_profile_by_character_id.items():
        character = character_by_id.get(character_id)
        if character is None:
            raise RuntimeError(f"character not found: {character_id}")
        profile_by_speaker[character.canonical_name] = profile_id
    for speaker, profile_id in system_voice_assignments.items():
        if speaker not in RESERVED_SPEAKER_KEYS:
            raise RuntimeError(f"not a system speaker: {speaker}")
        profile_by_speaker[speaker] = profile_id
    save_voice_assignments(
        project_id,
        profile_by_speaker,
        workspace_root=workspace_root,
    )


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


def apply_script_content_edits(
    project_id: str,
    updates: list[tuple[str, str, str, str | None]],
    inserts: list[tuple[str | None, str, str, str | None]],
    *,
    workspace_root: str | Path = "data/interim",
) -> ScriptArtifact:
    """Apply human-reviewed script text changes without renumbering existing segments."""
    if not updates and not inserts:
        workspace = Workspace(project_id, root=workspace_root)
        artifact_path = _select_script_artifact_path(workspace, None)
        artifact = _read_script_artifact(artifact_path)
        reconciled = _rebuild_source_spans_if_reconstructs(
            artifact.segments,
            _source_text_for_script_artifact(workspace, artifact),
        )
        if reconciled != artifact.segments:
            artifact = artifact.model_copy(update={"segments": reconciled})
            write_json(artifact_path, artifact)
        _write_script_validation_report(workspace, artifact)
        _refresh_voice_assignments(workspace, {})
        return artifact

    workspace = Workspace(project_id, root=workspace_root)
    edits_by_chunk: dict[str | None, list[tuple[str, str, str]]] = {}
    inserts_by_chunk: dict[str | None, list[tuple[str | None, str, str]]] = {}
    for segment_id, speaker, text, chunk_id in updates:
        edits_by_chunk.setdefault(chunk_id, []).append((segment_id, speaker, text))
    for after_segment_id, speaker, text, chunk_id in inserts:
        inserts_by_chunk.setdefault(chunk_id, []).append((after_segment_id, speaker, text))

    artifact: ScriptArtifact | None = None
    for chunk_id in dict.fromkeys([*edits_by_chunk, *inserts_by_chunk]):
        artifact_path = _select_script_artifact_path(workspace, chunk_id)
        current = _read_script_artifact(artifact_path)
        updated = _apply_content_edits_to_artifact(
            workspace,
            current,
            edits_by_chunk.get(chunk_id, []),
            inserts_by_chunk.get(chunk_id, []),
        )
        write_json(artifact_path, updated)
        _write_script_validation_report(workspace, updated)
        artifact = updated

    _refresh_voice_assignments(workspace, {})
    if artifact is None:
        raise RuntimeError("no script edits to apply")
    return artifact


def _apply_content_edits_to_artifact(
    workspace: Workspace,
    artifact: ScriptArtifact,
    updates: list[tuple[str, str, str]],
    inserts: list[tuple[str | None, str, str]],
) -> ScriptArtifact:
    updates_by_segment_id: dict[str, tuple[str, str]] = {}
    for segment_id, speaker, text in updates:
        _ensure_known_speaker(workspace, speaker)
        _ensure_script_text(text)
        if segment_id in updates_by_segment_id:
            raise RuntimeError(f"segment edited more than once: {segment_id}")
        updates_by_segment_id[segment_id] = (speaker, text)

    segment_ids = {segment.segment_id for segment in artifact.segments}
    unknown_ids = sorted(set(updates_by_segment_id) - segment_ids)
    if unknown_ids:
        raise RuntimeError(f"segment not found: {unknown_ids[0]}")

    inserts_after: dict[str | None, list[ScriptSegment]] = {}
    for after_segment_id, speaker, text in inserts:
        _ensure_known_speaker(workspace, speaker)
        _ensure_script_text(text)
        if after_segment_id is not None and after_segment_id not in segment_ids:
            raise RuntimeError(f"insert location not found: {after_segment_id}")
        anchor = next(
            (segment for segment in artifact.segments if segment.segment_id == after_segment_id),
            None,
        )
        position = anchor.source_span.end if anchor is not None else 0
        inserts_after.setdefault(after_segment_id, []).append(
            ScriptSegment(
                segment_id=f"manual_{uuid.uuid4().hex}",
                source_span=SourceSpan(start=position, end=position),
                script={speaker: text},
                raw_script_key=speaker,
                confidence=1.0,
                review_notes=["Human-inserted script segment."],
            )
        )

    updated_segments = [*inserts_after.get(None, [])]
    for segment in artifact.segments:
        speaker, text = updates_by_segment_id.get(
            segment.segment_id, (segment.speaker, segment.text)
        )
        if (speaker, text) == (segment.speaker, segment.text):
            updated_segments.append(segment)
        else:
            updated_segments.append(
                segment.model_copy(
                    update={
                        "script": {speaker: text},
                        "raw_script_key": segment.raw_script_key or segment.speaker,
                        "speaker_key_review": {
                            "current_key": segment.speaker,
                            "decision": "replace" if speaker != segment.speaker else "keep",
                            "replacement_key": speaker if speaker != segment.speaker else None,
                            "confidence": 1.0,
                            "evidence": ["Human review edit."],
                            "review_notes": ["manual_script_content_edit"],
                        },
                        "review_notes": [
                            *segment.review_notes,
                            "Human-edited script content.",
                        ],
                    }
                )
            )
        updated_segments.extend(inserts_after.get(segment.segment_id, []))

    updated_segments = _rebuild_source_spans_if_reconstructs(
        updated_segments,
        _source_text_for_script_artifact(workspace, artifact),
    )
    return artifact.model_copy(update={"segments": updated_segments})


def _ensure_script_text(text: str) -> None:
    if not text:
        raise RuntimeError("script text is required")


def _rebuild_source_spans_if_reconstructs(
    segments: list[ScriptSegment],
    source_text: str,
) -> list[ScriptSegment]:
    if normalize_content_text("".join(segment.text for segment in segments)) != normalize_content_text(source_text):
        return segments

    source_content_offsets = [
        index for index, char in enumerate(source_text) if char.isalnum()
    ]
    source_content_position = 0
    source_position = 0
    output = []
    for segment in segments:
        content_length = len(normalize_content_text(segment.text))
        if not content_length:
            output.append(segment)
            continue
        source_content_position += content_length
        end = source_content_offsets[source_content_position - 1] + 1
        output.append(
            segment.model_copy(
                update={"source_span": SourceSpan(start=source_position, end=end)}
            )
        )
        source_position = end
    return output


def _write_script_validation_report(
    workspace: Workspace,
    artifact: ScriptArtifact,
) -> None:
    source_text = _source_text_for_script_artifact(workspace, artifact)
    report = validate_script_segments(
        project_id=workspace.project_id,
        chunk_id=artifact.chunk_id,
        source_text=source_text,
        segments=artifact.segments,
    )
    write_json(workspace.script_validation_report_path(artifact.chunk_id), report)


def _source_text_for_script_artifact(
    workspace: Workspace,
    artifact: ScriptArtifact,
) -> str:
    if artifact.chunk_id == COMPLETE_SCRIPT_CHUNK_ID:
        if not workspace.chunks_path.exists():
            raise RuntimeError("chunks manifest not found")
        chunks = json.loads(workspace.chunks_path.read_text(encoding="utf-8"))
        return "".join(str(chunk.get("text", "")) for chunk in chunks.get("chunks", []))

    source_path = Path(artifact.chunk_source_path)
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    if not source_path.exists():
        raise RuntimeError(f"script source text not found: {source_path}")
    return source_path.read_text(encoding="utf-8")


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


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _join_notes(left: str | None, right: str | None) -> str | None:
    values = _dedupe([value for value in [left, right] if value])
    return "；".join(values) if values else None


def _ensure_known_speaker(workspace: Workspace, speaker: str) -> None:
    if speaker in RESERVED_SPEAKER_KEYS:
        return
    registry = _read_registry(workspace)
    allowed = {
        character.canonical_name for character in registry.characters
    } | _script_speaker_keys(workspace)
    if speaker not in allowed:
        raise RuntimeError(
            "speaker must be a canonical character name or an existing Script key: "
            f"{speaker}"
        )


def _script_speaker_keys(workspace: Workspace) -> set[str]:
    if not workspace.script_ir_dir.exists():
        return set()
    speakers: set[str] = set()
    for path in workspace.script_ir_dir.rglob("*_script.json"):
        artifact = _try_read_script_artifact(path)
        if artifact is not None:
            speakers.update(segment.speaker for segment in artifact.segments)
    return speakers


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
    effective_chunk_id = (
        chunk_id
        if chunk_id and chunk_id != "stitched_available_chunks"
        else COMPLETE_SCRIPT_CHUNK_ID
    )
    candidates = preferred_script_artifact_paths(workspace, effective_chunk_id)
    if candidates:
        return candidates[0]
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
