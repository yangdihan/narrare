from core.ir.script_builder import (
    merge_consecutive_same_speaker,
    split_long_script_segments,
)
from core.models.ir import ScriptSegment
from core.models.source import SourceSpan
from core.validation.script_integrity import validate_script_segments


def test_long_script_splits_at_sentence_punctuation_before_500_chinese_characters() -> None:
    text = "\u7532" * 490 + "\u3002" + "\u4e59" * 20
    segments = merge_consecutive_same_speaker(
        [
            ScriptSegment(
                segment_id="seg_000001",
                source_span=SourceSpan(start=10, end=10 + len(text)),
                script={"narrator": text},
                confidence=0.9,
            )
        ]
    )

    assert [segment.text for segment in segments] == [
        "\u7532" * 490 + "\u3002",
        "\u4e59" * 20,
    ]
    assert [segment.source_span for segment in segments] == [
        SourceSpan(start=10, end=501),
        SourceSpan(start=501, end=521),
    ]
    assert "".join(segment.text for segment in segments) == text


def test_long_script_without_allowed_punctuation_uses_strict_500_character_cap() -> None:
    text = "\u7532" * 501
    segments = split_long_script_segments(
        [
            ScriptSegment(
                segment_id="seg_000001",
                source_span=SourceSpan(start=0, end=len(text)),
                script={"narrator": text},
                confidence=0.9,
            )
        ]
    )

    assert [len(segment.text) for segment in segments] == [500, 1]
    assert "".join(segment.text for segment in segments) == text


def test_length_split_preserves_source_spans_when_script_omits_punctuation() -> None:
    source_text = "\u7532" * 490 + "\u3002" + "\u4e59" * 20
    script_text = "\u7532" * 490 + "\u4e59" * 20
    segments = split_long_script_segments(
        [
            ScriptSegment(
                segment_id="seg_000001",
                source_span=SourceSpan(start=0, end=len(source_text)),
                script={"narrator": script_text},
                confidence=0.9,
            )
        ],
        source_text=source_text,
    )

    report = validate_script_segments(
        project_id="fixture_project",
        chunk_id="complete",
        source_text=source_text,
        segments=segments,
    )

    assert report.exact_reconstruction_success is True
