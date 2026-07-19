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
from core.pipeline.qwen_tts import (
    create_qwen_voice_prompt,
    generate_qwen_clip,
    qwen_delete_readiness_report,
    run_qwen_bootstrap_workflow,
)
from core.pipeline.voice_assets import import_qwen_voice_assets
from core.pipeline.voice_assignment import (
    AudioGenerationProgress,
    build_voice_assignment_artifact,
    run_audio_generation_workflow,
    save_voice_assignments,
)
from tts.qwen.paths import QWEN_DEFAULT_MODEL_ID


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


def run_voice_import_command(
    prompt_source_dir: str,
    sample_source_dirs: list[str],
    voice_root: str,
) -> None:
    try:
        result = import_qwen_voice_assets(
            prompt_source_dir=prompt_source_dir,
            sample_source_dirs=sample_source_dirs,
            voice_root=voice_root,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    sample_count = sum(1 for profile in result.profiles if profile.sample_path)
    print(
        f"Imported {len(result.profiles)} Qwen voice prompts and "
        f"{sample_count} matched samples to {result.voice_root}/voice_profiles.json"
    )


def run_voice_assign_init_command(project_id: str) -> None:
    try:
        result = build_voice_assignment_artifact(project_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"Wrote {len(result.assignments)} speaker voice assignment slots to "
        f"data/interim/{project_id}/voice_assignments.json"
    )


def run_voice_assign_command(project_id: str, assignment_pairs: list[str]) -> None:
    mapping = {}
    for pair in assignment_pairs:
        speaker, separator, profile_id = pair.partition("=")
        if not separator or not speaker.strip() or not profile_id.strip():
            raise SystemExit("assignments must use speaker=voice_profile_id form")
        mapping[speaker.strip()] = profile_id.strip()
    try:
        result = save_voice_assignments(project_id, mapping)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    assigned_count = sum(1 for assignment in result.assignments if assignment.voice_profile_id)
    print(
        f"Saved {assigned_count}/{len(result.assignments)} voice assignments to "
        f"data/interim/{project_id}/voice_assignments.json"
    )


def run_audio_generate_command(project_id: str, only_missing: bool) -> None:
    renderer = AudioProgressRenderer(enabled=sys.stderr.isatty())
    try:
        result = run_audio_generation_workflow(
            project_id,
            only_missing=only_missing,
            progress_callback=renderer.update,
        )
    except RuntimeError as exc:
        renderer.finish()
        raise SystemExit(str(exc)) from exc
    renderer.finish()
    print(
        f"Generated {result['generated_count']} audio takes "
        f"({result['skipped_count']} skipped) under {result['audio_takes_dir']}"
    )


def run_qwen_bootstrap_command(source: str, model: str) -> None:
    try:
        result = run_qwen_bootstrap_workflow(source=source, model=model)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    manifest = result["manifest"]
    status = result["status"]
    print(f"Wrote Qwen bootstrap manifest to {result['manifest_path']}")
    print(f"vendor_package_exists={status['vendor_package_exists']}")
    print(f"model_exists={status['model_exists']} {status['model_path']}")
    print(f"copied_package_files={manifest.copied_package_files}")
    print(f"copied_model_files={manifest.copied_model_files}")
    print(f"copied_voice_profiles={manifest.copied_voice_profiles}")
    if manifest.missing_dependencies:
        print("missing_dependencies=" + ",".join(manifest.missing_dependencies))


def run_voice_prompt_create_command(
    sample: str,
    text: str,
    profile_id: str,
) -> None:
    try:
        result = create_qwen_voice_prompt(
            sample_path=sample,
            transcript=text,
            profile_id=profile_id,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"voice_profile_id={result['profile_id']}")
    print(f"prompt_path={result['prompt_path']}")
    print(f"voice_inventory_path={result['voice_inventory_path']}")


def run_tts_generate_command(
    text: str,
    voice_profile_id: str,
    output: str,
    language: str,
    device: str,
) -> None:
    try:
        result = generate_qwen_clip(
            text=text,
            voice_profile_id=voice_profile_id,
            output_path=output,
            language=language,
            device=device,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"output_path={result['output_path']}")
    print(f"manifest_path={result['manifest_path']}")
    print(f"sample_rate={result['sample_rate']}")


def run_qwen_delete_check_command() -> None:
    report = qwen_delete_readiness_report()
    print(
        "safe_to_delete_qwen_folders="
        f"{str(report['safe_to_delete_qwen_folders']).lower()}"
    )
    print(f"bootstrap_manifest_exists={report['bootstrap_manifest_exists']}")
    print(f"voice_inventory_exists={report['voice_inventory_exists']}")
    print(f"vendor_package_exists={report['vendor_package_exists']}")
    print(f"model_exists={report['model_exists']} {report['model_path']}")
    print(f"missing_dependencies={len(report['missing_dependencies'])}")
    print(f"missing_prompt_files={len(report['missing_prompt_files'])}")
    print(f"old_path_references={len(report['old_path_references'])}")
    for reference in report["old_path_references"]:
        print(f"old_path_reference: {reference}")
    for note in report["notes"]:
        print(f"note: {note}")


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


class AudioProgressRenderer:
    def __init__(self, *, enabled: bool, width: int = 30) -> None:
        self.enabled = enabled
        self.width = width
        self._last_line_length = 0
        self._last_progress_key: tuple[str, str | None, int] | None = None

    def update(self, progress: AudioGenerationProgress) -> None:
        line = self._format(progress)
        if self.enabled:
            padding = max(0, self._last_line_length - len(line))
            print("\r" + line + (" " * padding), end="", file=sys.stderr, flush=True)
            self._last_line_length = len(line)
            return

        progress_key = (
            progress.status,
            progress.current_segment_id,
            progress.completed_segments,
        )
        if progress_key != self._last_progress_key:
            print(line, file=sys.stderr)
            self._last_progress_key = progress_key

    def finish(self) -> None:
        if self.enabled and self._last_line_length:
            print(file=sys.stderr)
            self._last_line_length = 0

    def _format(self, progress: AudioGenerationProgress) -> str:
        total = max(progress.total_segments, 1)
        processed = min(progress.completed_segments, total)
        filled = int(self.width * processed / total)
        bar = "#" * filled + "-" * (self.width - filled)
        segment = progress.current_segment_id or "all"
        speaker = progress.current_speaker or "n/a"
        return (
            f"[{bar}] stage=tts segment={segment} "
            f"speaker={speaker} segments={processed}/{progress.total_segments} "
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

    voice_import_parser = subparsers.add_parser(
        "voice-import",
        help="Copy Qwen .pt voice prompts and source audio samples into data/voices/qwen.",
    )
    voice_import_parser.add_argument(
        "--prompt-source-dir",
        required=True,
    )
    voice_import_parser.add_argument(
        "--sample-source-dir",
        action="append",
        default=["data/voices"],
        help="Directory containing source .wav/.m4a/.mp3/.flac samples.",
    )
    voice_import_parser.add_argument("--voice-root", default="data/voices/qwen")

    voice_assign_init_parser = subparsers.add_parser(
        "voice-assign-init",
        help="Create voice assignment slots from the complete script speaker keys.",
    )
    voice_assign_init_parser.add_argument("--project-id", required=True)

    voice_assign_parser = subparsers.add_parser(
        "voice-assign",
        help="Save speaker=voice_profile_id assignments from the terminal.",
    )
    voice_assign_parser.add_argument("--project-id", required=True)
    voice_assign_parser.add_argument("assignments", nargs="+")

    audio_parser = subparsers.add_parser(
        "audio-generate",
        help="Generate one audio take per script segment from confirmed voice assignments.",
    )
    audio_parser.add_argument("--project-id", required=True)
    audio_parser.add_argument(
        "--all",
        action="store_true",
        help="Regenerate all takes instead of only missing take files.",
    )

    qwen_bootstrap_parser = subparsers.add_parser(
        "qwen-bootstrap",
        help="Copy required Qwen package, model, and voice assets into Narrare.",
    )
    qwen_bootstrap_parser.add_argument("--source", required=True)
    qwen_bootstrap_parser.add_argument("--model", default=QWEN_DEFAULT_MODEL_ID)

    voice_prompt_parser = subparsers.add_parser(
        "voice-prompt-create",
        help="Create a Qwen .pt voice prompt from a sample audio file and transcript.",
    )
    voice_prompt_parser.add_argument("--sample", required=True)
    voice_prompt_parser.add_argument("--text", required=True)
    voice_prompt_parser.add_argument("--profile-id", required=True)

    tts_generate_parser = subparsers.add_parser(
        "tts-generate",
        help="Generate one Qwen TTS clip from text and a voice profile id.",
    )
    tts_generate_parser.add_argument("--text", required=True)
    tts_generate_parser.add_argument("--voice-profile-id", required=True)
    tts_generate_parser.add_argument("--output", required=True)
    tts_generate_parser.add_argument("--language", default="Chinese")
    tts_generate_parser.add_argument(
        "--device",
        choices=["auto", "cpu", "mps", "cuda"],
        default="auto",
        help="Qwen inference device. Use cpu if Apple MPS crashes.",
    )

    subparsers.add_parser(
        "qwen-delete-check",
        help="Check whether old Qwen folders are safe to delete.",
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

    if args.command == "voice-import":
        run_voice_import_command(
            args.prompt_source_dir,
            args.sample_source_dir,
            args.voice_root,
        )
        return

    if args.command == "voice-assign-init":
        run_voice_assign_init_command(args.project_id)
        return

    if args.command == "voice-assign":
        run_voice_assign_command(args.project_id, args.assignments)
        return

    if args.command == "audio-generate":
        run_audio_generate_command(args.project_id, only_missing=not args.all)
        return

    if args.command == "qwen-bootstrap":
        run_qwen_bootstrap_command(args.source, args.model)
        return

    if args.command == "voice-prompt-create":
        run_voice_prompt_create_command(args.sample, args.text, args.profile_id)
        return

    if args.command == "tts-generate":
        run_tts_generate_command(
            args.text,
            args.voice_profile_id,
            args.output,
            args.language,
            args.device,
        )
        return

    if args.command == "qwen-delete-check":
        run_qwen_delete_check_command()
        return

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
