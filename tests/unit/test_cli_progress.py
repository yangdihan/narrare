from cli.main import ScriptProgressRenderer
from core.pipeline.script_conversion import ScriptProgress


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
