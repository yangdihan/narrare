from cli.main import (
    ContextProfileProgressRenderer,
    ScriptProgressRenderer,
    SpeakerKeyReviewProgressRenderer,
)
from core.pipeline.chunk_context_profiler import ContextProfileProgress
from core.pipeline.script_conversion import ScriptProgress
from core.pipeline.speaker_key_review import SpeakerKeyReviewProgress


def test_script_progress_renderer_shows_counts_and_remaining() -> None:
    renderer = ScriptProgressRenderer(enabled=False, width=10)
    progress = ScriptProgress(
        chunk_id="chunk_0001",
        attempt=3,
        max_attempts=5,
        attempt_elapsed_seconds=12.25,
        chunk_elapsed_seconds=31.5,
        status="attempt_complete",
        errors=[],
    )

    line = renderer._format(progress)

    assert "[######----]" in line
    assert "chunk=chunk_0001" in line
    assert "try=3/5" in line
    assert "try_elapsed=12.2s" in line
    assert "chunk_elapsed=31.5s" in line
    assert "status=attempt_complete" in line


def test_context_profile_progress_renderer_shows_chunk_counts() -> None:
    renderer = ContextProfileProgressRenderer(enabled=False, width=10)
    progress = ContextProfileProgress(
        chunk_id="chunk_0003",
        processed_chunks=3,
        total_chunks=5,
        chunk_elapsed_seconds=8.75,
        total_elapsed_seconds=42.0,
        status="chunk_complete",
        errors=[],
    )

    line = renderer._format(progress)

    assert "[######----]" in line
    assert "stage=1" in line
    assert "chunk=chunk_0003" in line
    assert "chunks=3/5" in line
    assert "chunk_elapsed=8.8s" in line
    assert "total_elapsed=42.0s" in line
    assert "status=chunk_complete" in line


def test_speaker_key_review_progress_renderer_shows_candidate_counts() -> None:
    renderer = SpeakerKeyReviewProgressRenderer(enabled=False, width=10)
    progress = SpeakerKeyReviewProgress(
        segment_id="seg_000123",
        current_key="马丁先生",
        processed_candidates=4,
        total_candidates=10,
        changed_count=2,
        candidate_elapsed_seconds=7.25,
        total_elapsed_seconds=50.0,
        status="candidate_complete",
        errors=[],
    )

    line = renderer._format(progress)

    assert "[####------]" in line
    assert "stage=3" in line
    assert "segment=seg_000123" in line
    assert "key=马丁先生" in line
    assert "candidates=4/10" in line
    assert "changed=2" in line
    assert "candidate_elapsed=7.2s" in line
    assert "total_elapsed=50.0s" in line
    assert "status=candidate_complete" in line
