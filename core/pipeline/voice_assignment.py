from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.voice import AudioTakeManifest, VoiceAssignment, VoiceAssignmentArtifact
from core.pipeline.script_assembly import COMPLETE_SCRIPT_CHUNK_ID
from core.pipeline.voice_assets import load_voice_inventory
from core.validation.script_integrity import normalize_content_text
from storage.json_store import write_json
from storage.workspace import Workspace
from tts.base import SynthesisRequest, TTSAdapter
from tts.dummy import DummyTTSAdapter
from tts.qwen.adapter import QwenTTSAdapter
from core.pipeline.qwen_tts import qwen_delete_readiness_report


ProgressCallback = Callable[["AudioGenerationProgress"], None]


@dataclass(frozen=True)
class AudioGenerationProgress:
    project_id: str
    status: str
    total_segments: int
    completed_segments: int
    current_segment_id: str | None = None
    current_speaker: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VoiceAssignmentView:
    project_id: str
    script_artifact_path: Path
    inventory_path: Path
    assignments: VoiceAssignmentArtifact
    voice_profiles: list[dict[str, object]]
    missing_voice_profile_ids: list[str]


def build_voice_assignment_artifact(
    project_id: str,
    *,
    workspace_root: str | Path = "data/interim",
    script_artifact_path: str | Path | None = None,
) -> VoiceAssignmentArtifact:
    workspace = Workspace(project_id, root=workspace_root)
    script_path = _select_script_artifact_path(workspace, script_artifact_path)
    script = _read_script_artifact(script_path)
    existing = _read_assignments(workspace)
    previous = {assignment.speaker: assignment for assignment in existing.assignments} if existing else {}
    summaries = _character_summaries(workspace)
    now = datetime.now(timezone.utc)
    assignments = []
    for speaker in _ordered_speakers(script.segments):
        representative = _representative_segment(script.segments, speaker)
        old = previous.get(speaker)
        assignments.append(
            VoiceAssignment(
                speaker=speaker,
                voice_profile_id=old.voice_profile_id if old else None,
                representative_segment_id=representative.segment_id if representative else None,
                representative_text=(
                    _representative_text(representative.text)
                    if representative is not None
                    else ""
                ),
                summary=summaries.get(speaker),
                sample_take_path=old.sample_take_path if old else None,
                confirmed=old.confirmed if old else False,
            )
        )
    artifact = VoiceAssignmentArtifact(
        project_id=project_id,
        script_artifact_path=str(script_path),
        created_at=existing.created_at if existing else now,
        updated_at=now,
        assignments=assignments,
    )
    write_json(workspace.voice_assignments_path, artifact)
    return artifact


def build_voice_assignment_view(
    project_id: str,
    *,
    workspace_root: str | Path = "data/interim",
    voice_inventory_path: str | Path = "data/voices/qwen/voice_profiles.json",
) -> VoiceAssignmentView:
    workspace = Workspace(project_id, root=workspace_root)
    script_path = _select_script_artifact_path(workspace, None)
    if workspace.voice_assignments_path.exists():
        assignments = _read_assignments(workspace)
        if assignments is None or assignments.script_artifact_path != str(script_path):
            assignments = build_voice_assignment_artifact(
                project_id,
                workspace_root=workspace_root,
                script_artifact_path=script_path,
            )
    else:
        assignments = build_voice_assignment_artifact(
            project_id,
            workspace_root=workspace_root,
            script_artifact_path=script_path,
        )
    inventory = load_voice_inventory(voice_inventory_path)
    profile_ids = {profile.profile_id for profile in inventory.profiles}
    assigned_profile_ids = {
        assignment.voice_profile_id
        for assignment in assignments.assignments
        if assignment.voice_profile_id
    }
    return VoiceAssignmentView(
        project_id=project_id,
        script_artifact_path=script_path,
        inventory_path=Path(voice_inventory_path),
        assignments=assignments,
        voice_profiles=[
            {
                **profile.model_dump(),
                "available": Path(profile.prompt_path).exists(),
            }
            for profile in inventory.profiles
        ],
        missing_voice_profile_ids=sorted(assigned_profile_ids - profile_ids),
    )


def save_voice_assignments(
    project_id: str,
    voice_profile_by_speaker: dict[str, str],
    *,
    workspace_root: str | Path = "data/interim",
) -> VoiceAssignmentArtifact:
    artifact = build_voice_assignment_artifact(
        project_id,
        workspace_root=workspace_root,
    )
    assignments = []
    for assignment in artifact.assignments:
        profile_id = voice_profile_by_speaker.get(assignment.speaker)
        assignments.append(
            assignment.model_copy(
                update={
                    "voice_profile_id": profile_id,
                    "confirmed": bool(profile_id),
                }
            )
        )
    updated = artifact.model_copy(
        update={
            "updated_at": datetime.now(timezone.utc),
            "assignments": assignments,
        }
    )
    workspace = Workspace(project_id, root=workspace_root)
    write_json(workspace.voice_assignments_path, updated)
    return updated


