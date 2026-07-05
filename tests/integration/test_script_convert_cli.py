import json
from pathlib import Path

from cli.main import run_script_convert_command


def test_script_convert_cli_validates_and_writes_ir(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    chunk = tmp_path / "chunk_0001.txt"
    chunk.write_text("他说，“你好。”\n她点头。", encoding="utf-8")
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "script": {"narrator": "他说"},
                        "confidence": 0.99,
                        "review_notes": [],
                    },
                    {
                        "script": {"安德鲁": "，“你好。"},
                        "confidence": 0.8,
                        "review_notes": ["Speaker inferred from context."],
                    },
                    {
                        "script": {"narrator": "”\n她点头。"},
                        "confidence": 0.95,
                        "review_notes": [],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    run_script_convert_command(
        str(chunk),
        project_id="fixture_project",
        chunk_id="chunk_0001",
        response_path=str(response),
    )

    output_root = Path("data/interim/fixture_project/ir/script")
    assert (output_root / "chunk_0001_script.json").exists()
    assert (output_root / "chunk_0001_validation_report.json").exists()
    assert (output_root / "chunk_0001" / "attempt_01_raw_response.json").exists()
    assert (output_root / "chunk_0001" / "attempt_01_script.json").exists()
    assert (output_root / "chunk_0001" / "attempt_01_validation_report.json").exists()

    report = json.loads(
        (output_root / "chunk_0001_validation_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["exact_reconstruction_success"] is True


def test_script_convert_cli_writes_chunk_attempt_for_chunk_fixture(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    chunk = tmp_path / "chunk_0001.txt"
    chunk.write_text(
        "　　安德鲁·马丁说，“谢谢，”同时坐下。\n\n　　他沉默了。",
        encoding="utf-8",
    )
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "script": {"narrator": "　　安德鲁·马丁说，"},
                        "confidence": 0.99,
                        "review_notes": [],
                    },
                    {
                        "script": {"安德鲁·马丁": "“谢谢，”"},
                        "confidence": 0.95,
                        "review_notes": [],
                    },
                    {
                        "script": {"narrator": "同时坐下。\n\n　　"},
                        "confidence": 0.98,
                        "review_notes": [],
                    },
                    {
                        "script": {"narrator": "他沉默了。"},
                        "confidence": 0.98,
                        "review_notes": [],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    run_script_convert_command(
        str(chunk),
        project_id="fixture_project",
        chunk_id="chunk_0001",
        response_path=str(response),
    )

    output_root = Path("data/interim/fixture_project/ir/script")
    merged = json.loads(
        (output_root / "chunk_0001_script.json").read_text(encoding="utf-8")
    )
    assert merged["processed_chunk_count"] == 1
    assert len(merged["segments"]) == 3
