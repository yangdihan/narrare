from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable

from config.loader import load_config
from config.models import AppConfig
from core.ir.script_builder import derive_script_segments, merge_consecutive_same_speaker
from core.ir.script_repair import (
    RepairDiagnosis,
    RepairSpan,
    diagnose_repair_span,
    renumber_segments,
)
from core.models.ir import (
    RawScriptSegment,
    ScriptArtifact,
    ScriptConverterResponse,
    ScriptSegment,
)
from core.validation.script_integrity import (
    find_content_mismatch,
    sha256_text,
    validate_script_segments,
)
from llm.json_utils import parse_json_object_response
from llm.prompts.script_converter import (
    SYSTEM_PROMPT,
    build_script_repair_user_prompt,
    build_script_converter_user_prompt,
)
from llm.schemas import LlmCompletion
from llm.service import LlmService
from storage.json_store import write_json
from storage.workspace import Workspace


@dataclass(frozen=True)
class ScriptProgress:
    chunk_id: str
    attempt: int
    max_attempts: int
    attempt_elapsed_seconds: float | None
    chunk_elapsed_seconds: float
    status: str
    errors: list[str]
    repair_start: int | None = None
    repair_end: int | None = None


ProgressCallback = Callable[[ScriptProgress], None]


@dataclass(frozen=True)
class ScriptConversionResult:
    workspace: Workspace
    artifact: ScriptArtifact
    validation_report_path: Path
    exact_reconstruction_success: bool
    errors: list[str]


def run_script_conversion_workflow(
    chunk_path: str | Path,
    project_id: str,
    chunk_id: str,
    *,
    response_path: str | Path | None = None,
    max_retries: int = 5,
    config: AppConfig | None = None,
    workspace_root: str | Path = "data/interim",
    progress_callback: ProgressCallback | None = None,
    llm_service: LlmService | None = None,
    enable_shrinking_retry: bool = True,
) -> ScriptConversionResult:
    app_config = config or load_config()
    chunk_text_value = Path(chunk_path).read_text(encoding="utf-8")
    workspace = Workspace(project_id, root=workspace_root)
    workspace.ensure()
    workspace.script_chunk_dir(chunk_id).mkdir(parents=True, exist_ok=True)

    service = llm_service
    if service is None and response_path is None:
        service = LlmService(app_config.llm)

    chunk_started_at = time.monotonic()
    response_source = "response_path" if response_path else "llm"
    _emit_progress(
        progress_callback,
        ScriptProgress(
            chunk_id=chunk_id,
            attempt=0,
            max_attempts=1 if response_path else max_retries,
            attempt_elapsed_seconds=None,
            chunk_elapsed_seconds=0.0,
            status="running",
            errors=[],
        ),
    )

    prompt = build_script_converter_user_prompt(
        chunk_id=chunk_id,
        chunk_text=chunk_text_value,
        previous_segments=[],
    )
    segments = _convert_script_chunk(
        project_id=project_id,
        chunk_id=chunk_id,
        chunk_text=chunk_text_value,
        chunk_path=str(chunk_path),
        response_path=response_path,
        llm_service=service,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=prompt,
        workspace=workspace,
        max_retries=max_retries,
        progress_callback=progress_callback,
        chunk_started_at=chunk_started_at,
        enable_shrinking_retry=enable_shrinking_retry and response_path is None,
    )

    artifact = ScriptArtifact(
        project_id=project_id,
        chunk_id=chunk_id,
        chunk_source_path=str(chunk_path),
        chunk_sha256=sha256_text(chunk_text_value),
        llm_provider=app_config.llm.provider,
        llm_model=app_config.llm.model,
        response_source=response_source,
        processed_chunk_count=1,
        segments=segments,
    )
    validation_report = validate_script_segments(
        project_id=project_id,
        chunk_id=chunk_id,
        source_text=chunk_text_value,
        segments=segments,
    )
    if not validation_report.exact_reconstruction_success:
        validation_report.errors.extend(
            _locate_merged_content_mismatch(
                chunk_id,
                chunk_text_value,
                segments,
            )
        )
    write_json(workspace.script_artifact_path(chunk_id), artifact)
    validation_report_path = workspace.script_validation_report_path(chunk_id)
    write_json(validation_report_path, validation_report)

    if not validation_report.exact_reconstruction_success:
        _emit_progress(
            progress_callback,
            ScriptProgress(
                chunk_id=chunk_id,
                attempt=0,
                max_attempts=1 if response_path else max_retries,
                attempt_elapsed_seconds=None,
                chunk_elapsed_seconds=time.monotonic() - chunk_started_at,
                status="failed",
                errors=validation_report.errors,
            ),
        )
        raise RuntimeError(
            "Merged script validation failed: " + "; ".join(validation_report.errors)
        )

    _emit_progress(
        progress_callback,
        ScriptProgress(
            chunk_id=chunk_id,
            attempt=0,
            max_attempts=1 if response_path else max_retries,
            attempt_elapsed_seconds=None,
            chunk_elapsed_seconds=time.monotonic() - chunk_started_at,
            status="complete",
            errors=[],
        ),
    )
    return ScriptConversionResult(
        workspace=workspace,
        artifact=artifact,
        validation_report_path=validation_report_path,
        exact_reconstruction_success=validation_report.exact_reconstruction_success,
        errors=validation_report.errors,
    )


