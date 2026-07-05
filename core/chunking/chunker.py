from __future__ import annotations

from dataclasses import dataclass
import math
import re

from core.models.chunk import ChunkingConfig, TextChunk
from core.models.source import SourceSpan


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class TokenUnit:
    start: int
    end: int
    estimated_tokens: int


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def token_units(text: str) -> list[TokenUnit]:
    units: list[TokenUnit] = []
    cursor = 0

    while cursor < len(text):
        char = text[cursor]

        if _is_cjk(char):
            units.append(TokenUnit(cursor, cursor + 1, 1))
            cursor += 1
            continue

        if char == "\n":
            units.append(TokenUnit(cursor, cursor + 1, 1))
            cursor += 1
            continue

        match = _ASCII_WORD_RE.match(text, cursor)
        if match:
            start, end = match.span()
            units.append(TokenUnit(start, end, max(1, math.ceil((end - start) / 4))))
            cursor = end
            continue

        start = cursor
        cursor += 1
        while cursor < len(text) and not _is_cjk(text[cursor]):
            if text[cursor] == "\n":
                break
            if _ASCII_WORD_RE.match(text, cursor):
                break
            cursor += 1
        units.append(TokenUnit(start, cursor, 1))

    return units


def estimate_tokens(text: str) -> int:
    return sum(unit.estimated_tokens for unit in token_units(text))


def _context_before(
    text: str, units: list[TokenUnit], boundary: int, overlap_tokens: int
) -> tuple[SourceSpan, str, int]:
    if overlap_tokens <= 0 or boundary <= 0:
        span = SourceSpan(start=boundary, end=boundary)
        return span, "", 0

    selected: list[TokenUnit] = []
    total = 0
    for unit in reversed(units):
        if unit.end > boundary:
            continue
        selected.append(unit)
        total += unit.estimated_tokens
        if total >= overlap_tokens:
            break

    if not selected:
        span = SourceSpan(start=boundary, end=boundary)
        return span, "", 0

    start = min(unit.start for unit in selected)
    span = SourceSpan(start=start, end=boundary)
    return span, text[span.start : span.end], estimate_tokens(text[span.start : span.end])


def _context_after(
    text: str, units: list[TokenUnit], boundary: int, overlap_tokens: int
) -> tuple[SourceSpan, str, int]:
    if overlap_tokens <= 0 or boundary >= len(text):
        span = SourceSpan(start=boundary, end=boundary)
        return span, "", 0

    selected: list[TokenUnit] = []
    total = 0
    for unit in units:
        if unit.start < boundary:
            continue
        selected.append(unit)
        total += unit.estimated_tokens
        if total >= overlap_tokens:
            break

    if not selected:
        span = SourceSpan(start=boundary, end=boundary)
        return span, "", 0

    end = max(unit.end for unit in selected)
    span = SourceSpan(start=boundary, end=end)
    return span, text[span.start : span.end], estimate_tokens(text[span.start : span.end])


def _nearest_line_break_boundary(
    text: str, start: int, provisional_end: int, target_chunk_tokens: int
) -> int:
    if provisional_end >= len(text):
        return len(text)

    candidates: list[tuple[int, int, int]] = []
    previous_newline = text.rfind("\n", start + 1, provisional_end + 1)
    if previous_newline != -1:
        boundary = previous_newline + 1
        candidates.append((abs(provisional_end - boundary), 0, boundary))

    next_newline = text.find("\n", provisional_end)
    if next_newline != -1:
        boundary = next_newline + 1
        if estimate_tokens(text[start:boundary]) <= target_chunk_tokens:
            candidates.append((abs(provisional_end - boundary), 1, boundary))

    if not candidates:
        return provisional_end

    return min(candidates)[2]


def _paragraph_spans(text: str) -> list[SourceSpan]:
    if not text:
        return []

    spans: list[SourceSpan] = []
    start = 0
    cursor = 0
    while cursor < len(text):
        if text[cursor] == "\n":
            spans.append(SourceSpan(start=start, end=cursor + 1))
            start = cursor + 1
        cursor += 1

    if start < len(text):
        spans.append(SourceSpan(start=start, end=len(text)))

    return spans


def _group_paragraph_spans(text: str, config: ChunkingConfig) -> list[SourceSpan]:
    paragraph_spans = _paragraph_spans(text)
    grouped: list[SourceSpan] = []
    current_start: int | None = None
    current_end: int | None = None

    for paragraph in paragraph_spans:
        if paragraph.end - paragraph.start > config.max_chunk_chars:
            if current_start is not None and current_end is not None:
                grouped.append(SourceSpan(start=current_start, end=current_end))
                current_start = None
                current_end = None
            grouped.extend(_split_oversized_span(paragraph, config.max_chunk_chars))
            continue

        if current_start is None:
            current_start = paragraph.start
            current_end = paragraph.end
            continue

        assert current_end is not None
        current_length = current_end - current_start
        candidate_length = paragraph.end - current_start
        if (
            current_length >= config.min_chunk_chars
            and candidate_length > config.target_chunk_chars
        ) or candidate_length > config.max_chunk_chars:
            grouped.append(SourceSpan(start=current_start, end=current_end))
            current_start = paragraph.start
            current_end = paragraph.end
            continue

        current_end = paragraph.end
        if current_end - current_start >= config.target_chunk_chars:
            grouped.append(SourceSpan(start=current_start, end=current_end))
            current_start = None
            current_end = None

    if current_start is not None and current_end is not None:
        grouped.append(SourceSpan(start=current_start, end=current_end))

    return grouped


def _split_oversized_span(span: SourceSpan, max_chars: int) -> list[SourceSpan]:
    return [
        SourceSpan(start=start, end=min(span.end, start + max_chars))
        for start in range(span.start, span.end, max_chars)
    ]


def chunk_text(text: str, config: ChunkingConfig | None = None) -> list[TextChunk]:
    config = config or ChunkingConfig()
    units = token_units(text)
    chunks: list[TextChunk] = []

    for index, source_span in enumerate(_group_paragraph_spans(text, config)):
        chunk_body = text[source_span.start : source_span.end]
        prev_span, prev_text, prev_tokens = _context_before(
            text, units, source_span.start, config.overlap_tokens
        )
        next_span, next_text, next_tokens = _context_after(
            text, units, source_span.end, config.overlap_tokens
        )

        chunks.append(
            TextChunk(
                chunk_id=f"chunk_{index + 1:04d}",
                index=index,
                source_span=source_span,
                text=chunk_body,
                estimated_tokens=estimate_tokens(chunk_body),
                previous_context_span=prev_span,
                previous_context=prev_text,
                previous_context_estimated_tokens=prev_tokens,
                next_context_span=next_span,
                next_context=next_text,
                next_context_estimated_tokens=next_tokens,
            )
        )

    return chunks
