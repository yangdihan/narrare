from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from core.models.audiobook import (
    AssemblyClipSelection,
    AssemblyManifest,
    FinalAudiobookManifest,
    NormalizedClipResult,
)
from core.models.ir import ScriptArtifact
from core.models.voice import VoiceAssignmentArtifact
from core.pipeline.script_artifact_selection import select_script_artifact_path
from core.pipeline.script_assembly import COMPLETE_SCRIPT_CHUNK_ID
from core.pipeline.voice_assignment import (
    list_audio_takes,
    selected_audio_take_numbers,
)
from storage.json_store import write_json
from storage.workspace import Workspace

DEFAULT_TARGET_ACTIVE_RMS_DBFS = -20.0
DEFAULT_PEAK_CEILING_DBFS = -1.0
DEFAULT_MAX_GAIN_DB = 18.0
DEFAULT_OUTPUT_SAMPLE_RATE = 24_000
ProgressCallback = Callable[["AudiobookAssemblyProgress"], None]


@dataclass(frozen=True)
class AudiobookAssemblyProgress:
    project_id: str
    status: str
    total_clips: int
    completed_clips: int
    current_segment_id: str | None = None


def build_assembly_manifest(
    project_id: str,
    *,
    workspace_root: str | Path = "data/interim",
) -> AssemblyManifest:
    """Freeze the selected, current take for every script segment in order."""
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()
    script_path = select_script_artifact_path(workspace, COMPLETE_SCRIPT_CHUNK_ID)
    script = ScriptArtifact.model_validate_json(script_path.read_text(encoding="utf-8"))
    if not workspace.voice_assignments_path.exists():
        raise RuntimeError("voice assignments artifact not found")
    assignments = VoiceAssignmentArtifact.model_validate_json(
        workspace.voice_assignments_path.read_text(encoding="utf-8")
    )
    assignment_by_speaker = {
        assignment.speaker: assignment for assignment in assignments.assignments
    }
    takes_by_segment = list_audio_takes(project_id, workspace_root=workspace.root)
    selected_numbers = selected_audio_take_numbers(
        project_id,
        workspace_root=workspace.root,
    )

    clips = []
    errors = []
    for segment in script.segments:
        takes = takes_by_segment.get(segment.segment_id, [])
        take_number = selected_numbers.get(segment.segment_id)
        if take_number is None and any(take.take_number == 1 for take in takes):
            take_number = 1
        take = next(
            (item for item in takes if item.take_number == take_number),
            None,
        )
        assignment = assignment_by_speaker.get(segment.speaker)
        if take is None:
            errors.append(f"{segment.segment_id}: no selected audio take")
            continue
        if assignment is None or not assignment.voice_profile_id:
            errors.append(f"{segment.segment_id}: no assigned voice")
            continue
        if (
            take.speaker != segment.speaker
            or take.text != segment.text
            or take.voice_profile_id != assignment.voice_profile_id
        ):
            errors.append(f"{segment.segment_id}: selected take is stale")
            continue
        clips.append(
            AssemblyClipSelection(
                segment_id=segment.segment_id,
                speaker=segment.speaker,
                take_number=take.take_number,
                voice_profile_id=take.voice_profile_id,
                text_sha256=hashlib.sha256(segment.text.encode("utf-8")).hexdigest(),
                audio_path=str(
                    workspace.audio_take_path(segment.segment_id, take.take_number)
                ),
                take_manifest_path=str(
                    workspace.audio_take_manifest_path(
                        segment.segment_id,
                        take.take_number,
                    )
                ),
            )
        )

    if errors:
        preview = "; ".join(errors[:10])
        suffix = f"; and {len(errors) - 10} more" if len(errors) > 10 else ""
        raise RuntimeError(
            f"cannot assemble audiobook; {len(errors)} script segments are not ready: "
            f"{preview}{suffix}"
        )

    manifest = AssemblyManifest(
        project_id=project_id,
        script_artifact_path=str(script_path),
        created_at=datetime.now(UTC),
        clips=clips,
    )
    write_json(workspace.assembly_manifest_path, manifest)
    return manifest


