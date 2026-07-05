from __future__ import annotations

import argparse
import sys

from config.loader import load_config
from core.pipeline.chunking import run_chunking_workflow
from core.pipeline.script_conversion import (
    ScriptProgress,
    run_script_conversion_workflow,
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


class ScriptProgressRenderer:
    def __init__(self, *, enabled: bool, width: int = 30) -> None:
        self.enabled = enabled
        self.width = width
        self._last_line_length = 0
        self._last_progress_key: tuple[str, int] | None = None

    def update(self, progress: ScriptProgress) -> None:
        line = self._format(progress)
        if self.enabled:
            padding = max(0, self._last_line_length - len(line))
            print("\r" + line + (" " * padding), end="", file=sys.stderr, flush=True)
            self._last_line_length = len(line)
            return

        progress_key = (progress.status, progress.attempt)
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
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="narrare")
    subparsers = parser.add_subparsers(dest="command", required=True)

    chunk_parser = subparsers.add_parser("chunk", help="Chunk a TXT source file.")
    chunk_parser.add_argument("source_path")
    chunk_parser.add_argument("--project-id", required=True)

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

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "chunk":
        run_chunk_command(args.source_path, args.project_id)
        return

    if args.command == "script-convert":
        run_script_convert_command(
            args.chunk_path,
            args.project_id,
            args.chunk_id,
            args.response_path,
            args.max_retries,
            args.llm_max_output_tokens,
        )
        return

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
