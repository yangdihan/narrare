from pathlib import Path

from core.document.txt_loader import load_txt


def test_load_txt_preserves_exact_text() -> None:
    path = Path("tests/fixtures/tiny_source.txt")
    expected = path.read_text(encoding="utf-8")

    document = load_txt(path)

    assert document.text == expected
    assert document.character_count == len(expected)
    assert document.encoding == "utf-8"