def render_assembly_manifest(
    manifest_path: str | Path,
    *,
    output_path: str | Path,
    final_manifest_path: str | Path,
    target_active_rms_dbfs: float = DEFAULT_TARGET_ACTIVE_RMS_DBFS,
    peak_ceiling_dbfs: float = DEFAULT_PEAK_CEILING_DBFS,
    max_gain_db: float = DEFAULT_MAX_GAIN_DB,
    output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
    progress_callback: ProgressCallback | None = None,
) -> FinalAudiobookManifest:
    """Render one normalized WAV using only the frozen assembly manifest."""
    if peak_ceiling_dbfs > 0:
        raise RuntimeError("peak ceiling must be at or below 0 dBFS")
    if output_sample_rate < 8_000:
        raise RuntimeError("output sample rate must be at least 8000 Hz")
    if max_gain_db < 0:
        raise RuntimeError("maximum gain must not be negative")

    manifest_file = Path(manifest_path)
    manifest = AssemblyManifest.model_validate_json(
        manifest_file.read_text(encoding="utf-8")
    )
    if not manifest.clips:
        raise RuntimeError("assembly manifest contains no clips")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.assembling.wav")
    if temporary.exists():
        temporary.unlink()

    _emit(progress_callback, manifest.project_id, "running", len(manifest.clips), 0)
    normalized_clips = []
    total_samples = 0
    try:
        with sf.SoundFile(
            temporary,
            mode="w",
            samplerate=output_sample_rate,
            channels=1,
            subtype="PCM_16",
            format="WAV",
        ) as output:
            for index, clip in enumerate(manifest.clips):
                _emit(
                    progress_callback,
                    manifest.project_id,
                    "running",
                    len(manifest.clips),
                    index,
                    clip.segment_id,
                )
                samples, input_sample_rate = _read_mono_audio(clip.audio_path)
                samples = _resample(samples, input_sample_rate, output_sample_rate)
                normalized, active_dbfs, gain_db, peak_dbfs = _normalize_clip(
                    samples,
                    output_sample_rate,
                    target_active_rms_dbfs,
                    peak_ceiling_dbfs,
                    max_gain_db,
                )
                output.write(normalized)
                total_samples += len(normalized)
                normalized_clips.append(
                    NormalizedClipResult(
                        **clip.model_dump(),
                        input_sample_rate=input_sample_rate,
                        output_sample_count=len(normalized),
                        input_active_rms_dbfs=active_dbfs,
                        applied_gain_db=gain_db,
                        output_peak_dbfs=peak_dbfs,
                    )
                )
        temporary.replace(destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    result = FinalAudiobookManifest(
        project_id=manifest.project_id,
        assembly_manifest_path=str(manifest_file),
        output_path=str(destination),
        sample_rate=output_sample_rate,
        subtype="PCM_16",
        target_active_rms_dbfs=target_active_rms_dbfs,
        peak_ceiling_dbfs=peak_ceiling_dbfs,
        clip_count=len(normalized_clips),
        duration_seconds=total_samples / output_sample_rate,
        created_at=datetime.now(UTC),
        clips=normalized_clips,
    )
    write_json(final_manifest_path, result)
    _emit(
        progress_callback,
        manifest.project_id,
        "complete",
        len(manifest.clips),
        len(manifest.clips),
    )
    return result


def run_audiobook_assembly_workflow(
    project_id: str,
    *,
    workspace_root: str | Path = "data/interim",
    target_active_rms_dbfs: float = DEFAULT_TARGET_ACTIVE_RMS_DBFS,
    peak_ceiling_dbfs: float = DEFAULT_PEAK_CEILING_DBFS,
    progress_callback: ProgressCallback | None = None,
) -> FinalAudiobookManifest:
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()
    build_assembly_manifest(project_id, workspace_root=workspace.root)
    return render_assembly_manifest(
        workspace.assembly_manifest_path,
        output_path=workspace.final_audiobook_path,
        final_manifest_path=workspace.final_audiobook_manifest_path,
        target_active_rms_dbfs=target_active_rms_dbfs,
        peak_ceiling_dbfs=peak_ceiling_dbfs,
        progress_callback=progress_callback,
    )


def _read_mono_audio(path: str | Path) -> tuple[np.ndarray, int]:
    audio_path = Path(path)
    if not audio_path.is_absolute():
        audio_path = Path.cwd() / audio_path
    if not audio_path.exists():
        raise RuntimeError(f"selected audio file not found: {audio_path}")
    samples, sample_rate = sf.read(
        audio_path,
        dtype="float32",
        always_2d=True,
    )
    if not len(samples):
        raise RuntimeError(f"selected audio file is empty: {audio_path}")
    mono = np.mean(samples, axis=1, dtype=np.float32)
    mono = np.nan_to_num(mono, nan=0.0, posinf=0.0, neginf=0.0)
    mono -= np.mean(mono, dtype=np.float64)
    return mono.astype(np.float32, copy=False), int(sample_rate)


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples
    divisor = math.gcd(source_rate, target_rate)
    return resample_poly(
        samples,
        target_rate // divisor,
        source_rate // divisor,
    ).astype(np.float32, copy=False)


def _normalize_clip(
    samples: np.ndarray,
    sample_rate: int,
    target_dbfs: float,
    peak_ceiling_dbfs: float,
    max_gain_db: float,
) -> tuple[np.ndarray, float | None, float, float | None]:
    active_dbfs = _active_rms_dbfs(samples, sample_rate)
    gain_db = 0.0
    if active_dbfs is not None:
        gain_db = float(np.clip(target_dbfs - active_dbfs, -max_gain_db, max_gain_db))
    normalized = samples * (10.0 ** (gain_db / 20.0))

    peak = float(np.max(np.abs(normalized), initial=0.0))
    peak_limit = 10.0 ** (peak_ceiling_dbfs / 20.0)
    if peak > peak_limit and peak > 0:
        peak_gain = peak_limit / peak
        normalized *= peak_gain
        gain_db += 20.0 * math.log10(peak_gain)
        peak = peak_limit
    peak_dbfs = 20.0 * math.log10(peak) if peak > 0 else None
    return normalized.astype(np.float32, copy=False), active_dbfs, gain_db, peak_dbfs


def _active_rms_dbfs(samples: np.ndarray, sample_rate: int) -> float | None:
    frame_length = max(1, int(sample_rate * 0.05))
    frame_count = max(1, math.ceil(len(samples) / frame_length))
    padded = np.pad(samples, (0, frame_count * frame_length - len(samples)))
    frames = padded.reshape(frame_count, frame_length)
    frame_rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    positive = frame_rms[frame_rms > 1e-9]
    if not len(positive):
        return None
    frame_dbfs = 20.0 * np.log10(np.maximum(frame_rms, 1e-9))
    gate_dbfs = max(-50.0, float(np.max(frame_dbfs)) - 20.0)
    active = frame_rms[frame_dbfs >= gate_dbfs]
    if not len(active):
        return None
    rms = math.sqrt(float(np.mean(np.square(active, dtype=np.float64))))
    return 20.0 * math.log10(rms) if rms > 0 else None


def _emit(
    callback: ProgressCallback | None,
    project_id: str,
    status: str,
    total: int,
    completed: int,
    current_segment_id: str | None = None,
) -> None:
    if callback is None:
        return
    callback(
        AudiobookAssemblyProgress(
            project_id=project_id,
            status=status,
            total_clips=total,
            completed_clips=completed,
            current_segment_id=current_segment_id,
        )
    )
