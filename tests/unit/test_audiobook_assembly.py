from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.source import SourceSpan
from core.models.voice import (
    AudioTakeManifest,
    AudioTakeSelectionArtifact,
    VoiceAssignment,
    VoiceAssignmentArtifact,
)
from core.pipeline.audiobook_assembly import (
    build_assembly_manifest,
    run_audiobook_assembly_workflow,
)
from storage.json_store import write_json
from storage.workspace import Workspace


def test_assembly_uses_selected_takes_in_order_and_normalizes_active_rms(
    tmp_path: Path,
) -> None:
    workspace = _write_assembly_fixture(tmp_path)

    result = run_audiobook_assembly_workflow(
        "fixture_project",
        workspace_root=workspace.root,
    )

    assert [clip.segment_id for clip in result.clips] == [
        "seg_000001",
        "seg_000002",
    ]
    assert [clip.take_number for clip in result.clips] == [1, 2]
    assert result.clip_count == 2
    assert result.sample_rate == 24_000
    assert result.duration_seconds == 2.0
    assert workspace.assembly_manifest_path.exists()
    assert workspace.final_audiobook_manifest_path.exists()

    audio, sample_rate = sf.read(workspace.final_audiobook_path, dtype="float32")
    assert sample_rate == 24_000
    assert len(audio) == 48_000
    first_rms = _rms_dbfs(audio[:24_000])
    second_rms = _rms_dbfs(audio[24_000:])
    assert abs(first_rms - (-20.0)) < 0.1
    assert abs(second_rms - (-20.0)) < 0.1
    assert abs(first_rms - second_rms) < 0.05


def test_assembly_rejects_stale_selected_take(tmp_path: Path) -> None:
    workspace = _write_assembly_fixture(tmp_path)
    script = ScriptArtifact.model_validate_json(
        workspace.script_artifact_path("complete").read_text(encoding="utf-8")
    )
    changed = script.segments[0].model_copy(
        update={"script": {"narrator": "changed text"}}
    )
    write_json(
        workspace.script_artifact_path("complete"),
        script.model_copy(update={"segments": [changed, script.segments[1]]}),
    )

    try:
        build_assembly_manifest("fixture_project", workspace_root=workspace.root)
    except RuntimeError as exc:
        assert "seg_000001: selected take is stale" in str(exc)
    else:
        raise AssertionError("stale selected audio must block final assembly")


def _write_assembly_fixture(tmp_path: Path) -> Workspace:
    workspace = Workspace("fixture_project", root=tmp_path / "interim")
    workspace.ensure()
    script_path = workspace.script_artifact_path("complete")
    segments = [
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=5),
            script={"narrator": "first"},
            confidence=1.0,
        ),
        ScriptSegment(
            segment_id="seg_000002",
            source_span=SourceSpan(start=5, end=11),
            script={"speaker": "second"},
            confidence=1.0,
        ),
    ]
    write_json(
        script_path,
        ScriptArtifact(
            project_id="fixture_project",
            chunk_id="complete",
            chunk_source_path="source.txt",
            chunk_sha256="hash",
            llm_provider="test",
            llm_model="test",
            response_source="assembled",
            processed_chunk_count=1,
            segments=segments,
        ),
    )
    now = datetime.now(UTC)
    write_json(
        workspace.voice_assignments_path,
        VoiceAssignmentArtifact(
            project_id="fixture_project",
            script_artifact_path=str(script_path),
            created_at=now,
            updated_at=now,
            assignments=[
                VoiceAssignment(
                    speaker="narrator",
                    voice_profile_id="voice_a",
                    confirmed=True,
                ),
                VoiceAssignment(
                    speaker="speaker",
                    voice_profile_id="voice_b",
                    confirmed=True,
                ),
            ],
        ),
    )

    _write_take(workspace, segments[0], 1, "voice_a", 0.03, 16_000, now)
    _write_take(workspace, segments[1], 2, "voice_b", 0.5, 24_000, now)
    write_json(
        workspace.audio_take_selections_path,
        AudioTakeSelectionArtifact(
            project_id="fixture_project",
            selected_take_by_segment={"seg_000001": 1, "seg_000002": 2},
            updated_at=now,
        ),
    )
    return workspace


def _write_take(
    workspace: Workspace,
    segment: ScriptSegment,
    take_number: int,
    voice_profile_id: str,
    amplitude: float,
    sample_rate: int,
    created_at: datetime,
) -> None:
    output_path = workspace.audio_take_path(segment.segment_id, take_number)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    time = np.arange(sample_rate, dtype=np.float32) / sample_rate
    samples = amplitude * np.sin(2 * math.pi * 220 * time)
    sf.write(output_path, samples, sample_rate, subtype="PCM_16")
    write_json(
        workspace.audio_take_manifest_path(segment.segment_id, take_number),
        AudioTakeManifest(
            project_id=workspace.project_id,
            segment_id=segment.segment_id,
            take_number=take_number,
            speaker=segment.speaker,
            text=segment.text,
            voice_profile_id=voice_profile_id,
            voice_prompt_path=f"{voice_profile_id}.pt",
            script_artifact_path=str(workspace.script_artifact_path("complete")),
            adapter="test",
            output_path=str(output_path),
            created_at=created_at,
        ),
    )


def _rms_dbfs(samples: np.ndarray) -> float:
    rms = math.sqrt(float(np.mean(np.square(samples, dtype=np.float64))))
    return 20 * math.log10(rms)
