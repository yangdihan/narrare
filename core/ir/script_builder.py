from __future__ import annotations

import json

from core.models.ir import RawScriptSegment, ScriptSegment
from core.models.source import SourceSpan


class ScriptTextAlignmentError(ValueError):
    def __init__(self, message: str, source_offset: int) -> None:
        super().__init__(message)
        self.source_offset = source_offset


def derive_script_segments(
    raw_segments: list[RawScriptSegment],
    source_start: int,
    starting_index: int = 0,
    source_text: str | None = None,
    source_end: int | None = None,
    chunk_id: str | None = None,
) -> list[ScriptSegment]:
    segments: list[ScriptSegment] = []
    cursor = source_start

    for raw_index, raw in enumerate(raw_segments):
        text = raw.text
        if not _voice_content_text(text):
            continue

        if source_text is None:
            end = cursor + len(text)
            span_start = cursor
        else:
            try:
                span_start, end = _align_text_to_source(
                    text,
                    source_text=source_text,
                    cursor=cursor,
                    source_end=source_end if source_end is not None else len(source_text),
                )
            except ScriptTextAlignmentError as exc:
                line_number, line_text = _line_context(source_text, exc.source_offset)
                script_number = starting_index + raw_index + 1
                chunk_label = chunk_id or "chunk"
                script_value = json.dumps(raw.script, ensure_ascii=False)
                raise ValueError(
                    f"{exc}; line {line_number} in {chunk_label} reads "
                    f"{line_text!r}; script #{script_number:06d} = {script_value}"
                ) from exc
        segments.append(
            ScriptSegment(
                segment_id=f"seg_{starting_index + len(segments) + 1:06d}",
                source_span=SourceSpan(start=span_start, end=end),
                script=raw.script,
                confidence=raw.confidence,
                review_notes=raw.review_notes,
            )
        )
        cursor = end

    return segments


def _line_context(source_text: str, offset: int) -> tuple[int, str]:
    bounded_offset = min(max(offset, 0), len(source_text))
    line_start = source_text.rfind("\n", 0, bounded_offset) + 1
    line_end = source_text.find("\n", bounded_offset)
    if line_end == -1:
        line_end = len(source_text)
    line_number = source_text.count("\n", 0, bounded_offset) + 1
    return line_number, source_text[line_start:line_end].strip()


def _align_text_to_source(
    text: str,
    *,
    source_text: str,
    cursor: int,
    source_end: int,
) -> tuple[int, int]:
    if not _voice_content_text(text):
        raise ValueError("script segment contains no voice-bearing content")

    span_start = cursor
    source_cursor = cursor

    for char in text:
        if not _is_voice_char(char):
            continue

        while source_cursor < source_end and not _is_voice_char(
            source_text[source_cursor]
        ):
            source_cursor += 1

        if source_cursor >= source_end:
            raise ScriptTextAlignmentError(
                f"script text extends beyond source while matching {char!r}",
                source_cursor,
            )

        if source_text[source_cursor] != char:
            raise ScriptTextAlignmentError(
                "script text content does not match source: "
                f"expected {source_text[source_cursor]!r}, got {char!r}",
                source_cursor,
            )

        source_cursor += 1

    return span_start, source_cursor


def _voice_content_text(text: str) -> str:
    return "".join(char for char in text if _is_voice_char(char))


def _is_voice_char(char: str) -> bool:
    return char.isalnum()


def merge_consecutive_same_speaker(
    segments: list[ScriptSegment],
    starting_index: int = 0,
) -> list[ScriptSegment]:
    if not segments:
        return []

    merged: list[ScriptSegment] = []
    current = segments[0]

    for segment in segments[1:]:
        if segment.speaker != current.speaker:
            merged.append(current)
            current = segment
            continue

        current = ScriptSegment(
            segment_id=current.segment_id,
            source_span=SourceSpan(
                start=current.source_span.start,
                end=segment.source_span.end,
            ),
            script={current.speaker: current.text + segment.text},
            confidence=min(current.confidence, segment.confidence),
            review_notes=[
                *current.review_notes,
                *segment.review_notes,
                "Merged consecutive same-speaker segments deterministically.",
            ],
        )

    merged.append(current)
    return _renumber_segments(merged, starting_index)


def _renumber_segments(
    segments: list[ScriptSegment], starting_index: int
) -> list[ScriptSegment]:
    return [
        ScriptSegment(
            segment_id=f"seg_{starting_index + index + 1:06d}",
            source_span=segment.source_span,
            script=segment.script,
            confidence=segment.confidence,
            review_notes=segment.review_notes,
        )
        for index, segment in enumerate(segments)
    ]
