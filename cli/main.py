from __future__ import annotations

import argparse
import sys

from config.loader import load_config
from core.pipeline.chunking import run_chunking_workflow
from core.pipeline.chunk_context_profiler import (
    ContextProfileProgress,
    run_chunk_context_profiler_workflow,
)
from core.pipeline.script_assembly import run_script_assembly_workflow
from core.pipeline.script_conversion import (
    ScriptProgress,
    run_script_conversion_workflow,
)
from core.pipeline.speaker_key_normalization import (
    run_speaker_key_normalization_workflow,
)
from core.pipeline.speaker_key_review import (
    SpeakerKeyReviewProgress,
    run_speaker_key_review_workflow,
)


def run_chunk_command(source_path: str, project_id: str) -> None:
    try:
        result = run_chunking_workflow(source_path, project_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Wrote {len(result.chunks)} chunks to {result.workspace.project_root}")


def run_script_convert_command(
    chunk_path: str,
    project_id: str,
    chunk_id: str,
    response_path: str | None = None,
    max_retries: int = 5,
    llm_max_output_tokens: int | None = None,
    enable_shrinking_retry: bool = True,
) -> None:
    renderer = ScriptProgressRenderer(enabled=sys.stderr.isatty())
    config = load_config()
    if llm_max_output_tokens is not None:
        config = config.model_copy(
            update={
                "llm": config.llm.model_copy(
                    update={"max_output_tokens": llm_max_output_tokens}
                )
            }
        )
    try:
        result = run_script_conversion_workflow(
            chunk_path,
            project_id,
            chunk_id,
            response_path=response_path,
            max_retries=max_retries,
            config=config,
            progress_callback=renderer.update,
            enable_shrinking_retry=enable_shrinking_retry,
        )
    except (RuntimeError, ValueError) as exc:
        renderer.finish()
        raise SystemExit(str(exc)) from exc

    renderer.finish()
    print(
        f"Wrote {len(result.artifact.segments)} script segments from "
        f"{result.artifact.processed_chunk_count} chunk to "
        f"{result.workspace.script_artifact_path(chunk_id)}"
    )


def run_context_profile_command(
    project_id: str,
    response_dir: str | None = None,
) -> None:
    renderer = ContextProfileProgressRenderer(enabled=sys.stderr.isatty())
    try:
        result = run_chunk_context_profiler_workflow(
            project_id,
            response_dir=response_dir,
            progress_callback=renderer.update,
        )
    except (RuntimeError, ValueError) as exc:
        renderer.finish()
        raise SystemExit(str(exc)) from exc

    renderer.finish()
    print(
        f"Wrote {len(result.artifacts)} Stage 1 context artifacts and "
        f"{len(result.registry.characters)} character records to "
        f"{result.workspace.character_registry_path}"
    )


def run_script_assemble_command(project_id: str) -> None:
    try:
        result = run_script_assembly_workflow(project_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print(
        f"Wrote complete script with {len(result.artifact.segments)} segments "
        f"from {result.artifact.processed_chunk_count} chunks to "
        f"{result.workspace.script_artifact_path('complete')} "
        f"({result.boundary_merge_count} boundary merges)"
    )


def run_speaker_key_normalize_command(project_id: str) -> None:
    try:
        result = run_speaker_key_normalization_workflow(project_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print(
        f"Wrote normalized script with {result.renamed_count} renamed speaker keys "
        f"and {result.unresolved_count} unresolved keys to "
        f"{result.workspace.normalized_script_artifact_path('complete')}"
    )


def run_speaker_key_review_command(
    project_id: str,
    response_dir: str | None = None,
    confidence_threshold: float = 0.85,
) -> None:
    renderer = SpeakerKeyReviewProgressRenderer(enabled=sys.stderr.isatty())
    try:
        result = run_speaker_key_review_workflow(
            project_id,
            response_dir=response_dir,
            confidence_threshold=confidence_threshold,
            progress_callback=renderer.update,
        )
    except RuntimeError as exc:
        renderer.finish()
        raise SystemExit(str(exc)) from exc

    renderer.finish()
    print(
        f"Wrote key-reviewed script with {result.changed_count} changed speaker keys "
        f"from {result.reviewed_count} reviewed candidates to "
        f"{result.workspace.key_reviewed_script_artifact_path('complete')}"
    )


class ScriptProgressRenderer:
    def __init__(self, *, enabled: bool, width: int = 30) -> None:
        self.enabled = enabled
        self.width = width
        self._last_line_length = 0
        self._last_progress_key: tuple[str, int, int | None, int | None] | None = None

    def update(self, progress: ScriptProgress) -> None:
        line = self._format(progress)
        if self.enabled:
            padding = max(0, self._last_line_length - len(line))
            print("\r" + line + (" " * padding), end="", file=sys.stderr, flush=True)
            self._last_line_length = len(line)
            return

        progress_key = (
            progress.status,
            progress.attempt,
            progress.repair_start,
            progress.repair_end,
        )
        if progress_key != self._last_progress_key:
            print(line, file=sys.stderr)
            self._last_progress_key = progress_key

    def finish(self) -> None:
        if self.enabled and self._last_line_length:
            print(file=sys.stderr)
            self._last_line_length = 0

    def _format(self, progress: ScriptProgress) -> str:
        processed = min(progress.attempt, progress.max_attempts)
        filled = int(self.width * processed / progress.max_attempts)
        bar = "#" * filled + "-" * (self.width - filled)
        attempt_elapsed = (
            "n/a"
            if progress.attempt_elapsed_seconds is None
            else f"{progress.attempt_elapsed_seconds:.1f}s"
        )
        return (
            f"[{bar}] chunk={progress.chunk_id} "
            f"try={processed}/{progress.max_attempts} "
            f"try_elapsed={attempt_elapsed} "
            f"chunk_elapsed={progress.chunk_elapsed_seconds:.1f}s "
            f"status={progress.status}"
            f"{self._format_repair_span(progress)}"
        )

    def _format_repair_span(self, progress: ScriptProgress) -> str:
        if progress.repair_start is None or progress.repair_end is None:
            return ""
        return f" repair_span={progress.repair_start}:{progress.repair_end}"


class ContextProfileProgressRenderer:
    def __init__(self, *, enabled: bool, width: int = 30) -> None:
        self.enabled = enabled
        self.width = width
        self._last_line_length = 0
        self._last_progress_key: tuple[str, str | None, int] | None = None

    def update(self, progress: ContextProfileProgress) -> None:
        line = self._format(progress)
        if self.enabled:
            padding = max(0, self._last_line_length - len(line))
            print("\r" + line + (" " * padding), end="", file=sys.stderr, flush=True)
            self._last_line_length = len(line)
            return

        progress_key = (
            progress.status,
            progress.chunk_id,
            progress.processed_chunks,
        )
        if progress_key != self._last_progress_key:
            print(line, file=sys.stderr)
            self._last_progress_key = progress_key

    def finish(self) -> None:
        if self.enabled and self._last_line_length:
            print(file=sys.stderr)
            self._last_line_length = 0

    def _format(self, progress: ContextProfileProgress) -> str:
        total = max(progress.total_chunks, 1)
        processed = min(progress.processed_chunks, total)
        filled = int(self.width * processed / total)
        bar = "#" * filled + "-" * (self.width - filled)
        chunk_elapsed = (
            "n/a"
            if progress.chunk_elapsed_seconds is None
            else f"{progress.chunk_elapsed_seconds:.1f}s"
        )
        chunk_label = progress.chunk_id or "all"
        return (
            f"[{bar}] stage=1 chunk={chunk_label} "
            f"chunks={processed}/{progress.total_chunks} "
            f"chunk_elapsed={chunk_elapsed} "
            f"total_elapsed={progress.total_elapsed_seconds:.1f}s "
            f"status={progress.status}"
        )


class SpeakerKeyReviewProgressRenderer:
    def __init__(self, *, enabled: bool, width: int = 30) -> None:
        self.enabled = enabled
        self.width = width
        self._last_line_length = 0
        self._last_progress_key: tuple[str, str | None, int] | None = None

    def update(self, progress: SpeakerKeyReviewProgress) -> None:
        line = self._format(progress)
        if self.enabled:
            padding = max(0, self._last_line_length - len(line))
            print("\r" + line + (" " * padding), end="", file=sys.stderr, flush=True)
            self._last_line_length = len(line)
            return

        progress_key = (
            progress.status,
            progress.segment_id,
            progress.processed_candidates,
        )
        if progress_key != self._last_progress_key:
            print(line, file=sys.stderr)
            self._last_progress_key = progress_key

    def finish(self) -> None:
        if self.enabled and self._last_line_length:
            print(file=sys.stderr)
            self._last_line_length = 0

    def _format(self, progress: SpeakerKeyReviewProgress) -> str:
        total = max(progress.total_candidates, 1)
        processed = min(progress.processed_candidates, total)
        filled = int(self.width * processed / total)
        bar = "#" * filled + "-" * (self.width - filled)
        candidate_elapsed = (
            "n/a"
            if progress.candidate_elapsed_seconds is None
            else f"{progress.candidate_elapsed_seconds:.1f}s"
        )
        segment_label = progress.segment_id or "all"
        key_label = progress.current_key or "n/a"
        return (
            f"[{bar}] stage=3 segment={segment_label} "
            f"key={key_label} "
            f"candidates={processed}/{progress.total_candidates} "
            f"changed={progress.changed_count} "
            f"candidate_elapsed={candidate_elapsed} "
            f"total_elapsed={progress.total_elapsed_seconds:.1f}s "
            f"status={progress.status}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="narrare")
    subparsers = parser.add_subparsers(dest="command", required=True)

    chunk_parser = subparsers.add_parser("chunk", help="Chunk a TXT source file.")
    chunk_parser.add_argument("source_path")
    chunk_parser.add_argument("--project-id", required=True)

    context_parser = subparsers.add_parser(
        "context-profile",
        help="Run Stage 1 chunk context and character profiling.",
    )
    context_parser.add_argument("--project-id", required=True)
    context_parser.add_argument(
        "--response-dir",
        help=(
            "Read fixture responses named <chunk_id>_response.json instead of "
            "calling the LLM."
        ),
    )

    script_parser = subparsers.add_parser(
        "script-convert", help="Convert one chunk into Stage 2 script IR."
    )
    script_parser.add_argument("chunk_path")
    script_parser.add_argument("--project-id", required=True)
    script_parser.add_argument("--chunk-id", required=True)
    script_parser.add_argument(
        "--response-path",
        help="Validate and store one existing LLM JSON response instead of calling LLM.",
    )
    script_parser.add_argument("--max-retries", type=int, default=5)
    script_parser.add_argument(
        "--llm-max-output-tokens",
        type=int,
        help="Override config llm.max_output_tokens for this run.",
    )
    script_parser.add_argument(
        "--disable-shrinking-retry",
        action="store_true",
        help="Disable paragraph-bounded repair retries after Stage 2 misalignment.",
    )

    assemble_parser = subparsers.add_parser(
        "script-assemble",
        help="Deterministically assemble all Stage 2 chunk scripts.",
    )
    assemble_parser.add_argument("--project-id", required=True)

    normalize_parser = subparsers.add_parser(
        "speaker-key-normalize",
        help="Deterministically normalize complete-script speaker keys.",
    )
    normalize_parser.add_argument("--project-id", required=True)

    review_parser = subparsers.add_parser(
        "speaker-key-review",
        help="Run Stage 3 LLM speaker-key review on assembled script keys.",
    )
    review_parser.add_argument("--project-id", required=True)
    review_parser.add_argument(
        "--response-dir",
        help=(
            "Read fixture responses named <segment_id>_response.json instead of "
            "calling the LLM."
        ),
    )
    review_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.85,
        help="Minimum confidence required to apply a replacement key.",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "chunk":
        run_chunk_command(args.source_path, args.project_id)
        return

    if args.command == "context-profile":
        run_context_profile_command(args.project_id, args.response_dir)
        return

    if args.command == "script-convert":
        run_script_convert_command(
            args.chunk_path,
            args.project_id,
            args.chunk_id,
            args.response_path,
            args.max_retries,
            args.llm_max_output_tokens,
            not args.disable_shrinking_retry,
        )
        return

    if args.command == "script-assemble":
        run_script_assemble_command(args.project_id)
        return

    if args.command == "speaker-key-normalize":
        run_speaker_key_normalize_command(args.project_id)
        return

    if args.command == "speaker-key-review":
        run_speaker_key_review_command(
            args.project_id,
            args.response_dir,
            args.confidence_threshold,
        )
        return

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
