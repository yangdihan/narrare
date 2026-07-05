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

from core.pipeline.chunking import run_chunking_workflow
from core.pipeline.script_conversion import (
    ScriptProgress,
    run_script_conversion_workflow,
)
from core.validation.script_integrity import normalize_content_text
from storage.workspace import Workspace

BASE_DIR = Path(__file__).resolve().parent


class ChunkRequest(BaseModel):
    source_path: str
    project_id: str


class Stage2JobRequest(BaseModel):
    project_id: str
    chunk_id: str
    response_path: str | None = None
    max_windows: int | None = None
    max_retries: int = 1


@dataclass
class Stage2Job:
    job_id: str
    project_id: str
    chunk_id: str
    status: str = "queued"
    total_windows: int = 0
    processed_windows: int = 0
    current_window_id: str | None = None
    errors: list[str] = field(default_factory=list)
    artifact_path: str | None = None
    validation_report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "chunk_id": self.chunk_id,
            "status": self.status,
            "total_windows": self.total_windows,
            "processed_windows": self.processed_windows,
            "current_window_id": self.current_window_id,
            "errors": self.errors,
            "artifact_path": self.artifact_path,
            "validation_report_path": self.validation_report_path,
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Stage2Job] = {}
        self._lock = threading.Lock()

    def create(self, project_id: str, chunk_id: str) -> Stage2Job:
        job = Stage2Job(
            job_id=uuid.uuid4().hex,
            project_id=project_id,
            chunk_id=chunk_id,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Stage2Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, updater: Callable[[Stage2Job], None]) -> None:
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

    @app.post("/api/stage2/jobs")
    def start_stage2_job(
        request: Request,
        payload: Stage2JobRequest,
    ) -> dict[str, Any]:
        workspace = _workspace(request, payload.project_id)
        chunk_path = workspace.chunks_dir / f"{payload.chunk_id}.txt"
        if not chunk_path.exists():
            raise HTTPException(status_code=404, detail="chunk text not found")

        response_path = None
        if payload.response_path:
            response_path = _resolve_existing_path(payload.response_path)

        registry: JobRegistry = request.app.state.jobs
        job = registry.create(payload.project_id, payload.chunk_id)
        thread = threading.Thread(
            target=_run_stage2_job,
            args=(request.app, job.job_id, payload, chunk_path, response_path),
            daemon=True,
        )
        thread.start()
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
        report_path = workspace.script_validation_report_path(chunk_id)
        if not artifact_path.exists():
            raise HTTPException(status_code=404, detail="script artifact not found")

        artifact = _read_json(artifact_path)
        report = _read_json(report_path) if report_path.exists() else None
        chunk_source_path = Path(artifact["chunk_source_path"])
        if not chunk_source_path.is_absolute():
            chunk_source_path = Path.cwd() / chunk_source_path
        source_text = (
            chunk_source_path.read_text(encoding="utf-8")
            if chunk_source_path.exists()
            else ""
        )
        segments = _script_segments_with_validation(
            source_text,
            artifact.get("segments", []),
            report or {},
        )
        return {
            "project_id": project_id,
            "chunk_id": chunk_id,
            "artifact": artifact,
            "validation_report": report,
            "segments": segments,
        }

    return app


def _run_stage2_job(
    app: FastAPI,
    job_id: str,
    payload: Stage2JobRequest,
    chunk_path: Path,
    response_path: Path | None,
) -> None:
    registry: JobRegistry = app.state.jobs
    workspace_root: Path = app.state.workspace_root

    def on_progress(progress: ScriptProgress) -> None:
        def update(job: Stage2Job) -> None:
            job.status = progress.status
            job.total_windows = 1
            job.processed_windows = (
                1 if progress.status in {"attempt_complete", "complete"} else 0
            )
            job.current_window_id = (
                progress.chunk_id
                if progress.status not in {"attempt_complete", "complete"}
                else None
            )
            job.errors = progress.errors

        registry.update(job_id, update)

    try:
        result = run_script_conversion_workflow(
            chunk_path,
            payload.project_id,
            payload.chunk_id,
            response_path=response_path,
            max_retries=payload.max_retries,
            workspace_root=workspace_root,
            progress_callback=on_progress,
        )

        def complete(job: Stage2Job) -> None:
            job.status = "complete"
            job.total_windows = result.artifact.processed_chunk_count
            job.processed_windows = result.artifact.processed_chunk_count
            job.current_window_id = None
            job.errors = result.errors
            job.artifact_path = str(result.workspace.script_artifact_path(job.chunk_id))
            job.validation_report_path = str(result.validation_report_path)

        registry.update(job_id, complete)
    except Exception as exc:
        error_message = str(exc)
        workspace = Workspace(payload.project_id, root=workspace_root)

        def fail(job: Stage2Job) -> None:
            job.status = "failed"
            if not job.errors:
                job.errors = [error_message]
            job.current_window_id = None
            script_path = workspace.script_artifact_path(job.chunk_id)
            report_path = workspace.script_validation_report_path(job.chunk_id)
            job.artifact_path = str(script_path) if script_path.exists() else None
            job.validation_report_path = str(report_path) if report_path.exists() else None

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


def _resolve_existing_path(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="response path not found")
    return candidate


def _default_project_id(source_path: Path) -> str:
    project_id = re.sub(r"[^\w-]+", "_", source_path.stem, flags=re.UNICODE)
    return project_id.strip("_").lower() or "project"


app = create_app()
