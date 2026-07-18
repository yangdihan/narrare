from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.pipeline.chunk_context_profiler import (
    ContextProfileProgress,
    run_chunk_context_profiler_workflow,
)
from core.pipeline.chunking import run_chunking_workflow
from core.pipeline.script_assembly import (
    COMPLETE_SCRIPT_CHUNK_ID,
    run_script_assembly_workflow,
)
from core.pipeline.script_conversion import (
    ScriptProgress,
    run_script_conversion_workflow,
)
from core.validation.script_integrity import normalize_content_text
from storage.workspace import Workspace

BASE_DIR = Path(__file__).resolve().parent

VIEW_OPTIONS = [
    {"id": "original_text", "label": "Original Text"},
    {"id": "chunks", "label": "Chunks"},
    {"id": "scene_summary", "label": "Chunk Scene Summary"},
    {"id": "character_summary", "label": "Character Summary"},
    {"id": "scripts", "label": "Scripts"},
]


class ChunkRequest(BaseModel):
    source_path: str
    project_id: str


class Stage1JobRequest(BaseModel):
    project_id: str
    response_dir: str | None = None


class Stage2JobRequest(BaseModel):
    project_id: str
    selection: str | None = None
    chunk_id: str | None = None
    response_path: str | None = None
    response_dir: str | None = None
    max_windows: int | None = None
    max_retries: int = 1


@dataclass
class PipelineJob:
    job_id: str
    project_id: str
    phase: str
    selection: str
    status: str = "queued"
    total_chunks: int = 0
    completed_chunks: int = 0
    current_chunk_id: str | None = None
    errors: list[str] = field(default_factory=list)
    artifact_paths: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "phase": self.phase,
            "selection": self.selection,
            "chunk_id": self.current_chunk_id or self.selection,
            "status": self.status,
            "total_chunks": self.total_chunks,
            "completed_chunks": self.completed_chunks,
            "current_chunk_id": self.current_chunk_id,
            "total_windows": self.total_chunks,
            "processed_windows": self.completed_chunks,
            "current_window_id": self.current_chunk_id,
            "errors": self.errors,
            "artifact_paths": self.artifact_paths,
            "artifact_path": self.artifact_paths.get("script"),
            "validation_report_path": self.artifact_paths.get("validation_report"),
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, PipelineJob] = {}
        self._lock = threading.Lock()

    def create(self, project_id: str, phase: str, selection: str) -> PipelineJob:
        job = PipelineJob(
            job_id=uuid.uuid4().hex,
            project_id=project_id,
            phase=phase,
            selection=selection,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> PipelineJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, updater: Callable[[PipelineJob], None]) -> None:
        with self._lock:
            updater(self._jobs[job_id])