def _convert_script_chunk(
    *,
    project_id: str,
    chunk_id: str,
    chunk_text: str,
    chunk_path: str,
    response_path: str | Path | None,
    llm_service: LlmService | None,
    system_prompt: str,
    user_prompt: str,
    workspace: Workspace,
    max_retries: int,
    progress_callback: ProgressCallback | None,
    chunk_started_at: float,
    enable_shrinking_retry: bool,
) -> list[ScriptSegment]:
    last_errors: list[str] = []
    attempts = 1 if response_path else max_retries
    max_repair_attempts = 2

    for attempt in range(1, attempts + 1):
        attempt_started_at = time.monotonic()
        _emit_progress(
            progress_callback,
            ScriptProgress(
                chunk_id=chunk_id,
                attempt=attempt,
                max_attempts=attempts,
                attempt_elapsed_seconds=0.0,
                chunk_elapsed_seconds=attempt_started_at - chunk_started_at,
                status="attempt_started",
                errors=[],
            ),
        )
        try:
            if response_path:
                completion = LlmCompletion(
                    content=Path(response_path).read_text(encoding="utf-8")
                )
            else:
                if llm_service is None:
                    raise RuntimeError("llm_service is required for live conversion")
                completion = llm_service.complete_json(system_prompt, user_prompt)
        except Exception as exc:
            last_errors = [f"LLM request failed: {exc}"]
            attempt_elapsed = time.monotonic() - attempt_started_at
            _write_failed_chunk_report(
                project_id,
                chunk_id,
                chunk_text,
                last_errors,
                workspace,
                attempt,
            )
            _emit_attempt_progress(
                progress_callback,
                chunk_id,
                attempt,
                attempts,
                attempt_elapsed,
                chunk_started_at,
                "attempt_failed",
                last_errors,
            )
            continue

        raw_path = workspace.script_attempt_raw_response_path(chunk_id, attempt)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(completion.content.strip() + "\n", encoding="utf-8")

        try:
            response_data = parse_json_object_response(completion.content)
            converter_response = ScriptConverterResponse.model_validate(response_data)
        except Exception as exc:
            last_errors = [f"invalid script converter JSON: {exc}"]
            attempt_elapsed = time.monotonic() - attempt_started_at
            _write_failed_chunk_report(
                project_id,
                chunk_id,
                chunk_text,
                last_errors,
                workspace,
                attempt,
            )
            _emit_attempt_progress(
                progress_callback,
                chunk_id,
                attempt,
                attempts,
                attempt_elapsed,
                chunk_started_at,
                "attempt_failed",
                last_errors,
            )
            continue

        alignment_errors: list[str] = []
        try:
            segments = derive_script_segments(
                converter_response.segments,
                source_start=0,
                starting_index=0,
                source_text=chunk_text,
                source_end=len(chunk_text),
                chunk_id=chunk_id,
            )
        except Exception as exc:
            alignment_errors = [f"script text alignment failed: {exc}"]
            segments = []

        repair_diagnosis: RepairDiagnosis | None = None
        validation_report = validate_script_segments(
            project_id=project_id,
            chunk_id=chunk_id,
            source_text=chunk_text,
            segments=segments,
        )
        validation_report.errors.extend(alignment_errors)

        if validation_report.exact_reconstruction_success and not alignment_errors:
            segments = merge_consecutive_same_speaker(segments, starting_index=0)
            validation_report = validate_script_segments(
                project_id=project_id,
                chunk_id=chunk_id,
                source_text=chunk_text,
                segments=segments,
            )
        elif enable_shrinking_retry and llm_service is not None:
            repair_diagnosis = diagnose_repair_span(
                chunk_text,
                converter_response.segments,
            )
            if repair_diagnosis is not None:
                repaired_segments = _run_shrinking_repair(
                    project_id=project_id,
                    chunk_id=chunk_id,
                    chunk_text=chunk_text,
                    chunk_path=chunk_path,
                    llm_service=llm_service,
                    system_prompt=system_prompt,
                    workspace=workspace,
                    attempt=attempt,
                    parent_max_attempts=attempts,
                    max_repair_attempts=max_repair_attempts,
                    progress_callback=progress_callback,
                    chunk_started_at=chunk_started_at,
                    repair_diagnosis=repair_diagnosis,
                )
                if repaired_segments is not None:
                    segments = repaired_segments
                    validation_report = validate_script_segments(
                        project_id=project_id,
                        chunk_id=chunk_id,
                        source_text=chunk_text,
                        segments=segments,
                    )

        write_json(
            workspace.script_attempt_artifact_path(chunk_id, attempt),
            {
                "project_id": project_id,
                "chunk_id": chunk_id,
                "chunk_source_path": chunk_path,
                "segments": [segment.model_dump() for segment in segments],
            },
        )
        write_json(
            workspace.script_attempt_validation_report_path(chunk_id, attempt),
            validation_report,
        )

        attempt_elapsed = time.monotonic() - attempt_started_at
        if validation_report.exact_reconstruction_success:
            _emit_attempt_progress(
                progress_callback,
                chunk_id,
                attempt,
                attempts,
                attempt_elapsed,
                chunk_started_at,
                "attempt_complete",
                [],
            )
            return segments

        last_errors = validation_report.errors
        if repair_diagnosis is not None:
            last_errors = [
                *last_errors,
                (
                    "shrinking repair failed for "
                    f"source_span={repair_diagnosis.span.start}:{repair_diagnosis.span.end}"
                ),
            ]
        _emit_attempt_progress(
            progress_callback,
            chunk_id,
            attempt,
            attempts,
            attempt_elapsed,
            chunk_started_at,
            "attempt_failed",
            last_errors,
        )

    raise RuntimeError(
        f"Script chunk {chunk_id} failed validation: " + "; ".join(last_errors)
    )