def generate_voice_sample(
    project_id: str,
    speaker: str,
    voice_profile_id: str,
    *,
    workspace_root: str | Path = "data/interim",
    voice_inventory_path: str | Path = "data/voices/qwen/voice_profiles.json",
    adapter: TTSAdapter | None = None,
) -> VoiceAssignmentArtifact:
    workspace = Workspace(project_id, root=workspace_root)
    artifact = build_voice_assignment_artifact(project_id, workspace_root=workspace_root)
    assignment = _assignment_for(artifact, speaker)
    text = assignment.representative_text
    if not text:
        raise RuntimeError(f"no representative text for speaker: {speaker}")
    profile = _profile_by_id(voice_inventory_path, voice_profile_id)
    output_path = workspace.voice_sample_path(speaker)
    adapter = adapter or _default_adapter()
    adapter.synthesize(
        SynthesisRequest(
            text=text,
            voice_prompt_path=Path(profile.prompt_path),
            output_path=output_path,
        )
    )
    updated = _update_assignment(
        artifact,
        speaker,
        {
            "voice_profile_id": voice_profile_id,
            "sample_take_path": str(output_path),
            "confirmed": True,
        },
    )
    write_json(workspace.voice_assignments_path, updated)
    return updated


def run_audio_generation_workflow(
    project_id: str,
    *,
    workspace_root: str | Path = "data/interim",
    voice_inventory_path: str | Path = "data/voices/qwen/voice_profiles.json",
    only_missing: bool = True,
    adapter: TTSAdapter | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, object]:
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()
    assignments = _read_assignments(workspace)
    if assignments is None:
        raise RuntimeError("voice assignments artifact not found")
    profile_by_id = {
        profile.profile_id: profile for profile in load_voice_inventory(voice_inventory_path).profiles
    }
    assignment_by_speaker = {
        assignment.speaker: assignment for assignment in assignments.assignments
    }
    script_path = Path(assignments.script_artifact_path)
    script = _read_script_artifact(script_path)
    missing_speakers = [
        speaker
        for speaker in _ordered_speakers(script.segments)
        if not assignment_by_speaker.get(speaker)
        or not assignment_by_speaker[speaker].voice_profile_id
    ]
    if missing_speakers:
        raise RuntimeError(
            "missing voice assignments for speakers: " + ", ".join(missing_speakers)
        )

    adapter = adapter or _default_adapter()
    targets = [
        segment
        for segment in script.segments
        if not only_missing or not workspace.audio_take_path(segment.segment_id).exists()
    ]
    _emit(progress_callback, project_id, "running", len(targets), 0)
    generated = 0
    errors: list[str] = []
    for segment in targets:
        assignment = assignment_by_speaker[segment.speaker]
        profile = profile_by_id.get(assignment.voice_profile_id or "")
        if profile is None:
            raise RuntimeError(f"unknown voice profile: {assignment.voice_profile_id}")
        _emit(
            progress_callback,
            project_id,
            "running",
            len(targets),
            generated,
            current_segment_id=segment.segment_id,
            current_speaker=segment.speaker,
        )
        output_path = workspace.audio_take_path(segment.segment_id)
        try:
            result = adapter.synthesize(
                SynthesisRequest(
                    text=segment.text,
                    voice_prompt_path=Path(profile.prompt_path),
                    output_path=output_path,
                )
            )
        except Exception as exc:
            errors.append(f"{segment.segment_id}: {exc}")
            _emit(
                progress_callback,
                project_id,
                "failed",
                len(targets),
                generated,
                current_segment_id=segment.segment_id,
                current_speaker=segment.speaker,
                errors=errors,
            )
            raise
        write_json(
            workspace.audio_take_manifest_path(segment.segment_id),
            AudioTakeManifest(
                project_id=project_id,
                segment_id=segment.segment_id,
                speaker=segment.speaker,
                text=segment.text,
                voice_profile_id=profile.profile_id,
                voice_prompt_path=profile.prompt_path,
                script_artifact_path=str(script_path),
                adapter=result.adapter,
                model_path=str(result.model_path) if result.model_path else None,
                parameters=result.parameters,
                output_path=str(result.output_path),
                created_at=datetime.now(timezone.utc),
            ),
        )
        generated += 1
    _emit(progress_callback, project_id, "complete", len(targets), generated)
    return {
        "project_id": project_id,
        "generated_count": generated,
        "skipped_count": len(script.segments) - len(targets),
        "audio_takes_dir": str(workspace.audio_takes_dir),
        "errors": errors,
    }


