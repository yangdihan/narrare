from __future__ import annotations

from pathlib import Path

from core.ir.script_revision import script_artifact_revision
from core.models.ir import ScriptArtifact
from storage.workspace import Workspace


def preferred_script_artifact_paths(
    workspace: Workspace,
    chunk_id: str,
) -> list[Path]:
    """Return current derived scripts first, followed by the Stage 2 base script.

    A normalization or key-review file is usable only when it identifies the
    current base script revision. This preserves old review artifacts for audit
    while preventing a reassembly from being hidden by an obsolete derivative.
    """
    base_path = workspace.script_artifact_path(chunk_id)
    derived_paths = [
        workspace.key_reviewed_script_artifact_path(chunk_id),
        workspace.normalized_script_artifact_path(chunk_id),
    ]
    if not base_path.exists():
        return [path for path in [*derived_paths, base_path] if path.exists()]

    try:
        base_revision = script_artifact_revision(_read_script_artifact(base_path))
    except (OSError, ValueError):
        return [path for path in [*derived_paths, base_path] if path.exists()]

    current_derived = [
        path
        for path in derived_paths
        if _has_current_source_revision(path, base_revision)
    ]
    return [*current_derived, base_path]


def select_script_artifact_path(workspace: Workspace, chunk_id: str) -> Path:
    paths = preferred_script_artifact_paths(workspace, chunk_id)
    if paths:
        return paths[0]
    raise RuntimeError("script artifact not found")


def _has_current_source_revision(path: Path, base_revision: str) -> bool:
    if not path.exists():
        return False
    try:
        artifact = _read_script_artifact(path)
    except (OSError, ValueError):
        return False
    return artifact.source_script_revision == base_revision


def _read_script_artifact(path: Path) -> ScriptArtifact:
    return ScriptArtifact.model_validate_json(path.read_text(encoding="utf-8"))