def create_app(
    *,
    raw_dir: str | Path = "data/raw",
    workspace_root: str | Path = "data/interim",
) -> FastAPI:
    app = FastAPI(title="Narrare Pipeline")
    app.state.raw_dir = Path(raw_dir)
    app.state.workspace_root = Path(workspace_root)
    app.state.jobs = JobRegistry()

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.mount(
        "/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="static",
    )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/sources")
    def list_sources(request: Request) -> dict[str, Any]:
        raw_root = _state_path(request, "raw_dir")
        sources = []
        if raw_root.exists():
            for source in sorted(raw_root.glob("*.txt")):
                sources.append(
                    {
                        "name": source.name,
                        "path": str(source),
                        "default_project_id": _default_project_id(source),
                    }
                )
        return {"sources": sources}

    @app.get("/api/source")
    def get_source(
        request: Request,
        path: str = Query(..., min_length=1),
    ) -> dict[str, Any]:
        source_path = _resolve_source_path(request, path)
        text = source_path.read_text(encoding="utf-8")
        return {
            "name": source_path.name,
            "path": str(source_path),
            "default_project_id": _default_project_id(source_path),
            "text": text,
            "character_count": len(text),
        }

    @app.post("/api/chunk")
    def run_chunk(request: Request, payload: ChunkRequest) -> dict[str, Any]:
        source_path = _resolve_source_path(request, payload.source_path)
        workspace_root = _state_path(request, "workspace_root")
        try:
            result = run_chunking_workflow(
                source_path,
                payload.project_id,
                workspace_root=workspace_root,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _chunks_response(result.workspace)

    @app.get("/api/projects/{project_id}/chunks")
    def get_chunks(request: Request, project_id: str) -> dict[str, Any]:
        workspace = _workspace(request, project_id)
        if not workspace.chunks_path.exists():
            raise HTTPException(status_code=404, detail="chunks artifact not found")
        return _chunks_response(workspace)

    @app.get("/api/projects/{project_id}/artifact-options")
    def get_artifact_options(request: Request, project_id: str) -> dict[str, Any]:
        workspace = _workspace(request, project_id)
        source_path = _source_path_from_manifest(workspace)
        return {
            "project_id": project_id,
            "views": [
                {
                    **option,
                    "available": _view_available(
                        workspace,
                        option["id"],
                        source_path=source_path,
                    ),
                }
                for option in VIEW_OPTIONS
            ],
        }

    @app.get("/api/projects/{project_id}/views/{view_type}")
    def get_artifact_view(
        request: Request,
        project_id: str,
        view_type: str,
        source_path: str | None = None,
        chunk_id: str | None = None,
    ) -> dict[str, Any]:
        workspace = _workspace(request, project_id)
        if view_type not in {option["id"] for option in VIEW_OPTIONS}:
            raise HTTPException(status_code=404, detail="unknown view type")

        resolved_source_path = None
        if source_path:
            resolved_source_path = _resolve_source_path(request, source_path)
        elif workspace.source_manifest_path.exists():
            resolved_source_path = _source_path_from_manifest(workspace)

        if view_type == "original_text":
            return _original_text_view(project_id, resolved_source_path)
        if view_type == "chunks":
            return _chunks_view(workspace)
        if view_type == "scene_summary":
            return _scene_summary_view(workspace)
        if view_type == "character_summary":
            return _character_summary_view(workspace)
        if view_type == "scripts":
            return _scripts_view(workspace, chunk_id)

        raise HTTPException(status_code=404, detail="unknown view type")

    @app.post("/api/stage1/jobs")
    def start_stage1_job(
        request: Request,
        payload: Stage1JobRequest,
    ) -> dict[str, Any]:
        workspace = _workspace(request, payload.project_id)
        if not workspace.chunks_path.exists():
            raise HTTPException(status_code=404, detail="chunks artifact not found")

        response_dir = None
        if payload.response_dir:
            response_dir = _resolve_existing_dir(payload.response_dir)

        registry: JobRegistry = request.app.state.jobs
        job = registry.create(payload.project_id, "stage1", "all")
        thread = threading.Thread(
            target=_run_stage1_job,
            args=(request.app, job.job_id, payload, response_dir),
            daemon=True,
        )
        thread.start()
        return job.to_dict()

    @app.post("/api/stage2/jobs")
    def start_stage2_job(
        request: Request,
        payload: Stage2JobRequest,
    ) -> dict[str, Any]:
        workspace = _workspace(request, payload.project_id)
        selection = _stage2_selection(payload)
        chunk_ids = _selected_chunk_ids(workspace, selection)

        response_path = None
        if payload.response_path:
            response_path = _resolve_existing_path(payload.response_path)
        response_dir = None
        if payload.response_dir:
            response_dir = _resolve_existing_dir(payload.response_dir)
        if response_path is not None and len(chunk_ids) != 1:
            raise HTTPException(
                status_code=400,
                detail="response_path is only valid when one chunk is selected",
            )

        registry: JobRegistry = request.app.state.jobs
        job = registry.create(payload.project_id, "stage2", selection)
        thread = threading.Thread(
            target=_run_stage2_job,
            args=(
                request.app,
                job.job_id,
                payload,
                selection,
                chunk_ids,
                response_path,
                response_dir,
            ),
            daemon=True,
        )
        thread.start()
        return job.to_dict()

    @app.get("/api/jobs/{job_id}")
    def get_job(request: Request, job_id: str) -> dict[str, Any]:
        registry: JobRegistry = request.app.state.jobs
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    @app.get("/api/stage2/jobs/{job_id}")
    def get_stage2_job(request: Request, job_id: str) -> dict[str, Any]:
        registry: JobRegistry = request.app.state.jobs
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    @app.get("/api/projects/{project_id}/script/{chunk_id}")
    def get_script(
        request: Request,
        project_id: str,
        chunk_id: str,
    ) -> dict[str, Any]:
        workspace = _workspace(request, project_id)
        artifact_path = workspace.script_artifact_path(chunk_id)
        if not artifact_path.exists():
            raise HTTPException(status_code=404, detail="script artifact not found")
        return {
            "project_id": project_id,
            **get_script_payload(workspace, chunk_id),
        }

    return app


def _run_stage1_job(
    app: FastAPI,
    job_id: str,
    payload: Stage1JobRequest,
    response_dir: Path | None,
) -> None:
    registry: JobRegistry = app.state.jobs
    workspace_root: Path = app.state.workspace_root

    def on_progress(progress: ContextProfileProgress) -> None:
        def update(job: PipelineJob) -> None:
            job.status = progress.status
            job.total_chunks = progress.total_chunks
            job.completed_chunks = progress.processed_chunks
            job.current_chunk_id = progress.chunk_id
            job.errors = progress.errors

        registry.update(job_id, update)

    try:
        result = run_chunk_context_profiler_workflow(
            payload.project_id,
            response_dir=response_dir,
            workspace_root=workspace_root,
            progress_callback=on_progress,
        )

        def complete(job: PipelineJob) -> None:
            job.status = "complete"
            job.total_chunks = len(result.artifacts)
            job.completed_chunks = len(result.artifacts)
            job.current_chunk_id = None
            job.errors = []
            job.artifact_paths = {
                "context_dir": str(result.workspace.context_ir_dir),
                "characters": str(result.workspace.character_registry_path),
            }

        registry.update(job_id, complete)
    except Exception as exc:
        error_message = str(exc)
        workspace = Workspace(payload.project_id, root=workspace_root)

        def fail(job: PipelineJob) -> None:
            job.status = "failed"
            if not job.errors:
                job.errors = [error_message]
            job.current_chunk_id = None
            job.artifact_paths = {
                "context_dir": str(workspace.context_ir_dir),
                "characters": str(workspace.character_registry_path),
            }

        registry.update(job_id, fail)


def _run_stage2_job(
    app: FastAPI,
    job_id: str,
    payload: Stage2JobRequest,
    selection: str,
    chunk_ids: list[str],
    response_path: Path | None,
    response_dir: Path | None,
) -> None:
    registry: JobRegistry = app.state.jobs
    workspace_root: Path = app.state.workspace_root
    workspace = Workspace(payload.project_id, root=workspace_root)
    completed_chunk_ids: set[str] = set()

    def on_progress(progress: ScriptProgress) -> None:
        def update(job: PipelineJob) -> None:
            job.status = progress.status
            job.total_chunks = len(chunk_ids)
            job.completed_chunks = len(completed_chunk_ids) + (
                1 if progress.status in {"attempt_complete", "complete"} else 0
            )
            job.current_chunk_id = (
                progress.chunk_id
                if progress.status not in {"attempt_complete", "complete"}
                else None
            )
            job.errors = progress.errors

        registry.update(job_id, update)

    try:
        results = []
        for chunk_id in chunk_ids:
            chunk_path = workspace.chunks_dir / f"{chunk_id}.txt"
            if not chunk_path.exists():
                raise RuntimeError(f"chunk text not found: {chunk_path}")
            selected_response_path = response_path
            if response_dir is not None:
                selected_response_path = response_dir / f"{chunk_id}_response.json"
                if not selected_response_path.exists():
                    raise RuntimeError(
                        f"Missing Stage 2 response fixture: {selected_response_path}"
                    )

            result = run_script_conversion_workflow(
                chunk_path,
                payload.project_id,
                chunk_id,
                response_path=selected_response_path,
                max_retries=payload.max_retries,
                workspace_root=workspace_root,
                progress_callback=on_progress,
            )
            results.append(result)
            completed_chunk_ids.add(chunk_id)

            def chunk_complete(job: PipelineJob) -> None:
                job.status = "chunk_complete"
                job.total_chunks = len(chunk_ids)
                job.completed_chunks = len(completed_chunk_ids)
                job.current_chunk_id = None
                job.errors = result.errors
                job.artifact_paths[f"{chunk_id}_script"] = str(
                    result.workspace.script_artifact_path(chunk_id)
                )
                job.artifact_paths[f"{chunk_id}_validation_report"] = str(
                    result.validation_report_path
                )

            registry.update(job_id, chunk_complete)

        assembly_errors: list[str] = []
        if selection == "all" or _all_chunk_scripts_exist(workspace):
            try:
                assembly = run_script_assembly_workflow(
                    payload.project_id,
                    workspace_root=workspace_root,
                )
            except Exception as exc:
                assembly_errors = [f"script assembly failed: {exc}"]
            else:
                def assembly_complete(job: PipelineJob) -> None:
                    job.artifact_paths["script"] = str(
                        assembly.workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID)
                    )
                    job.artifact_paths["validation_report"] = str(
                        assembly.validation_report_path
                    )

                registry.update(job_id, assembly_complete)

        def complete(job: PipelineJob) -> None:
            job.status = "complete"
            job.total_chunks = len(chunk_ids)
            job.completed_chunks = len(completed_chunk_ids)
            job.current_chunk_id = None
            job.errors = assembly_errors
            if len(results) == 1:
                chunk_id = results[0].artifact.chunk_id
                job.artifact_paths.setdefault(
                    "script",
                    str(results[0].workspace.script_artifact_path(chunk_id)),
                )
                job.artifact_paths.setdefault(
                    "validation_report",
                    str(results[0].validation_report_path),
                )

        registry.update(job_id, complete)
    except Exception as exc:
        error_message = str(exc)

        def fail(job: PipelineJob) -> None:
            job.status = "failed"
            if not job.errors:
                job.errors = [error_message]
            job.current_chunk_id = None
            for chunk_id in chunk_ids:
                script_path = workspace.script_artifact_path(chunk_id)
                report_path = workspace.script_validation_report_path(chunk_id)
                if script_path.exists():
                    job.artifact_paths[f"{chunk_id}_script"] = str(script_path)
                if report_path.exists():
                    job.artifact_paths[f"{chunk_id}_validation_report"] = str(
                        report_path
                    )

        registry.update(job_id, fail)


