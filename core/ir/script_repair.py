from __future__ import annotations

from dataclasses import dataclass

from core.models.ir import RawScriptSegment, ScriptSegment
from core.models.source import SourceSpan


@dataclass(frozen=True)
class RepairSpan:
    start: int
    end: int
    reason: str
    prefix_segment_count: int
    suffix_segment_count: int


@dataclass(frozen=True)
class RepairDiagnosis:
    span: RepairSpan
    prefix_segments: list[ScriptSegment]
    suffix_segments: list[ScriptSegment]


@dataclass(frozen=True)
class SegmentAlignment:
    raw_index: int
    segment: ScriptSegment


@dataclass(frozen=True)
class ForwardAlignment:
    aligned: list[SegmentAlignment]
    mismatch_offset: int | None
    mismatch_raw_index: int | None


def diagnose_repair_span(
    source_text: str,
    raw_segments: list[RawScriptSegment],
) -> RepairDiagnosis | None:
    forward = align_segments_forward(source_text, raw_segments)
    prefix_segments = [item.segment for item in forward.aligned]
    prefix_end = prefix_segments[-1].source_span.end if prefix_segments else 0

    suffix_aligned = align_segments_reverse(
        source_text,
        raw_segments,
        stop_before_offset=prefix_end,
        stop_before_raw_index=forward.mismatch_raw_index,
    )
    suffix_segments = [item.segment for item in suffix_aligned]
    suffix_start = suffix_segments[0].source_span.start if suffix_segments else len(source_text)

    if suffix_start < prefix_end:
        return None

    seed_start = (
        forward.mismatch_offset if forward.mismatch_offset is not None else prefix_end
    )
    seed_end = suffix_start

    repair_start, repair_end = expand_to_paragraph_bounds(
        source_text,
        seed_start,
        seed_end,
    )
    prefix_segments = [
        segment for segment in prefix_segments if segment.source_span.end <= repair_start
    ]
    suffix_segments = [
        segment for segment in suffix_segments if segment.source_span.start >= repair_end
    ]

    if not prefix_segments and not suffix_segments:
        return None

    if repair_start >= repair_end:
        return None

    reason = (
        "source/script misalignment"
        if forward.mismatch_offset is not None
        else "content reconstruction mismatch"
    )
    return RepairDiagnosis(
        span=RepairSpan(
            start=repair_start,
            end=repair_end,
            reason=reason,
            prefix_segment_count=len(prefix_segments),
            suffix_segment_count=len(suffix_segments),
        ),
        prefix_segments=prefix_segments,
        suffix_segments=suffix_segments,
    )


def align_segments_forward(
    source_text: str,
    raw_segments: list[RawScriptSegment],
) -> ForwardAlignment:
    aligned: list[SegmentAlignment] = []
    cursor = 0

    for raw_index, raw in enumerate(raw_segments):
        if not _voice_content_text(raw.text):
            continue
        match = _align_text_forward(raw.text, source_text, cursor, len(source_text))
        if match is None:
            return ForwardAlignment(
                aligned=aligned,
                mismatch_offset=_first_voice_offset(source_text, cursor),
                mismatch_raw_index=raw_index,
            )
        span_start, span_end = match
        aligned.append(
            SegmentAlignment(
                raw_index=raw_index,
                segment=ScriptSegment(
                    segment_id=f"seg_{len(aligned) + 1:06d}",
                    source_span=SourceSpan(start=span_start, end=span_end),
                    script=raw.script,
                    confidence=raw.confidence,
                    review_notes=raw.review_notes,
                ),
            )
        )
        cursor = span_end

    if _voice_content_text(source_text[cursor:]):
        return ForwardAlignment(
            aligned=aligned,
            mismatch_offset=_first_voice_offset(source_text, cursor),
            mismatch_raw_index=None,
        )

    return ForwardAlignment(
        aligned=aligned,
        mismatch_offset=None,
        mismatch_raw_index=None,
    )


def align_segments_reverse(
    source_text: str,
    raw_segments: list[RawScriptSegment],
    *,
    stop_before_offset: int = 0,
    stop_before_raw_index: int | None = None,
) -> list[SegmentAlignment]:
    aligned: list[SegmentAlignment] = []
    cursor_end = len(source_text)

    for raw_index in range(len(raw_segments) - 1, -1, -1):
        if stop_before_raw_index is not None and raw_index <= stop_before_raw_index:
            break

        raw = raw_segments[raw_index]
        if not _voice_content_text(raw.text):
            continue
        match = _align_text_reverse(raw.text, source_text, stop_before_offset, cursor_end)
        if match is None:
            break
        span_start, span_end = match
        if span_start < stop_before_offset:
            break
        aligned.append(
            SegmentAlignment(
                raw_index=raw_index,
                segment=ScriptSegment(
                    segment_id=f"seg_{raw_index + 1:06d}",
                    source_span=SourceSpan(start=span_start, end=span_end),
                    script=raw.script,
                    confidence=raw.confidence,
                    review_notes=raw.review_notes,
                ),
            )
        )
        cursor_end = span_start

    return list(reversed(aligned))


def expand_to_paragraph_bounds(
    source_text: str,
    start: int,
    end: int,
) -> tuple[int, int]:
    bounded_start = min(max(start, 0), len(source_text))
    bounded_end = min(max(end, bounded_start), len(source_text))

    paragraph_start = source_text.rfind("\n", 0, bounded_start) + 1
    if bounded_end < len(source_text) and (
        bounded_end == 0 or source_text[bounded_end - 1] == "\n"
    ):
        paragraph_end = bounded_end
    else:
        next_break = source_text.find("\n", bounded_end)
        paragraph_end = len(source_text) if next_break == -1 else next_break + 1
    return paragraph_start, paragraph_end


def renumber_segments(
    segments: list[ScriptSegment], starting_index: int = 0
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


def _align_text_forward(
    text: str,
    source_text: str,
    cursor: int,
    source_end: int,
) -> tuple[int, int] | None:
    span_start = cursor
    source_cursor = cursor

    for char in text:
        if not _is_voice_char(char):
            continue
        while source_cursor < source_end and not _is_voice_char(
            source_text[source_cursor]
        ):
            source_cursor += 1
        if source_cursor >= source_end or source_text[source_cursor] != char:
            return None
        source_cursor += 1

    return span_start, source_cursor


def _align_text_reverse(
    text: str,
    source_text: str,
    source_start: int,
    cursor_end: int,
) -> tuple[int, int] | None:
    source_cursor = cursor_end - 1
    span_end = cursor_end

    for char in reversed(text):
        if not _is_voice_char(char):
            continue
        while source_cursor >= source_start and not _is_voice_char(
            source_text[source_cursor]
        ):
            source_cursor -= 1
        if source_cursor < source_start or source_text[source_cursor] != char:
            return None
        source_cursor -= 1

    return source_cursor + 1, span_end


def _first_voice_offset(source_text: str, cursor: int) -> int:
    for index in range(cursor, len(source_text)):
        if _is_voice_char(source_text[index]):
            return index
    return len(source_text)


def _voice_content_text(text: str) -> str:
    return "".join(char for char in text if _is_voice_char(char))


def _is_voice_char(char: str) -> bool:
    return char.isalnum()
