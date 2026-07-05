from pathlib import Path

from cli.main import run_chunk_command


def test_cli_writes_expected_chunk_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "tiny.txt"
    source.write_text("第一段。\nSecond paragraph.\n", encoding="utf-8")

    run_chunk_command(str(source), "fixture_project")

    output_root = Path("data/interim/fixture_project")
    assert (output_root / "source_manifest.json").exists()
    assert (output_root / "chunks.json").exists()
    assert (output_root / "validation_report.json").exists()
    assert (output_root / "chunks" / "chunk_0001.txt").exists()