def deletion_readiness_report(
    *,
    voice_inventory_path: str | Path = "data/voices/qwen/voice_profiles.json",
) -> dict[str, object]:
    return qwen_delete_readiness_report(voice_inventory_path=voice_inventory_path)


def _default_adapter() -> TTSAdapter:
    return DummyTTSAdapter() if _use_dummy_adapter() else QwenTTSAdapter()


def _use_dummy_adapter() -> bool:
    import os

    return os.environ.get("NARRARE_TTS_ADAPTER") == "dummy"


def _select_script_artifact_path(
    workspace: Workspace,
    script_artifact_path: str | Path | None,
) -> Path:
    if script_artifact_path is not None:
        path = Path(script_artifact_path)
        return path if path.is_absolute() else Path.cwd() / path
    candidates = [
        workspace.key_reviewed_script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
        workspace.normalized_script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
        workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError("no complete script artifact found")


def _read_script_artifact(path: Path) -> ScriptArtifact:
    if not path.exists():
        raise RuntimeError(f"script artifact not found: {path}")
    return ScriptArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _read_assignments(workspace: Workspace) -> VoiceAssignmentArtifact | None:
    if not workspace.voice_assignments_path.exists():
        return None
    return VoiceAssignmentArtifact.model_validate_json(
        workspace.voice_assignments_path.read_text(encoding="utf-8")
    )


def _ordered_speakers(segments: list[ScriptSegment]) -> list[str]:
    seen = set()
    output = []
    for segment in segments:
        if segment.speaker not in seen:
            seen.add(segment.speaker)
            output.append(segment.speaker)
    return output


def _representative_segment(
    segments: list[ScriptSegment],
    speaker: str,
) -> ScriptSegment | None:
    candidates = [segment for segment in segments if segment.speaker == speaker]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda segment: abs(len(normalize_content_text(_representative_text(segment.text))) - 30),
    )


def _representative_text(text: str) -> str:
    parts = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in "。！？!?":
            end = index + 1
            while end < len(text) and text[end] in "”’」』》）)]":
                end += 1
            parts.append(text[start:end].strip())
            start = end
            index = end
            continue
        index += 1
    if start < len(text):
        parts.append(text[start:].strip())
    candidates = [part for part in parts if normalize_content_text(part)]
    if not candidates:
        return text.strip()
    return min(
        candidates,
        key=lambda part: abs(len(normalize_content_text(part)) - 30),
    )


def _character_summaries(workspace: Workspace) -> dict[str, str]:
    summaries = {
        "narrator": "旁白。",
        "unknown_speaker": "未确认说话人。",
    }
    if not workspace.character_registry_path.exists():
        return summaries
    registry = json.loads(workspace.character_registry_path.read_text(encoding="utf-8"))
    for character in registry.get("characters", []):
        names = [
            character.get("canonical_name", ""),
            *character.get("stable_aliases", []),
        ]
        summary = "；".join(
            value
            for value in [
                character.get("persona_summary"),
                character.get("speaking_style"),
                character.get("age_impression"),
            ]
            if value
        )
        for name in names:
            if name:
                summaries.setdefault(name, summary or character.get("canonical_name") or name)
    return summaries


def _assignment_for(artifact: VoiceAssignmentArtifact, speaker: str) -> VoiceAssignment:
    for assignment in artifact.assignments:
        if assignment.speaker == speaker:
            return assignment
    raise RuntimeError(f"speaker not found in assignments: {speaker}")


def _profile_by_id(voice_inventory_path: str | Path, profile_id: str):
    inventory = load_voice_inventory(voice_inventory_path)
    for profile in inventory.profiles:
        if profile.profile_id == profile_id:
            return profile
    raise RuntimeError(f"voice profile not found: {profile_id}")


def _update_assignment(
    artifact: VoiceAssignmentArtifact,
    speaker: str,
    updates: dict[str, object],
) -> VoiceAssignmentArtifact:
    assignments = []
    for assignment in artifact.assignments:
        assignments.append(
            assignment.model_copy(update=updates)
            if assignment.speaker == speaker
            else assignment
        )
    return artifact.model_copy(
        update={
            "updated_at": datetime.now(timezone.utc),
            "assignments": assignments,
        }
    )


def _emit(
    callback: ProgressCallback | None,
    project_id: str,
    status: str,
    total_segments: int,
    completed_segments: int,
    *,
    current_segment_id: str | None = None,
    current_speaker: str | None = None,
    errors: list[str] | None = None,
) -> None:
    if callback is None:
        return
    callback(
        AudioGenerationProgress(
            project_id=project_id,
            status=status,
            total_segments=total_segments,
            completed_segments=completed_segments,
            current_segment_id=current_segment_id,
            current_speaker=current_speaker,
            errors=errors or [],
        )
    )
