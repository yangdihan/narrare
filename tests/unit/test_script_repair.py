from core.ir.script_repair import (
    align_segments_forward,
    align_segments_reverse,
    diagnose_repair_span,
    expand_to_paragraph_bounds,
)
from core.models.ir import RawScriptSegment


def test_forward_alignment_stops_at_misaligned_segment() -> None:
    source = "甲甲。\n乙乙。\n丙丙。"
    raw_segments = [
        RawScriptSegment(script={"narrator": "甲甲"}, confidence=0.9),
        RawScriptSegment(script={"narrator": "乙错"}, confidence=0.9),
        RawScriptSegment(script={"narrator": "丙丙"}, confidence=0.9),
    ]

    alignment = align_segments_forward(source, raw_segments)

    assert len(alignment.aligned) == 1
    assert alignment.mismatch_raw_index == 1
    assert alignment.mismatch_offset == 4


def test_reverse_alignment_finds_valid_suffix_after_mismatch() -> None:
    source = "甲甲。\n乙乙。\n丙丙。"
    raw_segments = [
        RawScriptSegment(script={"narrator": "甲甲"}, confidence=0.9),
        RawScriptSegment(script={"narrator": "乙错"}, confidence=0.9),
        RawScriptSegment(script={"narrator": "丙丙"}, confidence=0.9),
    ]

    suffix = align_segments_reverse(
        source,
        raw_segments,
        stop_before_offset=2,
        stop_before_raw_index=1,
    )

    assert len(suffix) == 1
    assert suffix[0].raw_index == 2
    assert suffix[0].segment.text == "丙丙"


def test_repair_span_expands_to_paragraph_bounds() -> None:
    source = "甲甲。\n乙乙。\n丙丙。"

    assert expand_to_paragraph_bounds(source, 5, 6) == (4, 8)


def test_diagnose_repair_span_uses_good_prefix_and_suffix() -> None:
    source = "甲甲。\n乙乙。\n丙丙。"
    raw_segments = [
        RawScriptSegment(script={"narrator": "甲甲"}, confidence=0.9),
        RawScriptSegment(script={"narrator": "乙错"}, confidence=0.9),
        RawScriptSegment(script={"narrator": "丙丙"}, confidence=0.9),
    ]

    diagnosis = diagnose_repair_span(source, raw_segments)

    assert diagnosis is not None
    assert diagnosis.span.start == 4
    assert diagnosis.span.end == 8
    assert diagnosis.span.prefix_segment_count == 1
    assert diagnosis.span.suffix_segment_count == 1
    assert [segment.text for segment in diagnosis.prefix_segments] == ["甲甲"]
    assert [segment.text for segment in diagnosis.suffix_segments] == ["丙丙"]


def test_diagnose_repair_span_returns_none_without_stable_anchors() -> None:
    source = "甲甲。"
    raw_segments = [
        RawScriptSegment(script={"narrator": "错错"}, confidence=0.9),
    ]

    assert diagnose_repair_span(source, raw_segments) is None
