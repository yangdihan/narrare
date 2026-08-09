from __future__ import annotations

import json

from core.models.ir import RawScriptSegment, ScriptSegment
from core.models.source import SourceSpan

MAX_SCRIPT_CHINESE_CHARACTERS = 500
SENTENCE_SPLIT_PUNCTUATION = frozenset("。！？；：…")


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
    source_text: str | None = None,
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
    return _renumber_segments(
        split_long_script_segments(merged, source_text=source_text),
        # Segment spans may include punctuation that the LLM omitted. Use the
        # immutable source to retain valid spans when an overlong segment splits.
        starting_index,
    )


def split_long_script_segments(
    segments: list[ScriptSegment],
    *,
    max_chinese_characters: int = MAX_SCRIPT_CHINESE_CHARACTERS,
    source_text: str | None = None,
) -> list[ScriptSegment]:
    """Split overlong scripts without changing their text or speaker key.

    Sentence punctuation is preferred so local TTS takes end at natural
    boundaries. Commas and enumeration commas are deliberately excluded.
    """
    if max_chinese_characters <= 0:
        raise ValueError("max_chinese_characters must be positive")

    split_segments: list[ScriptSegment] = []
    for segment in segments:
        if _chinese_character_count(segment.text) <= max_chinese_characters:
            split_segments.append(segment)
            continue
        offset = 0
        span_start = segment.source_span.start
        for part_number, end in enumerate(
            _split_offsets(segment.text, max_chinese_characters), start=1
        ):
            text = segment.text[offset:end]
            span_end = (
                segment.source_span.end
                if end == len(segment.text)
                else _source_split_offset(segment, end, source_text)
            )
            split_segments.append(
                ScriptSegment(
                    segment_id=(
                        segment.segment_id
                        if part_number == 1
                        else f"{segment.segment_id}_part_{part_number:02d}"
                    ),
                    source_span=SourceSpan(
                        start=span_start,
                        end=span_end,
                    ),
                    script={segment.speaker: text},
                    raw_script_key=segment.raw_script_key,
                    speaker_key_normalization=segment.speaker_key_normalization,
                    speaker_key_review=segment.speaker_key_review,
                    confidence=segment.confidence,
                    review_notes=[
                        *segment.review_notes,
                        "Split to keep the TTS script within 500 Chinese characters.",
                    ]
                    if end < len(segment.text)
                    else segment.review_notes,
                )
            )
            offset = end
            span_start = span_end
    return split_segments


def _source_split_offset(
    segment: ScriptSegment,
    text_end: int,
    source_text: str | None,
) -> int:
    if source_text is None:
        return segment.source_span.start + text_end

    source_cursor = segment.source_span.start
    source_end = segment.source_span.end
    for char in segment.text[:text_end]:
        if not _is_voice_char(char):
            continue
        while source_cursor < source_end and not _is_voice_char(
            source_text[source_cursor]
        ):
            source_cursor += 1
        if source_cursor >= source_end or source_text[source_cursor] != char:
            raise ScriptTextAlignmentError(
                "script text content does not match source while splitting", source_cursor
            )
        source_cursor += 1
    return source_cursor


def _split_offsets(text: str, max_chinese_characters: int) -> list[int]:
    if _chinese_character_count(text) <= max_chinese_characters:
        return [len(text)]

    offsets: list[int] = []
    start = 0
    while start < len(text):
        end = _max_offset_for_chinese_characters(
            text,
            start,
            max_chinese_characters,
        )
        if end == len(text):
            offsets.append(end)
            break

        boundary = _last_sentence_boundary(text, start, end)
        # A hard fallback is necessary when a source segment has no permitted
        # sentence punctuation before its limit; it keeps the TTS cap strict.
        offsets.append(boundary if boundary is not None else end)
        start = offsets[-1]
    return offsets


def _max_offset_for_chinese_characters(text: str, start: int, limit: int) -> int:
    chinese_characters = 0
    for index in range(start, len(text)):
        if _is_chinese_character(text[index]):
            chinese_characters += 1
            if chinese_characters > limit:
                return index
    return len(text)


def _last_sentence_boundary(text: str, start: int, end: int) -> int | None:
    for index in range(end - 1, start - 1, -1):
        if text[index] in SENTENCE_SPLIT_PUNCTUATION:
            return index + 1
    return None


def _chinese_character_count(text: str) -> int:
    return sum(_is_chinese_character(char) for char in text)


def _is_chinese_character(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"


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
