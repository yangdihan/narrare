from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core.models.character import CharacterCurationAddition, CharacterCurationUpdate
from core.pipeline.character_review import (
    add_character,
    apply_character_curation,
    apply_character_edits,
    apply_script_content_edits,
    apply_script_speaker_edits,
    merge_character,
    rename_character,
    speaker_options,
    update_script_segment_speaker,
)
from core.pipeline.chunk_context_profiler import (
    ContextProfileProgress,
    run_chunk_context_profiler_workflow,
)
from core.pipeline.chunking import run_chunking_workflow
from core.pipeline.qwen_tts import qwen_delete_readiness_report
from core.pipeline.script_artifact_selection import preferred_script_artifact_paths
from core.pipeline.script_assembly import (
    COMPLETE_SCRIPT_CHUNK_ID,
    run_script_assembly_workflow,
)
from core.pipeline.script_conversion import (
    ScriptProgress,
    run_script_conversion_workflow,
)
from core.pipeline.speaker_key_review import (
    DEFAULT_REVIEW_MAX_OUTPUT_TOKENS,
    SpeakerKeyReviewProgress,
    run_speaker_key_review_workflow,
)
from core.pipeline.voice_assets import load_voice_inventory
from core.pipeline.voice_assignment import (
    AudioGenerationProgress,
    build_voice_assignment_view,
    generate_audio_take,
    generate_voice_sample,
    list_audio_takes,
    run_audio_generation_workflow,
    save_voice_assignments,
    select_audio_take,
    selected_audio_take_numbers,
)
from core.validation.script_integrity import normalize_content_text
from storage.workspace import Workspace
from tts.dummy import DummyTTSAdapter
from tts.qwen.adapter import QwenTTSAdapter

BASE_DIR = Path(__file__).resolve().parent

VIEW_OPTIONS = [
    {"id": "original_text", "label": "Original Text"},
    {"id": "characters", "label": "Characters"},
    {"id": "scripts", "label": "Scripts"},
]

VIEW_ALIASES = {
    "character_summary": "characters",
    "voice_assignment": "characters",
    "voice-assignment": "characters",
    "voice_assignments": "characters",
    "voices": "characters",
    "chunks": "chunks",
    "scene_summary": "scene_summary",
}


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


class Stage3JobRequest(BaseModel):
    project_id: str
    response_dir: str | None = None
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    batch_size: int = Field(default=16, ge=1, le=64)
    max_output_tokens: int = Field(
        default=DEFAULT_REVIEW_MAX_OUTPUT_TOKENS, ge=256, le=4_096
    )


class VoiceSampleRequest(BaseModel):
    speaker: str
    voice_profile_id: str


class VoiceAssignmentRequest(BaseModel):
    assignments: dict[str, str]


class AudioGenerationJobRequest(BaseModel):
    assignments: dict[str, str]
    only_missing: bool = True


class SegmentAudioTakeJobRequest(BaseModel):
    segment_id: str


class AudioTakeSelectionRequest(BaseModel):
    take_number: int = Field(ge=1)


class CharacterAddRequest(BaseModel):
    name: str


class CharacterMergeRequest(BaseModel):
    source_character_id: str
    target_character_id: str


class ScriptSpeakerMergeRequest(BaseModel):
    source_speaker: str
    target_character_id: str


class CharacterRenameRequest(BaseModel):
    character_id: str
    name: str


class CharacterEditSaveRequest(BaseModel):
    additions: list[str] = []
    renames: dict[str, str] = {}
    merges: list[CharacterMergeRequest] = []


class CharacterCurationSaveRequest(BaseModel):
    additions: list[CharacterCurationAddition] = []
    updates: list[CharacterCurationUpdate] = []
    removals: list[str] = []
    merges: list[CharacterMergeRequest] = []
    script_speaker_merges: list[ScriptSpeakerMergeRequest] = []
    voice_profile_by_character_id: dict[str, str] = {}
    system_voice_assignments: dict[str, str] = {}


class ScriptSpeakerUpdateRequest(BaseModel):
    segment_id: str
    speaker: str
    chunk_id: str | None = None


