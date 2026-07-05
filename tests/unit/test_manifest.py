from core.document.manifest import create_source_manifest, sha256_text
from core.document.txt_loader import load_txt


def test_source_manifest_hash_matches_loaded_text() -> None:
    document = load_txt("tests/fixtures/tiny_source.txt")

    manifest = create_source_manifest("fixture", document)

    assert manifest.sha256 == sha256_text(document.text)
    assert manifest.character_count == len(document.text)
    assert manifest.estimated_token_count > 0
