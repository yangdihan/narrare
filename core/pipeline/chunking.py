from __future__ import annotations

from pathlib import Path

from config.loader import load_config
from core.chunking.chunker import chunk_text
from core.document.manifest import create_source_manifest
from core.document.txt_loader import load_txt
from core.models.chunk import ChunksArtifact, ChunkingConfig, TextChunk
from core.models.validation import ValidationReport
from core.validation.text_integrity import validate_chunk_reconstruction
from storage.json_store import write_json
from storage.workspace import Workspace


class ChunkingResult:
    def __init__(
        self,
        *,
        workspace: Workspace,
        artifact: ChunksArtifact,
        validation_report: ValidationReport,
    ) -> None:
        self.workspace = workspace
        self.artifact = artifact
        self.validation_report = validation_report

    @property
    def chunks(self) -> list[TextChunk]:
        return self.artifact.chunks


def run_chunking_workflow(
    source_path: str | Path,
    project_id: str,
    *,
    config: ChunkingConfig | None = None,
    workspace_root: str | Path = "data/interim",
) -> ChunkingResult:
    document = load_txt(source_path)
    chunking_config = config or load_config().chunking
    manifest = create_source_manifest(project_id, document)
    chunks = chunk_text(document.text, chunking_config)
    validation_report = validate_chunk_reconstruction(project_id, document.text, chunks)

    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()

    artifact = ChunksArtifact(
        project_id=project_id,
        source_sha256=manifest.sha256,
        chunking_config=chunking_config,
        chunks=chunks,
    )
    write_json(workspace.source_manifest_path, manifest)
    write_json(workspace.chunks_path, artifact)
    write_json(workspace.validation_report_path, validation_report)

    for chunk in chunks:
        Path(workspace.chunk_text_path(chunk.index)).write_text(
            chunk.text, encoding=document.encoding
        )

    if not validation_report.exact_reconstruction_success:
        raise RuntimeError(
            "Chunk validation failed: " + "; ".join(validation_report.errors)
        )

    return ChunkingResult(
        workspace=workspace,
        artifact=artifact,
        validation_report=validation_report,
    )