def _chunks_response(workspace: Workspace) -> dict[str, Any]:
    artifact = _read_json(workspace.chunks_path)
    report = (
        _read_json(workspace.validation_report_path)
        if workspace.validation_report_path.exists()
        else None
    )
    chunks = []
    for chunk in artifact.get("chunks", []):
        text_path = workspace.chunk_text_path(int(chunk["index"]))
        text = text_path.read_text(encoding="utf-8") if text_path.exists() else chunk["text"]
        chunks.append(
            {
                **chunk,
                "text": text,
                "text_path": str(text_path),
                "character_count": len(text),
            }
        )
    return {
        "project_id": workspace.project_id,
        "project_root": str(workspace.project_root),
        "artifact": artifact,
        "validation_report": report,
        "chunks": chunks,
    }


def _original_text_view(
    project_id: str,
    source_path: Path | None,
) -> dict[str, Any]:
    if source_path is None or not source_path.exists():
        return _empty_view(
            project_id,
            "original_text",
            "Select a source file or run chunking to create a source manifest.",
        )
    text = source_path.read_text(encoding="utf-8")
    return {
        "project_id": project_id,
        "view_type": "original_text",
        "available": True,
        "source": {
            "name": source_path.name,
            "path": str(source_path),
            "character_count": len(text),
            "text": text,
        },
    }