class ScriptSpeakerEditSaveRequest(BaseModel):
    edits: list[ScriptSpeakerUpdateRequest] = []


class ScriptContentUpdateRequest(BaseModel):
    segment_id: str
    speaker: str
    text: str
    chunk_id: str | None = None


class ScriptInsertRequest(BaseModel):
    after_segment_id: str | None = None
    speaker: str
    text: str
    chunk_id: str | None = None


class ScriptEditSaveRequest(BaseModel):
    updates: list[ScriptContentUpdateRequest] = []
    inserts: list[ScriptInsertRequest] = []


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
    current_speaker: str | None = None
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
            "total_segments": self.total_chunks,
            "completed_segments": self.completed_chunks,
            "current_segment_id": self.current_chunk_id,
            "current_speaker": self.current_speaker,
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

    def latest_active(self, project_id: str, phase: str) -> PipelineJob | None:
        with self._lock:
            return next(
                (
                    job
                    for job in reversed(list(self._jobs.values()))
                    if job.project_id == project_id
                    and job.phase == phase
                    and job.status in {"queued", "running"}
                ),
                None,
            )

    def update(self, job_id: str, updater: Callable[[PipelineJob], None]) -> None:
        with self._lock:
            updater(self._jobs[job_id])


def create_app(
    *,
    raw_dir: str | Path = "data/raw",
    workspace_root: str | Path = "data/interim",
    voice_inventory_path: str | Path = "data/voices/qwen/voice_profiles.json",
    tts_adapter_name: str | None = None,
) -> FastAPI:
    app = FastAPI(title="Narrare Pipeline")
    app.state.raw_dir = Path(raw_dir)
    app.state.workspace_root = Path(workspace_root)
    app.state.voice_inventory_path = Path(voice_inventory_path)
    app.state.jobs = JobRegistry()
    app.state.tts_adapter_name = tts_adapter_name

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
        view_type = _canonical_view_type(view_type)

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
        if view_type == "characters":
            return _characters_view(request, workspace)
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

    @app.post("/api/stage3/jobs")
    def start_stage3_job(
        request: Request,
        payload: Stage3JobRequest,
    ) -> dict[str, Any]:
        workspace = _workspace(request, payload.project_id)
        missing_inputs = _stage3_missing_inputs(workspace)
        if missing_inputs:
            raise HTTPException(
                status_code=409,
                detail="Stage 3 requires " + ", ".join(missing_inputs),
            )

        response_dir = None
        if payload.response_dir:
            response_dir = _resolve_existing_dir(payload.response_dir)

        registry: JobRegistry = request.app.state.jobs
        job = registry.create(payload.project_id, "stage3", "complete")
        thread = threading.Thread(
            target=_run_stage3_job,
            args=(request.app, job.job_id, payload, response_dir),
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

    @app.get("/api/projects/{project_id}/audio/jobs/active")
    def get_active_audio_job(request: Request, project_id: str) -> dict[str, Any]:
        registry: JobRegistry = request.app.state.jobs
        job = registry.latest_active(project_id, "tts")
        return {"job": job.to_dict() if job is not None else None}

    @app.get("/api/projects/{project_id}/audio/playlist")
    def get_audio_playlist(request: Request, project_id: str) -> dict[str, Any]:
        payload = _scripts_view(_workspace(request, project_id), None)
        if not payload.get("available"):
            raise HTTPException(status_code=404, detail="script artifact not found")

        items = []
        missing_segment_ids = []
        for segment in payload.get("segments", []):
            selected_take = next(
                (
                    take
                    for take in segment.get("audio_takes", [])
                    if take.get("selected") and take.get("audio_url")
                ),
                None,
            )
            if selected_take is None:
                missing_segment_ids.append(str(segment.get("segment_id", "")))
                continue
            items.append(
                {
                    "segment_id": segment["segment_id"],
                    "speaker": segment["speaker"],
                    "take_number": selected_take["take_number"],
                    "audio_url": selected_take["audio_url"],
                }
            )
        return {
            "project_id": project_id,
            "ready": not missing_segment_ids,
            "items": items,
            "missing_segment_ids": missing_segment_ids,
        }

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

    @app.post("/api/projects/{project_id}/characters")
    def add_character_endpoint(
        request: Request,
        project_id: str,
        payload: CharacterAddRequest,
    ) -> dict[str, Any]:
        try:
            add_character(
                project_id,
                payload.name,
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _characters_view(request, _workspace(request, project_id))

    @app.post("/api/projects/{project_id}/characters/merge")
    def merge_character_endpoint(
        request: Request,
        project_id: str,
        payload: CharacterMergeRequest,
    ) -> dict[str, Any]:
        try:
            merge_character(
                project_id,
                payload.source_character_id,
                payload.target_character_id,
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _characters_view(request, _workspace(request, project_id))

    @app.patch("/api/projects/{project_id}/characters/rename")
    def rename_character_endpoint(
        request: Request,
        project_id: str,
        payload: CharacterRenameRequest,
    ) -> dict[str, Any]:
        try:
            rename_character(
                project_id,
                payload.character_id,
                payload.name,
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _characters_view(request, _workspace(request, project_id))

    @app.post("/api/projects/{project_id}/character-edits")
    def save_character_edits_endpoint(
        request: Request,
        project_id: str,
        payload: CharacterEditSaveRequest,
    ) -> dict[str, Any]:
        try:
            apply_character_edits(
                project_id,
                additions=payload.additions,
                renames=payload.renames,
                merges=[
                    (merge.source_character_id, merge.target_character_id)
                    for merge in payload.merges
                ],
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _characters_view(request, _workspace(request, project_id))

    @app.post("/api/projects/{project_id}/characters/curation")
    def save_character_curation_endpoint(
        request: Request,
        project_id: str,
        payload: CharacterCurationSaveRequest,
    ) -> dict[str, Any]:
        try:
            apply_character_curation(
                project_id,
                additions=payload.additions,
                updates=payload.updates,
                removals=payload.removals,
                merges=[
                    (merge.source_character_id, merge.target_character_id)
                    for merge in payload.merges
                ],
                script_speaker_merges=[
                    (merge.source_speaker, merge.target_character_id)
                    for merge in payload.script_speaker_merges
                ],
                voice_profile_by_character_id=payload.voice_profile_by_character_id,
                system_voice_assignments=payload.system_voice_assignments,
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _characters_view(request, _workspace(request, project_id))

    @app.patch("/api/projects/{project_id}/script-speakers")
    def update_script_speaker_endpoint(
        request: Request,
        project_id: str,
        payload: ScriptSpeakerUpdateRequest,
    ) -> dict[str, Any]:
        try:
            update_script_segment_speaker(
                project_id,
                payload.segment_id,
                payload.speaker,
                chunk_id=payload.chunk_id,
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _scripts_view(_workspace(request, project_id), payload.chunk_id)

    @app.post("/api/projects/{project_id}/script-speaker-edits")
    def save_script_speaker_edits_endpoint(
        request: Request,
        project_id: str,
        payload: ScriptSpeakerEditSaveRequest,
    ) -> dict[str, Any]:
        try:
            apply_script_speaker_edits(
                project_id,
                [
                    (edit.segment_id, edit.speaker, edit.chunk_id)
                    for edit in payload.edits
                ],
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _scripts_view(_workspace(request, project_id), None)

    @app.post("/api/projects/{project_id}/script-edits")
    def save_script_edits_endpoint(
        request: Request,
        project_id: str,
        payload: ScriptEditSaveRequest,
    ) -> dict[str, Any]:
        try:
            apply_script_content_edits(
                project_id,
                [
                    (edit.segment_id, edit.speaker, edit.text, edit.chunk_id)
                    for edit in payload.updates
                ],
                [
                    (insert.after_segment_id, insert.speaker, insert.text, insert.chunk_id)
                    for insert in payload.inserts
                ],
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _scripts_view(_workspace(request, project_id), None)

    @app.post("/api/projects/{project_id}/voice-samples")
    def generate_voice_sample_endpoint(
        request: Request,
        project_id: str,
        payload: VoiceSampleRequest,
    ) -> dict[str, Any]:
        _ensure_tts_generation_enabled(request)
        try:
            result = generate_voice_sample(
                project_id,
                payload.speaker,
                payload.voice_profile_id,
                workspace_root=_state_path(request, "workspace_root"),
                voice_inventory_path=_state_path(request, "voice_inventory_path"),
                adapter=_tts_adapter(request),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        workspace = _workspace(request, project_id)
        assignment = next(
            item for item in result.assignments if item.speaker == payload.speaker
        )
        return {
            "project_id": project_id,
            "speaker": payload.speaker,
            "assignment": assignment.model_dump(),
            "sample_url": _audio_url(workspace, assignment.sample_take_path),
        }

    @app.post("/api/projects/{project_id}/voice-assignments")
    def save_voice_assignment_endpoint(
        request: Request,
        project_id: str,
        payload: VoiceAssignmentRequest,
    ) -> dict[str, Any]:
        try:
            result = save_voice_assignments(
                project_id,
                payload.assignments,
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"project_id": project_id, "assignments": _assignment_payloads(result)}

    @app.post("/api/projects/{project_id}/audio/jobs")
    def start_audio_generation_job(
        request: Request,
        project_id: str,
        payload: AudioGenerationJobRequest,
    ) -> dict[str, Any]:
        _ensure_tts_generation_enabled(request)
        try:
            save_voice_assignments(
                project_id,
                payload.assignments,
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        registry: JobRegistry = request.app.state.jobs
        job = registry.create(project_id, "tts", "all")
        thread = threading.Thread(
            target=_run_audio_generation_job,
            args=(request.app, job.job_id, project_id, payload.only_missing),
            daemon=True,
        )
        thread.start()
        return job.to_dict()

    @app.post("/api/projects/{project_id}/audio/segment-jobs")
    def start_segment_audio_take_job(
        request: Request,
        project_id: str,
        payload: SegmentAudioTakeJobRequest,
    ) -> dict[str, Any]:
        _ensure_tts_generation_enabled(request)
        registry: JobRegistry = request.app.state.jobs
        job = registry.create(project_id, "tts", payload.segment_id)
        thread = threading.Thread(
            target=_run_segment_audio_take_job,
            args=(request.app, job.job_id, project_id, payload.segment_id),
            daemon=True,
        )
        thread.start()
        return job.to_dict()

    @app.post("/api/projects/{project_id}/audio-takes/{segment_id}/select")
    def select_audio_take_endpoint(
        request: Request,
        project_id: str,
        segment_id: str,
        payload: AudioTakeSelectionRequest,
    ) -> dict[str, Any]:
        try:
            selections = select_audio_take(
                project_id,
                segment_id,
                payload.take_number,
                workspace_root=_state_path(request, "workspace_root"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "project_id": project_id,
            "segment_id": segment_id,
            "selected_take_number": selections.selected_take_by_segment[segment_id],
        }

    @app.get("/api/projects/{project_id}/audio-file/{audio_path:path}")
    def get_audio_file(
        request: Request,
        project_id: str,
        audio_path: str,
    ) -> FileResponse:
        workspace = _workspace(request, project_id)
        audio_root = workspace.audio_dir.resolve()
        target = (audio_root / audio_path).resolve()
        if audio_root not in target.parents and target != audio_root:
            raise HTTPException(status_code=400, detail="audio path escapes project")
        if not target.exists() or target.suffix.lower() != ".wav":
            raise HTTPException(status_code=404, detail="audio file not found")
        return FileResponse(target, media_type="audio/wav")

    @app.get("/api/voice-profiles/{profile_id}/sample")
    def get_voice_profile_sample(request: Request, profile_id: str) -> FileResponse:
        inventory_path = _state_path(request, "voice_inventory_path")
        inventory = load_voice_inventory(inventory_path)
        profile = next(
            (item for item in inventory.profiles if item.profile_id == profile_id),
            None,
        )
        if profile is None or not profile.sample_path:
            raise HTTPException(status_code=404, detail="voice sample not found")
        target = _resolve_voice_inventory_asset(inventory_path, profile.sample_path)
        if target.suffix.lower() not in {".wav", ".m4a"}:
            raise HTTPException(status_code=404, detail="voice sample not found")
        media_type = "audio/wav" if target.suffix.lower() == ".wav" else "audio/mp4"
        return FileResponse(target, media_type=media_type)

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
                        assembly.workspace.script_artifact_path(
                            COMPLETE_SCRIPT_CHUNK_ID
                        )
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


def _run_stage3_job(
    app: FastAPI,
    job_id: str,
    payload: Stage3JobRequest,
    response_dir: Path | None,
) -> None:
    registry: JobRegistry = app.state.jobs
    workspace_root: Path = app.state.workspace_root

    def on_progress(progress: SpeakerKeyReviewProgress) -> None:
        def update(job: PipelineJob) -> None:
            job.status = progress.status
            job.total_chunks = progress.total_candidates
            job.completed_chunks = progress.processed_candidates
            job.current_chunk_id = progress.segment_id
            job.current_speaker = progress.current_key
            job.errors = progress.errors

        registry.update(job_id, update)

    try:
        result = run_speaker_key_review_workflow(
            payload.project_id,
            response_dir=response_dir,
            workspace_root=workspace_root,
            confidence_threshold=payload.confidence_threshold,
            batch_size=payload.batch_size,
            max_output_tokens=payload.max_output_tokens,
            progress_callback=on_progress,
        )

        def complete(job: PipelineJob) -> None:
            job.status = "complete"
            job.total_chunks = result.reviewed_count + result.deterministic_renamed_count
            job.completed_chunks = result.reviewed_count + result.deterministic_renamed_count
            job.current_chunk_id = None
            job.current_speaker = None
            job.errors = result.errors
            job.artifact_paths = {
                "script": str(
                    result.workspace.key_reviewed_script_artifact_path(
                        COMPLETE_SCRIPT_CHUNK_ID
                    )
                ),
                "review_report": str(result.report_path),
                "stage3_metrics": (
                    f"deterministic={result.deterministic_renamed_count}; "
                    f"unresolved={result.unresolved_count}; batches={result.llm_batch_count}; "
                    f"prompt_tokens={result.prompt_tokens}; output_tokens={result.output_tokens}"
                ),
            }

        registry.update(job_id, complete)
    except Exception as exc:
        error_message = str(exc)

        def fail(job: PipelineJob) -> None:
            job.status = "failed"
            if not job.errors:
                job.errors = [error_message]
            job.current_chunk_id = None
            job.current_speaker = None

        registry.update(job_id, fail)


def _run_audio_generation_job(
    app: FastAPI,
    job_id: str,
    project_id: str,
    only_missing: bool,
) -> None:
    registry: JobRegistry = app.state.jobs
    workspace_root: Path = app.state.workspace_root

    def on_progress(progress: AudioGenerationProgress) -> None:
        def update(job: PipelineJob) -> None:
            job.status = progress.status
            job.total_chunks = progress.total_segments
            job.completed_chunks = progress.completed_segments
            job.current_chunk_id = progress.current_segment_id
            job.current_speaker = progress.current_speaker
            job.errors = progress.errors

        registry.update(job_id, update)

    try:
        result = run_audio_generation_workflow(
            project_id,
            workspace_root=workspace_root,
            voice_inventory_path=app.state.voice_inventory_path,
            only_missing=only_missing,
            adapter=_app_tts_adapter(app),
            progress_callback=on_progress,
        )

        def complete(job: PipelineJob) -> None:
            job.status = "complete"
            job.current_chunk_id = None
            job.current_speaker = None
            job.errors = list(result.get("errors", []))
            job.artifact_paths = {"audio_takes_dir": str(result["audio_takes_dir"])}

        registry.update(job_id, complete)
    except Exception as exc:
        error_message = str(exc)

        def fail(job: PipelineJob) -> None:
            job.status = "failed"
            if not job.errors:
                job.errors = [error_message]
            job.current_chunk_id = None
            job.current_speaker = None

        registry.update(job_id, fail)


def _run_segment_audio_take_job(
    app: FastAPI,
    job_id: str,
    project_id: str,
    segment_id: str,
) -> None:
    registry: JobRegistry = app.state.jobs
    workspace_root: Path = app.state.workspace_root

    def running(job: PipelineJob) -> None:
        job.status = "running"
        job.total_chunks = 1
        job.completed_chunks = 0
        job.current_chunk_id = segment_id

    registry.update(job_id, running)
    try:
        manifest = generate_audio_take(
            project_id,
            segment_id,
            workspace_root=workspace_root,
            voice_inventory_path=app.state.voice_inventory_path,
            adapter=_app_tts_adapter(app),
        )

        def complete(job: PipelineJob) -> None:
            job.status = "complete"
            job.total_chunks = 1
            job.completed_chunks = 1
            job.current_chunk_id = None
            job.artifact_paths = {
                "audio_take": manifest.output_path,
                "take_number": str(manifest.take_number),
            }

        registry.update(job_id, complete)
    except Exception as exc:
        error_message = str(exc)

        def fail(job: PipelineJob) -> None:
            job.status = "failed"
            if not job.errors:
                job.errors = [error_message]
            job.current_chunk_id = None

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
        text = (
            text_path.read_text(encoding="utf-8")
            if text_path.exists()
            else chunk["text"]
        )
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


def _characters_view(request: Request, workspace: Workspace) -> dict[str, Any]:
    if not workspace.character_registry_path.exists():
        registry = {"project_id": workspace.project_id, "characters": []}
    else:
        registry = _read_json(workspace.character_registry_path)
    characters = [
        {key: value for key, value in character.items() if key != "confirmed"}
        for character in registry.get("characters", [])
    ]
    registry = {**registry, "characters": characters}
    assignments = []
    missing_voice_profile_ids: list[str] = []
    script_artifact_path: str | None = None
    try:
        voice_view = build_voice_assignment_view(
            workspace.project_id,
            workspace_root=_state_path(request, "workspace_root"),
            voice_inventory_path=_state_path(request, "voice_inventory_path"),
        )
        assignments = _assignment_payloads(voice_view.assignments, workspace=workspace)
        missing_voice_profile_ids = voice_view.missing_voice_profile_ids
        script_artifact_path = str(voice_view.script_artifact_path)
    except RuntimeError:
        voice_view = None

    voice_profiles = []
    try:
        inventory = load_voice_inventory(_state_path(request, "voice_inventory_path"))
        voice_profiles = _voice_profile_payloads(
            request,
            [
                {
                    **profile.model_dump(),
                    "available": Path(profile.prompt_path).exists(),
                }
                for profile in inventory.profiles
            ],
        )
    except RuntimeError:
        pass
    return {
        "project_id": workspace.project_id,
        "view_type": "characters",
        "available": True,
        "registry": registry,
        "characters": characters,
        "script_speaker_keys": _script_speaker_keys(workspace),
        "assignments": assignments,
        "voice_profiles": voice_profiles,
        "missing_voice_profile_ids": missing_voice_profile_ids,
        "script_artifact_path": script_artifact_path,
        "tts_generation_enabled": _tts_generation_enabled(request),
        "tts_generation_status": _tts_generation_status(request),
        "review": _character_review_payload(workspace, registry),
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
    stage3_missing_inputs = _stage3_missing_inputs(workspace)
    continuous_payload = {
        **continuous_payload,
        "segments": _segments_with_audio_takes(
            workspace,
            continuous_payload.get("segments", []),
        ),
    }

    return {
        "project_id": workspace.project_id,
        "view_type": "scripts",
        "available": True,
        "requested_chunk_id": chunk_id,
        "script_options": _script_options(workspace),
        "speaker_options": _speaker_options_payload(workspace),
        "speaker_filter_options": _script_speaker_keys(workspace),
        "stage3_enabled": not stage3_missing_inputs,
        "stage3_missing_inputs": stage3_missing_inputs,
        **continuous_payload,
    }


def _stage3_missing_inputs(workspace: Workspace) -> list[str]:
    missing = []
    if not workspace.script_artifact_path(COMPLETE_SCRIPT_CHUNK_ID).exists():
        missing.append("an assembled complete script")
    if not workspace.character_registry_path.exists():
        missing.append("the Stage 1 character registry")
    if not workspace.chunks_path.exists():
        missing.append("the chunks manifest")
    elif not all(
        workspace.context_artifact_path(str(chunk["chunk_id"])).exists()
        for chunk in _read_json(workspace.chunks_path).get("chunks", [])
    ):
        missing.append("Stage 1 context for every chunk")
    return missing


def _script_speaker_keys(workspace: Workspace) -> list[str]:
    continuous = _continuous_script_payload(workspace)
    if continuous is None:
        return []
    seen: set[str] = set()
    keys = []
    for segment in continuous.get("segments", []):
        speaker = segment.get("speaker")
        if isinstance(speaker, str) and speaker and speaker not in seen:
            seen.add(speaker)
            keys.append(speaker)
    return keys


def _character_review_payload(
    workspace: Workspace,
    registry: dict[str, Any],
) -> dict[str, Any]:
    characters = registry.get("characters", [])
    alias_owners: dict[str, list[str]] = {}
    for character in characters:
        character_id = str(character.get("character_id", ""))
        references = [
            *character.get("stable_aliases", []),
            *character.get("aliases", []),
        ]
        for alias in [character.get("canonical_name"), *references]:
            if isinstance(alias, str) and alias.strip():
                alias_owners.setdefault(alias.strip(), []).append(character_id)

    conflicts = [
        {
            "type": "shared_alias",
            "alias": alias,
            "character_ids": character_ids,
        }
        for alias, character_ids in alias_owners.items()
        if len(set(character_ids)) > 1
    ]
    continuous = _continuous_script_payload(workspace)
    unknown_segment_ids = []
    if continuous is not None:
        unknown_segment_ids = [
            segment["segment_id"]
            for segment in continuous.get("segments", [])
            if segment.get("speaker") == "unknown_speaker"
        ]
    return {
        "conflicts": conflicts,
        "unknown_script_segment_ids": unknown_segment_ids,
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
    return preferred_script_artifact_paths(workspace, COMPLETE_SCRIPT_CHUNK_ID)


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


def _speaker_options_payload(workspace: Workspace) -> list[str]:
    try:
        return speaker_options(workspace.project_id, workspace_root=workspace.root)
    except RuntimeError:
        return ["narrator", "unknown_speaker"]


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


def _segments_with_audio_takes(
    workspace: Workspace,
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    takes_by_segment = list_audio_takes(
        workspace.project_id,
        workspace_root=workspace.root,
    )
    selected_take_numbers = selected_audio_take_numbers(
        workspace.project_id,
        workspace_root=workspace.root,
    )
    assigned_profiles: dict[str, str | None] | None = None
    if workspace.voice_assignments_path.exists():
        try:
            assignment_payload = _read_json(workspace.voice_assignments_path)
            assigned_profiles = {
                str(assignment.get("speaker", "")): assignment.get("voice_profile_id")
                for assignment in assignment_payload.get("assignments", [])
            }
        except (OSError, ValueError):
            assigned_profiles = None
    output = []
    for segment in segments:
        segment_id = str(segment.get("segment_id", ""))
        speaker = str(segment.get("speaker", ""))
        text = str(segment.get("text", ""))
        historical_takes = takes_by_segment.get(segment_id, [])
        assigned_profile = (
            assigned_profiles.get(speaker) if assigned_profiles is not None else None
        )
        takes = [
            take
            for take in historical_takes
            if take.speaker == speaker
            and take.text == text
            and (
                assigned_profiles is None
                or (
                    bool(assigned_profile)
                    and take.voice_profile_id == assigned_profile
                )
            )
        ]
        selected_take_number = selected_take_numbers.get(segment_id)
        if selected_take_number is not None and not any(
            take.take_number == selected_take_number for take in takes
        ):
            selected_take_number = None
        if selected_take_number is None and any(
            take.take_number == 1 for take in takes
        ):
            selected_take_number = 1
        output.append(
            {
                **segment,
                "audio_takes": [
                    {
                        "take_number": take.take_number,
                        "audio_url": _audio_url(workspace, take.output_path),
                        "selected": take.take_number == selected_take_number,
                        "voice_profile_id": take.voice_profile_id,
                    }
                    for take in takes
                ],
                "stale_audio_take_count": len(historical_takes) - len(takes),
            }
        )
    return output


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_view_type(view_type: str) -> str:
    normalized = VIEW_ALIASES.get(view_type, view_type)
    if normalized not in {option["id"] for option in VIEW_OPTIONS} | {
        "chunks",
        "scene_summary",
    }:
        raise HTTPException(status_code=404, detail="unknown view type")
    return normalized


def _assignment_payloads(
    artifact,
    *,
    workspace: Workspace | None = None,
) -> list[dict[str, Any]]:
    output = []
    for assignment in artifact.assignments:
        payload = assignment.model_dump()
        payload["sample_url"] = (
            _audio_url(workspace, assignment.sample_take_path)
            if workspace is not None
            else None
        )
        output.append(payload)
    return output


def _audio_url(workspace: Workspace, path: str | None) -> str | None:
    if not path:
        return None
    target = Path(path)
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    audio_root = workspace.audio_dir.resolve()
    try:
        relative = target.resolve().relative_to(audio_root)
    except ValueError:
        return None
    return f"/api/projects/{workspace.project_id}/audio-file/{relative.as_posix()}"


def _voice_profile_payloads(
    request: Request,
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads = []
    inventory_path = _state_path(request, "voice_inventory_path")
    for profile in profiles:
        payload = dict(profile)
        if _voice_profile_sample_exists(inventory_path, profile.get("sample_path")):
            payload["sample_url"] = (
                f"/api/voice-profiles/{profile['profile_id']}/sample"
            )
        else:
            payload["sample_url"] = None
        payloads.append(payload)
    return payloads


def _voice_profile_sample_exists(
    inventory_path: Path,
    sample_path: object,
) -> bool:
    if not isinstance(sample_path, str) or not sample_path:
        return False
    try:
        target = _resolve_voice_inventory_asset(inventory_path, sample_path)
    except HTTPException:
        return False
    return target.exists() and target.suffix.lower() in {".wav", ".m4a"}


def _resolve_voice_inventory_asset(inventory_path: Path, asset_path: str) -> Path:
    inventory_root = inventory_path.parent.resolve()
    target = Path(asset_path)
    if not target.is_absolute():
        cwd_target = (Path.cwd() / target).resolve()
        inventory_target = (inventory_root / target).resolve()
        target = cwd_target if cwd_target.exists() else inventory_target
    else:
        target = target.resolve()
    if inventory_root not in target.parents and target != inventory_root:
        raise HTTPException(status_code=400, detail="voice asset escapes inventory")
    if not target.exists():
        raise HTTPException(status_code=404, detail="voice sample not found")
    return target


def _ensure_tts_generation_enabled(request: Request) -> None:
    if _tts_generation_enabled(request):
        return
    raise HTTPException(
        status_code=409,
        detail="Qwen web generation is disabled until CLI smoke tests pass.",
    )


def _tts_generation_enabled(request: Request) -> bool:
    if request.app.state.tts_adapter_name == "dummy":
        return True
    report = qwen_delete_readiness_report(
        voice_inventory_path=_state_path(request, "voice_inventory_path")
    )
    return bool(report["safe_to_delete_qwen_folders"])


def _tts_generation_status(request: Request) -> str:
    if request.app.state.tts_adapter_name == "dummy":
        return "ready"
    report = qwen_delete_readiness_report(
        voice_inventory_path=_state_path(request, "voice_inventory_path")
    )
    if report["safe_to_delete_qwen_folders"]:
        return "ready"
    return "; ".join(report["notes"]) or "CLI smoke pending"


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
    if view_type == "characters":
        return (
            workspace.character_registry_path.exists()
            or _continuous_script_payload(workspace) is not None
        )
    if view_type == "scripts":
        return _continuous_script_payload(workspace) is not None
    return False


def _tts_adapter(request: Request):
    return _app_tts_adapter(request.app)


def _app_tts_adapter(app: FastAPI):
    adapter_name = getattr(app.state, "tts_adapter_name", None)
    if adapter_name == "dummy":
        return DummyTTSAdapter()
    return QwenTTSAdapter()


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
        raise HTTPException(
            status_code=400, detail="source path must be under data/raw"
        )
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
