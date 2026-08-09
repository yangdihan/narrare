from pathlib import Path

from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.source import SourceSpan
from core.pipeline.chunking import run_chunking_workflow
from core.pipeline.script_assembly import run_script_assembly_workflow
from core.validation.script_integrity import sha256_text
from storage.json_store import write_json


def _write_chunk_script(
    project_id: str,
    chunk_id: str,
    chunk_text: str,
    segments: list[ScriptSegment],
) -> ScriptArtifact:
    return ScriptArtifact(
        project_id=project_id,
        chunk_id=chunk_id,
        chunk_source_path="unused",
        chunk_sha256=sha256_text(chunk_text),
        llm_provider="test",
        llm_model="test",
        response_source="response_path",
        processed_chunk_count=1,
        segments=segments,
    )


def test_assembly_preserves_ids_for_unchanged_segments_after_earlier_resegmentation(
    tmp_path: Path,
) -> None:
    project_id = "fixture_project"
    source = tmp_path / "source.txt"
    source.write_text("\u7532\u4e59\u4e19", encoding="utf-8")
    chunk_result = run_chunking_workflow(
        source,
        project_id,
        workspace_root=tmp_path / "interim",
    )
    workspace = chunk_result.workspace
    chunk = chunk_result.chunks[0]
    write_json(
        workspace.script_artifact_path(chunk.chunk_id),
        _write_chunk_script(
            project_id,
            chunk.chunk_id,
            chunk.text,
            [
                ScriptSegment(
                    segment_id="seg_000001",
                    source_span=SourceSpan(start=0, end=2),
                    script={"narrator": "\u7532\u4e59"},
                    confidence=0.9,
                ),
                ScriptSegment(
                    segment_id="seg_000002",
                    source_span=SourceSpan(start=2, end=3),
                    script={"speaker_b": "\u4e19"},
                    confidence=0.9,
                ),
            ],
        ),
    )
    first = run_script_assembly_workflow(project_id, workspace_root=workspace.root)
    unchanged_id = first.artifact.segments[1].segment_id

    write_json(
        workspace.script_artifact_path(chunk.chunk_id),
        _write_chunk_script(
            project_id,
            chunk.chunk_id,
            chunk.text,
            [
                ScriptSegment(
                    segment_id="seg_000001",
                    source_span=SourceSpan(start=0, end=1),
                    script={"narrator": "\u7532"},
                    confidence=0.9,
                ),
                ScriptSegment(
                    segment_id="seg_000002",
                    source_span=SourceSpan(start=1, end=2),
                    script={"speaker_a": "\u4e59"},
                    confidence=0.9,
                ),
                ScriptSegment(
                    segment_id="seg_000003",
                    source_span=SourceSpan(start=2, end=3),
                    script={"speaker_b": "\u4e19"},
                    confidence=0.9,
                ),
            ],
        ),
    )
    second = run_script_assembly_workflow(project_id, workspace_root=workspace.root)

    unchanged = second.artifact.segments[2]
    assert unchanged.text == "\u4e19"
    assert unchanged.segment_id == unchanged_id
    assert second.preserved_segment_id_count == 1
    assert second.new_segment_id_count == 2
    assert len({segment.segment_id for segment in second.artifact.segments}) == 3
