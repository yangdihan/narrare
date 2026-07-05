from core.chunking.chunker import chunk_text, estimate_tokens
from core.models.chunk import ChunkingConfig


def test_estimated_token_count_is_deterministic() -> None:
    text = "第一行。Andrew said hello."

    assert estimate_tokens(text) == estimate_tokens(text)


def test_chunks_are_contiguous_and_reconstruct_source() -> None:
    text = "第一段。" * 50 + "\nAndrew said hello. " * 50 + "最后一段。" * 50
    config = ChunkingConfig(
        target_chunk_tokens=80,
        min_chunk_chars=40,
        target_chunk_chars=80,
        max_chunk_chars=120,
        overlap_tokens=8,
    )

    chunks = chunk_text(text, config)

    assert "".join(chunk.text for chunk in chunks) == text
    expected_start = 0
    for chunk in chunks:
        assert chunk.source_span.start == expected_start
        assert chunk.source_span.end > chunk.source_span.start
        assert 0 <= chunk.previous_context_span.start <= chunk.previous_context_span.end
        assert 0 <= chunk.next_context_span.start <= chunk.next_context_span.end
        assert chunk.previous_context_span.end <= len(text)
        assert chunk.next_context_span.end <= len(text)
        expected_start = chunk.source_span.end
    assert expected_start == len(text)


def test_chunk_boundaries_snap_to_nearest_line_break() -> None:
    text = ("一" * 30) + "\n" + ("二" * 30) + "\n" + ("三" * 30)
    config = ChunkingConfig(
        target_chunk_tokens=40,
        min_chunk_chars=1,
        target_chunk_chars=35,
        max_chunk_chars=40,
        overlap_tokens=0,
    )

    chunks = chunk_text(text, config)

    assert len(chunks) == 3
    assert chunks[0].text == ("一" * 30) + "\n"
    assert chunks[1].text == ("二" * 30) + "\n"
    assert chunks[2].text == "三" * 30
    assert "".join(chunk.text for chunk in chunks) == text


def test_chunking_groups_short_paragraphs_until_character_target() -> None:
    text = "甲" * 10 + "\n" + "乙" * 10 + "\n" + "丙" * 10 + "\n"
    config = ChunkingConfig(
        target_chunk_tokens=40,
        min_chunk_chars=15,
        target_chunk_chars=25,
        max_chunk_chars=40,
        overlap_tokens=0,
    )

    chunks = chunk_text(text, config)

    assert len(chunks) == 2
    assert chunks[0].text == ("甲" * 10) + "\n" + ("乙" * 10) + "\n"
    assert chunks[1].text == ("丙" * 10) + "\n"
