from datetime import UTC, datetime
import hashlib

from core.chunking.chunker import estimate_tokens
from core.models.source import SourceDocument, SourceManifest


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_source_manifest(project_id: str, document: SourceDocument) -> SourceManifest:
    return SourceManifest(
        project_id=project_id,
        source_path=str(document.path),
        encoding=document.encoding,
        sha256=sha256_text(document.text),
        character_count=document.character_count,
        estimated_token_count=estimate_tokens(document.text),
        created_at=datetime.now(UTC).isoformat(),
    )
