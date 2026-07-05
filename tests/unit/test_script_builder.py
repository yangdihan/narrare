import pytest

from core.ir.script_builder import derive_script_segments
from core.models.ir import RawScriptSegment


def test_script_alignment_error_reports_chunk_line_and_script() -> None:
    source = "第一行。\n他说，“你好。”"
    raw_segments = [
        RawScriptSegment(
            script={"narrator": "第一行。"},
            confidence=0.99,
        ),
        RawScriptSegment(
            script={"安德鲁": "他说，“您好。”"},
            confidence=0.8,
        ),
    ]

    with pytest.raises(ValueError) as exc_info:
        derive_script_segments(
            raw_segments,
            source_start=0,
            source_text=source,
            source_end=len(source),
            chunk_id="chunk_0001",
        )

    message = str(exc_info.value)
    assert "line 2 in chunk_0001 reads '他说，“你好。”'" in message
    assert 'script #000002 = {"安德鲁": "他说，“您好。”"}' in message
