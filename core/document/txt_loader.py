from pathlib import Path

from core.models.source import SourceDocument


def load_txt(path: str | Path, encoding: str = "utf-8") -> SourceDocument:
    source_path = Path(path)
    text = source_path.read_text(encoding=encoding)
    return SourceDocument(path=source_path, encoding=encoding, text=text)