def _chunks_view(workspace: Workspace) -> dict[str, Any]:
    if not workspace.chunks_path.exists():
        return _empty_view(
            workspace.project_id,
            "chunks",
            "No chunks artifact exists for this project.",
        )
    return {
        "view_type": "chunks",
        "available": True,
        **_chunks_response(workspace),
    }


def _scene_summary_view(workspace: Workspace) -> dict[str, Any]:
    context_paths = sorted(workspace.context_ir_dir.glob("*_context.json"))
    if not context_paths:
        return _empty_view(
            workspace.project_id,
            "scene_summary",
            "No Stage 1 context artifacts exist for this project.",
        )

    contexts = [_read_json(path) for path in context_paths]
    sections = []
    for context_artifact in contexts:
        context = context_artifact.get("context", {})
        sections.append(
            {
                "chunk_id": context_artifact.get("chunk_id"),
                "scene_summary": context.get("scene_summary"),
                "active_characters": context.get("active_characters", []),
                "important_context": context.get("important_context", []),
                "aliases_observed": context.get("aliases_observed", []),
                "unresolved_pronouns": context.get("unresolved_pronouns", []),
                "confidence": context.get("confidence"),
                "review_notes": context.get("review_notes", []),
                "artifact": context_artifact,
            }
        )
    return {
        "project_id": workspace.project_id,
        "view_type": "scene_summary",
        "available": True,
        "contexts": contexts,
        "sections": sections,
    }


