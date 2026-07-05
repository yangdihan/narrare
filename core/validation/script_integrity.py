from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from core.models.ir import ScriptSegment, ScriptValidationReport

_CHINESE_QUOTE_CHARS = "“”"
_VAGUE_ATTRIBUTION_RE = re.compile(
    r"^[他她它这那]?(?:们)?(?:说|说道|问|问道|回答|答道|喊|叫|低声说|解释|承认|继续说|补充道)$"
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContentMismatch:
    normalized_index: int
    source_offset: int
    reconstructed_offset: int
    source_excerpt: str
    reconstructed_excerpt: str


def validate_script_segments(
    project_id: str,
    chunk_id: str,
    source_text: str,
    segments: list[ScriptSegment],
    source_start: int = 0,
    source_end: int | None = None,
) -> ScriptValidationReport:
    errors: list[str] = []
    reconstructed_parts: list[str] = []
    source_end = len(source_text) if source_end is None else source_end
    expected_start = source_start
    seen_segment_ids: set[str] = set()

    if not segments:
        errors.append("script contains no segments")

    for segment in segments:
        if segment.segment_id in seen_segment_ids:
            errors.append(f"duplicate segment_id: {segment.segment_id}")
        seen_segment_ids.add(segment.segment_id)

        speaker = segment.speaker.strip()
        if _is_vague_attribution_speaker(speaker):
            errors.append(
                f"{segment.segment_id} uses attribution phrase as speaker key: {speaker}"
            )

        if segment.source_span.start != expected_start and _content_text(
            source_text[expected_start : segment.source_span.start]
        ):
            errors.append(
                f"{segment.segment_id} starts at {segment.source_span.start}, "
                f"expected {expected_start}"
            )

        if segment.source_span.end <= segment.source_span.start:
            errors.append(f"{segment.segment_id} has an empty or negative source span")

        if segment.source_span.end > source_end:
            errors.append(f"{segment.segment_id} ends beyond the source text")
            span_text = ""
        else:
            span_text = source_text[segment.source_span.start : segment.source_span.end]

        if _content_text(segment.text) != _content_text(span_text):
            errors.append(
                f"{segment.segment_id} script content does not match source span"
            )
            line_number, line_text = _line_context(source_text, segment.source_span.start)
            script_number = _segment_number(segment.segment_id)
            errors.append(
                f"line {line_number} in {chunk_id} reads {line_text!r}; "
                f"script #{script_number} = {json.dumps(segment.script, ensure_ascii=False)}"
            )

        reconstructed_parts.append(segment.text)
        expected_start = segment.source_span.end

    if expected_start != source_end and _content_text(
        source_text[expected_start:source_end]
    ):
        errors.append(
            f"segments end at {expected_start}, expected source end {source_end}"
        )

    reconstructed = "".join(reconstructed_parts)
    target_text = source_text[source_start:source_end]
    source_content = normalize_content_text(target_text)
    reconstructed_content = normalize_content_text(reconstructed)
    source_hash = sha256_text(source_content)
    reconstructed_hash = sha256_text(reconstructed_content)
    if reconstructed_content != source_content:
        errors.append(
            "reconstructed script content does not match source text "
            "after removing whitespace and punctuation"
        )
        mismatch = find_content_mismatch(target_text, reconstructed)
        if mismatch:
            errors.append(
                "first normalized content mismatch at "
                f"normalized_index={mismatch.normalized_index}, "
                f"source_offset={source_start + mismatch.source_offset}, "
                f"reconstructed_offset={mismatch.reconstructed_offset}; "
                f"source_excerpt={mismatch.source_excerpt!r}; "
                f"reconstructed_excerpt={mismatch.reconstructed_excerpt!r}"
            )

    if _has_chinese_quotes(target_text):
        non_narrator_segments = [
            segment for segment in segments if segment.speaker != "narrator"
        ]
        if not non_narrator_segments:
            errors.append(
                "quoted source text was converted entirely as narrator; "
                "speaker segmentation required"
            )
        if len(segments) == 1 and segments[0].speaker == "narrator":
            errors.append("one giant narrator segment is not valid for quoted text")

    return ScriptValidationReport(
        project_id=project_id,
        chunk_id=chunk_id,
        exact_reconstruction_success=not errors,
        segment_count=len(segments),
        source_character_count=len(target_text),
        reconstructed_character_count=len(reconstructed),
        source_hash=source_hash,
        reconstructed_hash=reconstructed_hash,
        errors=errors,
    )


def _has_chinese_quotes(text: str) -> bool:
    return any(char in text for char in _CHINESE_QUOTE_CHARS)


def _is_vague_attribution_speaker(speaker: str) -> bool:
    return bool(_VAGUE_ATTRIBUTION_RE.fullmatch(speaker))


def _content_text(text: str) -> str:
    return normalize_content_text(text)


def normalize_content_text(text: str) -> str:
    return "".join(char for char in text if _is_voice_char(char))


def find_content_mismatch(
    source_text: str,
    reconstructed_text: str,
    excerpt_radius: int = 12,
) -> ContentMismatch | None:
    source_map = _normalized_index_map(source_text)
    reconstructed_map = _normalized_index_map(reconstructed_text)
    source_content = "".join(char for char, _ in source_map)
    reconstructed_content = "".join(char for char, _ in reconstructed_map)

    if source_content == reconstructed_content:
        return None

    compare_len = min(len(source_content), len(reconstructed_content))
    mismatch_index = compare_len
    for index in range(compare_len):
        if source_content[index] != reconstructed_content[index]:
            mismatch_index = index
            break

    source_offset = (
        source_map[mismatch_index][1]
        if mismatch_index < len(source_map)
        else len(source_text)
    )
    reconstructed_offset = (
        reconstructed_map[mismatch_index][1]
        if mismatch_index < len(reconstructed_map)
        else len(reconstructed_text)
    )
    return ContentMismatch(
        normalized_index=mismatch_index,
        source_offset=source_offset,
        reconstructed_offset=reconstructed_offset,
        source_excerpt=_normalized_excerpt(source_content, mismatch_index, excerpt_radius),
        reconstructed_excerpt=_normalized_excerpt(
            reconstructed_content, mismatch_index, excerpt_radius
        ),
    )


def _normalized_index_map(text: str) -> list[tuple[str, int]]:
    return [(char, index) for index, char in enumerate(text) if _is_voice_char(char)]


def _normalized_excerpt(text: str, index: int, radius: int) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + radius)
    return text[start:end]


def _is_voice_char(char: str) -> bool:
    return char.isalnum()


def _line_context(source_text: str, offset: int) -> tuple[int, str]:
    bounded_offset = min(max(offset, 0), len(source_text))
    line_start = source_text.rfind("\n", 0, bounded_offset) + 1
    line_end = source_text.find("\n", bounded_offset)
    if line_end == -1:
        line_end = len(source_text)
    line_number = source_text.count("\n", 0, bounded_offset) + 1
    return line_number, source_text[line_start:line_end].strip()


def _segment_number(segment_id: str) -> str:
    if segment_id.startswith("seg_"):
        return segment_id.removeprefix("seg_")
    return segment_id