def _run_shrinking_repair(
    *,
    project_id: str,
    chunk_id: str,
    chunk_text: str,
    chunk_path: str,
    llm_service: LlmService,
    system_prompt: str,
    workspace: Workspace,
    attempt: int,
    parent_max_attempts: int,
    max_repair_attempts: int,
    progress_callback: ProgressCallback | None,
    chunk_started_at: float,
    repair_diagnosis: RepairDiagnosis,
) -> list[ScriptSegment] | None:
    diagnosis = repair_diagnosis

    for repair_attempt in range(1, max_repair_attempts + 1):
        repair_started_at = time.monotonic()
        _emit_progress(
            progress_callback,
            ScriptProgress(
                chunk_id=chunk_id,
                attempt=attempt,
                max_attempts=parent_max_attempts,
                attempt_elapsed_seconds=0.0,
                chunk_elapsed_seconds=repair_started_at - chunk_started_at,
                status="repair_started",
                errors=[],
                repair_start=diagnosis.span.start,
                repair_end=diagnosis.span.end,
            ),
        )
        repair_prompt = build_script_repair_user_prompt(
            chunk_id=chunk_id,
            repair_start=diagnosis.span.start,
            repair_end=diagnosis.span.end,
            repair_text=chunk_text[diagnosis.span.start : diagnosis.span.end],
            prefix_segments=_segment_context(diagnosis.prefix_segments[-3:]),
            suffix_segments=_segment_context(diagnosis.suffix_segments[:3]),
            reason=diagnosis.span.reason,
        )
        try:
            completion = llm_service.complete_json(system_prompt, repair_prompt)
        except Exception as exc:
            errors = [f"LLM repair request failed: {exc}"]
            _write_repair_validation_report(
                project_id,
                chunk_id,
                chunk_text,
                errors,
                workspace,
                attempt,
                repair_attempt,
            )
            _emit_attempt_progress(
                progress_callback,
                chunk_id,
                attempt,
                parent_max_attempts,
                time.monotonic() - repair_started_at,
                chunk_started_at,
                "repair_failed",
                errors,
                repair_span=diagnosis.span,
            )
            continue

        raw_path = workspace.script_repair_raw_response_path(
            chunk_id, attempt, repair_attempt
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(completion.content.strip() + "\n", encoding="utf-8")

        try:
            response_data = parse_json_object_response(completion.content)
            repair_response = ScriptConverterResponse.model_validate(response_data)
            repair_segments = derive_script_segments(
                repair_response.segments,
                source_start=diagnosis.span.start,
                starting_index=len(diagnosis.prefix_segments),
                source_text=chunk_text,
                source_end=diagnosis.span.end,
                chunk_id=chunk_id,
            )
        except Exception as exc:
            errors = [f"invalid repair JSON: {exc}"]
            _write_repair_validation_report(
                project_id,
                chunk_id,
                chunk_text,
                errors,
                workspace,
                attempt,
                repair_attempt,
            )
            _emit_attempt_progress(
                progress_callback,
                chunk_id,
                attempt,
                parent_max_attempts,
                time.monotonic() - repair_started_at,
                chunk_started_at,
                "repair_failed",
                errors,
                repair_span=diagnosis.span,
            )
            continue

        repair_validation_report = validate_script_segments(
            project_id=project_id,
            chunk_id=chunk_id,
            source_text=chunk_text,
            segments=repair_segments,
            source_start=diagnosis.span.start,
            source_end=diagnosis.span.end,
        )
        assembled_segments = renumber_segments(
            [
                *diagnosis.prefix_segments,
                *repair_segments,
                *diagnosis.suffix_segments,
            ]
        )
        assembled_segments = merge_consecutive_same_speaker(
            assembled_segments, starting_index=0
        )
        full_validation_report = validate_script_segments(
            project_id=project_id,
            chunk_id=chunk_id,
            source_text=chunk_text,
            segments=assembled_segments,
        )
        if not repair_validation_report.exact_reconstruction_success:
            full_validation_report.errors.extend(
                [
                    "repair span validation failed: " + error
                    for error in repair_validation_report.errors
                ]
            )

        write_json(
            workspace.script_repair_artifact_path(chunk_id, attempt, repair_attempt),
            {
                "project_id": project_id,
                "chunk_id": chunk_id,
                "chunk_source_path": chunk_path,
                "repair_source_span": {
                    "start": diagnosis.span.start,
                    "end": diagnosis.span.end,
                },
                "repair_segments": [segment.model_dump() for segment in repair_segments],
                "assembled_segments": [
                    segment.model_dump() for segment in assembled_segments
                ],
            },
        )
        write_json(
            workspace.script_repair_validation_report_path(
                chunk_id, attempt, repair_attempt
            ),
            full_validation_report,
        )

        if full_validation_report.exact_reconstruction_success:
            _emit_attempt_progress(
                progress_callback,
                chunk_id,
                attempt,
                parent_max_attempts,
                time.monotonic() - repair_started_at,
                chunk_started_at,
                "repair_complete",
                [],
                repair_span=diagnosis.span,
            )
            return assembled_segments

        next_diagnosis = diagnose_repair_span(
            chunk_text,
            _segments_to_raw(assembled_segments),
        )
        if next_diagnosis is not None:
            diagnosis = next_diagnosis

        _emit_attempt_progress(
            progress_callback,
            chunk_id,
            attempt,
            parent_max_attempts,
            time.monotonic() - repair_started_at,
            chunk_started_at,
            "repair_failed",
            full_validation_report.errors,
            repair_span=diagnosis.span,
        )

    return None


def _write_repair_validation_report(
    project_id: str,
    chunk_id: str,
    chunk_text: str,
    errors: list[str],
    workspace: Workspace,
    attempt: int,
    repair_attempt: int,
) -> None:
    report = validate_script_segments(
        project_id=project_id,
        chunk_id=chunk_id,
        source_text=chunk_text,
        segments=[],
    )
    report.errors.extend(errors)
    report.exact_reconstruction_success = False
    write_json(
        workspace.script_repair_validation_report_path(
            chunk_id, attempt, repair_attempt
        ),
        report,
    )


def _write_failed_chunk_report(
    project_id: str,
    chunk_id: str,
    chunk_text: str,
    errors: list[str],
    workspace: Workspace,
    attempt: int,
) -> None:
    report = validate_script_segments(
        project_id=project_id,
        chunk_id=chunk_id,
        source_text=chunk_text,
        segments=[],
    )
    report.errors.extend(errors)
    report.exact_reconstruction_success = False
    write_json(workspace.script_attempt_validation_report_path(chunk_id, attempt), report)


def _locate_merged_content_mismatch(
    chunk_id: str,
    source_text: str,
    segments: list[ScriptSegment],
) -> list[str]:
    reconstructed = "".join(segment.text for segment in segments)
    mismatch = find_content_mismatch(source_text, reconstructed)
    if mismatch is None:
        return []

    return [
        "normalized content mismatch is located in "
        f"{chunk_id} "
        f"(source_offset={mismatch.source_offset}, "
        f"normalized_index={mismatch.normalized_index}); "
        f"source_excerpt={mismatch.source_excerpt!r}; "
        f"reconstructed_excerpt={mismatch.reconstructed_excerpt!r}"
    ]


def _emit_attempt_progress(
    progress_callback: ProgressCallback | None,
    chunk_id: str,
    attempt: int,
    max_attempts: int,
    attempt_elapsed_seconds: float,
    chunk_started_at: float,
    status: str,
    errors: list[str],
    repair_span: RepairSpan | None = None,
) -> None:
    _emit_progress(
        progress_callback,
        ScriptProgress(
            chunk_id=chunk_id,
            attempt=attempt,
            max_attempts=max_attempts,
            attempt_elapsed_seconds=attempt_elapsed_seconds,
            chunk_elapsed_seconds=time.monotonic() - chunk_started_at,
            status=status,
            errors=errors,
            repair_start=repair_span.start if repair_span is not None else None,
            repair_end=repair_span.end if repair_span is not None else None,
        ),
    )


def _segment_context(segments: list[ScriptSegment]) -> list[dict[str, object]]:
    return [
        {
            "script": segment.script,
            "confidence": segment.confidence,
            "review_notes": segment.review_notes,
            "source_span": segment.source_span.model_dump(),
        }
        for segment in segments
    ]


def _segments_to_raw(segments: list[ScriptSegment]) -> list[RawScriptSegment]:
    return [
        RawScriptSegment(
            script=segment.script,
            confidence=segment.confidence,
            review_notes=segment.review_notes,
        )
        for segment in segments
    ]


def _emit_progress(
    progress_callback: ProgressCallback | None,
    progress: ScriptProgress,
) -> None:
    if progress_callback is not None:
        progress_callback(progress)