def _character_summary_view(workspace: Workspace) -> dict[str, Any]:
    if not workspace.character_registry_path.exists():
        return _empty_view(
            workspace.project_id,
            "character_summary",
            "No character registry exists for this project.",
        )
    registry = _read_json(workspace.character_registry_path)
    return {
        "project_id": workspace.project_id,
        "view_type": "character_summary",
        "available": True,
        "registry": registry,
        "characters": registry.get("characters", []),
    }


def _scripts_view(
    workspace: Workspace,
    chunk_id: str | None,
) -> dict[str, Any]:
    continuous_payload = _continuous_script_payload(workspace)
    if continuous_payload is None:
        return _empty_view(
            workspace.project_id,
            "scripts",
            "No script artifacts exist for this project.",
        )

    return {
        "project_id": workspace.project_id,
        "view_type": "scripts",
        "available": True,
        "requested_chunk_id": chunk_id,
        "script_options": _script_options(workspace),
        **continuous_payload,
    }


def get_script_payload(workspace: Workspace, chunk_id: str) -> dict[str, Any]:
    artifact_path = workspace.script_artifact_path(chunk_id)
    report_path = workspace.script_validation_report_path(chunk_id)
    artifact = _read_json(artifact_path)
    report = _read_json(report_path) if report_path.exists() else None
    source_text = _source_text_for_script_artifact(workspace, artifact)
    segments = _script_segments_with_validation(
        source_text,
        artifact.get("segments", []),
        report or {},
    )
    return {
        "chunk_id": chunk_id,
        "artifact": artifact,
        "validation_report": report,
        "segments": segments,
    }


def _continuous_script_payload(workspace: Workspace) -> dict[str, Any] | None:
    for artifact_path in _preferred_complete_script_paths(workspace):
        if artifact_path.exists():
            chunk_id = _chunk_id_from_script_artifact_path(artifact_path)
            artifact = _read_json(artifact_path)
            report_path = workspace.script_validation_report_path(
                COMPLETE_SCRIPT_CHUNK_ID
            )
            report = _read_json(report_path) if report_path.exists() else None
            source_text = _source_text_for_script_artifact(workspace, artifact)
            segments = _script_segments_with_validation(
                source_text,
                artifact.get("segments", []),
                report or {},
            )
            return {
                "chunk_id": chunk_id,
                "selected_chunk_id": chunk_id,
                "script_source": "continuous",
                "artifact": artifact,
                "validation_report": report,
                "segments": segments,
            }

    stitched = _stitched_chunk_script_payload(workspace)
    if stitched is not None:
        return stitched

    script_options = _script_options(workspace)
    if not script_options:
        return None
    selected_chunk_id = _select_script_chunk_id(script_options, None)
    return {
        "script_source": "single_chunk",
        **get_script_payload(workspace, selected_chunk_id),
        "selected_chunk_id": selected_chunk_id,
    }


def _preferred_complete_script_paths(workspace: Workspace) -> list[Path]:
    return [
        workspace.key_reviewed_script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
        workspace.normalized_script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
        workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID),
    ]


