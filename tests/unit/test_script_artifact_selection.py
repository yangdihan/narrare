from pathlib import Path

from core.ir.script_revision import script_artifact_revision
from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.source import SourceSpan
from core.pipeline.script_artifact_selection import preferred_script_artifact_paths
from storage.json_store import write_json
from storage.workspace import Workspace


def _artifact(
    *,
    source_script_revision: str | None = None,
    text: str = "甲乙",
) -> ScriptArtifact:
    return ScriptArtifact(
        project_id="fixture_project",
        chunk_id="complete",
        chunk_source_path="unused",
        chunk_sha256="unused",
        llm_provider="test",
        llm_model="test",
        response_source="assembled",
        source_script_revision=source_script_revision,
        processed_chunk_count=1,
        segments=[
            ScriptSegment(
                segment_id="seg_000001",
                source_span=SourceSpan(start=0, end=len(text)),
                script={"narrator": text},
                confidence=0.9,
            )
        ],
    )


def test_selection_ignores_stale_review_after_base_script_changes(
    tmp_path: Path,
) -> None:
    workspace = Workspace("fixture_project", root=tmp_path / "interim")
    workspace.ensure()
    base = _artifact(text="甲乙")
    write_json(workspace.script_artifact_path("complete"), base)
    write_json(
        workspace.key_reviewed_script_artifact_path("complete"),
        _artifact(text="甲乙"),
    )

    assert preferred_script_artifact_paths(workspace, "complete") == [
        workspace.script_artifact_path("complete")
    ]

    current_revision = script_artifact_revision(base)
    normalized = _artifact(source_script_revision=current_revision, text="甲乙")
    write_json(workspace.normalized_script_artifact_path("complete"), normalized)
    assert preferred_script_artifact_paths(workspace, "complete") == [
        workspace.normalized_script_artifact_path("complete"),
        workspace.script_artifact_path("complete"),
    ]

    changed_base = _artifact(text="甲乙丙")
    write_json(workspace.script_artifact_path("complete"), changed_base)
    assert preferred_script_artifact_paths(workspace, "complete") == [
        workspace.script_artifact_path("complete")
    ]
