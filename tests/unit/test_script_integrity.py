import pytest
from pydantic import ValidationError

from core.models.ir import ScriptSegment
from core.models.source import SourceSpan
from core.validation.script_integrity import (
    find_content_mismatch,
    validate_script_segments,
)


def test_script_segments_reconstruct_source_exactly() -> None:
    source = "他说，“你好。”\n她点头。"
    segments = [
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=2),
            script={"narrator": "他说"},
            confidence=0.99,
        ),
        ScriptSegment(
            segment_id="seg_000002",
            source_span=SourceSpan(start=2, end=7),
            script={"安德鲁": "，“你好。"},
            confidence=0.8,
        ),
        ScriptSegment(
            segment_id="seg_000003",
            source_span=SourceSpan(start=7, end=len(source)),
            script={"narrator": "”\n她点头。"},
            confidence=0.95,
        ),
    ]

    report = validate_script_segments("fixture_project", "chunk_0001", source, segments)

    assert report.exact_reconstruction_success is True
    assert report.errors == []


def test_script_validation_rejects_changed_text() -> None:
    source = "他说，“你好。”"
    segments = [
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=len(source)),
            script={"narrator": "他说，“您好。”"},
            confidence=0.9,
        )
    ]

    report = validate_script_segments("fixture_project", "chunk_0001", source, segments)

    assert report.exact_reconstruction_success is False
    assert "script content does not match source span" in "; ".join(report.errors)
    assert "first normalized content mismatch" in "; ".join(report.errors)
    assert "line 1 in chunk_0001 reads '他说，“你好。”'" in "; ".join(report.errors)
    assert 'script #000001 = {"narrator": "他说，“您好。”"}' in "; ".join(
        report.errors
    )


def test_script_validation_allows_missing_whitespace_line_breaks_and_punctuation() -> None:
    source = "他说，\n\n　　“你好。”"
    segments = [
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=3),
            script={"narrator": "他说"},
            confidence=0.99,
        ),
        ScriptSegment(
            segment_id="seg_000002",
            source_span=SourceSpan(start=3, end=len(source)),
            script={"unknown_speaker": "你好"},
            confidence=0.6,
        ),
    ]

    report = validate_script_segments("fixture_project", "chunk_0001", source, segments)

    assert report.exact_reconstruction_success is True


def test_find_content_mismatch_reports_offsets_while_ignoring_formatting() -> None:
    source = "他说，\n\n　　“你好。”"
    reconstructed = "他说，“您好。”"

    mismatch = find_content_mismatch(source, reconstructed)

    assert mismatch is not None
    assert mismatch.normalized_index == 2
    assert source[mismatch.source_offset] == "你"
    assert reconstructed[mismatch.reconstructed_offset] == "您"


def test_script_segment_requires_one_speaker_key() -> None:
    with pytest.raises(ValidationError):
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=2),
            script={"narrator": "他说", "安德鲁": "你好"},
            confidence=0.9,
        )


def test_script_validation_rejects_one_big_narrator_with_quotes() -> None:
    source = "他说，“你好。”"
    segments = [
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=len(source)),
            script={"narrator": source},
            confidence=0.9,
        )
    ]

    report = validate_script_segments("fixture_project", "chunk_0001", source, segments)

    assert report.exact_reconstruction_success is False
    assert "one giant narrator segment" in "; ".join(report.errors)


def test_script_validation_rejects_attribution_phrase_as_speaker() -> None:
    source = "他说，“你好。”"
    segments = [
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=3),
            script={"narrator": "他说，"},
            confidence=0.99,
        ),
        ScriptSegment(
            segment_id="seg_000002",
            source_span=SourceSpan(start=3, end=len(source)),
            script={"他说": "“你好。”"},
            confidence=0.8,
        ),
    ]

    report = validate_script_segments("fixture_project", "chunk_0001", source, segments)

    assert report.exact_reconstruction_success is False
    assert "uses attribution phrase as speaker key" in "; ".join(report.errors)


def test_script_validation_accepts_unknown_speaker_for_quoted_text() -> None:
    source = "他说，“你好。”"
    segments = [
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=3),
            script={"narrator": "他说，"},
            confidence=0.99,
        ),
        ScriptSegment(
            segment_id="seg_000002",
            source_span=SourceSpan(start=3, end=len(source)),
            script={"unknown_speaker": "“你好。”"},
            confidence=0.6,
            review_notes=["Speaker not confidently known."],
        ),
    ]

    report = validate_script_segments("fixture_project", "chunk_0001", source, segments)

    assert report.exact_reconstruction_success is True