def _stitched_chunk_script_payload(workspace: Workspace) -> dict[str, Any] | None:
    if not workspace.chunks_path.exists():
        return None
    chunks = _read_json(workspace.chunks_path).get("chunks", [])
    segments = []
    chunk_ids = []
    reports = []
    for chunk in chunks:
        chunk_id = str(chunk["chunk_id"])
        if chunk_id == COMPLETE_SCRIPT_CHUNK_ID:
            continue
        if not workspace.script_artifact_path(chunk_id).exists():
            continue
        payload = get_script_payload(workspace, chunk_id)
        chunk_ids.append(chunk_id)
        reports.append(payload.get("validation_report"))
        for segment in payload["segments"]:
            segments.append({**segment, "chunk_id": chunk_id})

    if not segments:
        return None
    return {
        "chunk_id": "stitched_available_chunks",
        "selected_chunk_id": "stitched_available_chunks",
        "script_source": "stitched_chunks",
        "artifact": None,
        "validation_report": {
            "exact_reconstruction_success": all(
                report and report.get("exact_reconstruction_success")
                for report in reports
            ),
            "chunk_ids": chunk_ids,
        },
        "segments": segments,
    }


def _source_text_for_script_artifact(
    workspace: Workspace,
    artifact: dict[str, Any],
) -> str:
    if artifact.get("chunk_id") == COMPLETE_SCRIPT_CHUNK_ID:
        return "".join(
            chunk.get("text", "")
            for chunk in _read_json(workspace.chunks_path).get("chunks", [])
        )

    chunk_source_path = Path(artifact["chunk_source_path"])
    if not chunk_source_path.is_absolute():
        chunk_source_path = Path.cwd() / chunk_source_path
    return (
        chunk_source_path.read_text(encoding="utf-8")
        if chunk_source_path.exists()
        else ""
    )


def _chunk_id_from_script_artifact_path(path: Path) -> str:
    name = path.name
    for suffix in (
        "_key_reviewed_script.json",
        "_normalized_script.json",
        "_script.json",
    ):
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def _script_options(workspace: Workspace) -> list[dict[str, Any]]:
    options = []
    for path in sorted(workspace.script_ir_dir.glob("*_script.json")):
        chunk_id = path.name.removesuffix("_script.json")
        if chunk_id.endswith("_normalized") or chunk_id.endswith("_key_reviewed"):
            continue
        try:
            artifact = _read_json(path)
        except json.JSONDecodeError:
            continue
        report_path = workspace.script_validation_report_path(chunk_id)
        report = _read_json(report_path) if report_path.exists() else None
        options.append(
            {
                "chunk_id": chunk_id,
                "path": str(path),
                "segment_count": len(artifact.get("segments", [])),
                "exact_reconstruction_success": (
                    report.get("exact_reconstruction_success") if report else None
                ),
            }
        )
    return options


def _select_script_chunk_id(
    script_options: list[dict[str, Any]],
    chunk_id: str | None,
) -> str:
    option_ids = {str(option["chunk_id"]) for option in script_options}
    if chunk_id in option_ids:
        return str(chunk_id)
    if "complete" in option_ids:
        return "complete"
    return str(script_options[0]["chunk_id"])


def _select_chunk_artifact(
    artifacts: list[dict[str, Any]],
    chunk_id: str | None,
) -> dict[str, Any] | None:
    if not artifacts:
        return None
    for artifact in artifacts:
        if artifact.get("chunk_id") == chunk_id:
            return artifact
    return artifacts[0]


def _empty_view(
    project_id: str,
    view_type: str,
    message: str,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "view_type": view_type,
        "available": False,
        "message": message,
    }


def _script_segments_with_validation(
    source_text: str,
    segments: list[dict[str, Any]],
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    expected_start = 0
    report_errors = report.get("errors", [])
    output = []
    for segment in segments:
        segment_id = segment["segment_id"]
        span = segment["source_span"]
        text = next(iter(segment["script"].values()))
        errors = []
        if any(segment_id in error for error in report_errors):
            errors.extend(error for error in report_errors if segment_id in error)
        if span["start"] != expected_start and normalize_content_text(
            source_text[expected_start : span["start"]]
        ):
            errors.append(f"span starts at {span['start']}, expected {expected_start}")
        if span["end"] <= span["start"]:
            errors.append("span is empty or negative")
        if span["end"] > len(source_text):
            errors.append("span ends beyond source text")
            source_slice = ""
        else:
            source_slice = source_text[span["start"] : span["end"]]
        if normalize_content_text(text) != normalize_content_text(source_slice):
            errors.append("script text does not match source span")
        expected_start = span["end"]
        output.append(
            {
                **segment,
                "speaker": next(iter(segment["script"])),
                "text": text,
                "validation_status": "failed" if errors else "passed",
                "validation_errors": errors,
            }
        )
    return output


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _view_available(
    workspace: Workspace,
    view_type: str,
    *,
    source_path: Path | None,
) -> bool:
    if view_type == "original_text":
        return source_path is not None and source_path.exists()
    if view_type == "chunks":
        return workspace.chunks_path.exists()
    if view_type == "scene_summary":
        return any(workspace.context_ir_dir.glob("*_context.json"))
    if view_type == "character_summary":
        return workspace.character_registry_path.exists()
    if view_type == "scripts":
        return _continuous_script_payload(workspace) is not None
    return False


def _stage2_selection(payload: Stage2JobRequest) -> str:
    selection = (payload.selection or payload.chunk_id or "").strip()
    if not selection:
        raise HTTPException(status_code=400, detail="selection or chunk_id is required")
    return selection


def _selected_chunk_ids(workspace: Workspace, selection: str) -> list[str]:
    if not workspace.chunks_path.exists():
        raise HTTPException(status_code=404, detail="chunks artifact not found")
    chunks = _read_json(workspace.chunks_path).get("chunks", [])
    chunk_ids = [str(chunk["chunk_id"]) for chunk in chunks]
    if selection == "all":
        return chunk_ids
    if selection not in chunk_ids:
        raise HTTPException(status_code=404, detail="selected chunk not found")
    return [selection]


def _all_chunk_scripts_exist(workspace: Workspace) -> bool:
    if not workspace.chunks_path.exists():
        return False
    chunks = _read_json(workspace.chunks_path).get("chunks", [])
    return all(
        workspace.script_artifact_path(str(chunk["chunk_id"])).exists()
        for chunk in chunks
    )


def _workspace(request: Request, project_id: str) -> Workspace:
    return Workspace(project_id, root=_state_path(request, "workspace_root"))


def _state_path(request: Request, key: str) -> Path:
    return Path(getattr(request.app.state, key))


def _resolve_source_path(request: Request, source_path: str) -> Path:
    raw_root = _state_path(request, "raw_dir").resolve()
    candidate = Path(source_path)
    if not candidate.is_absolute():
        if candidate.exists():
            candidate = candidate.resolve()
        else:
            candidate = (raw_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if raw_root not in candidate.parents and candidate != raw_root:
        raise HTTPException(status_code=400, detail="source path must be under data/raw")
    if candidate.suffix != ".txt" or not candidate.exists():
        raise HTTPException(status_code=404, detail="source text file not found")
    return candidate


def _source_path_from_manifest(workspace: Workspace) -> Path | None:
    if not workspace.source_manifest_path.exists():
        return None
    manifest = _read_json(workspace.source_manifest_path)
    raw_source_path = str(manifest.get("source_path", "")).strip()
    if not raw_source_path:
        return None
    source_path = Path(raw_source_path)
    if not source_path.is_absolute():
        source_path = (Path.cwd() / source_path).resolve()
    return source_path


def _resolve_existing_path(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="response path not found")
    return candidate


def _resolve_existing_dir(path: str) -> Path:
    candidate = _resolve_existing_path(path)
    if not candidate.is_dir():
        raise HTTPException(status_code=400, detail="response dir is not a directory")
    return candidate


def _default_project_id(source_path: Path) -> str:
    project_id = re.sub(r"[^\w-]+", "_", source_path.stem, flags=re.UNICODE)
    return project_id.strip("_").lower() or "project"


app = create_app()
