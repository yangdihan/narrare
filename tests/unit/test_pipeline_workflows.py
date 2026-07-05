import json
from pathlib import Path

from core.pipeline.chunking import run_chunking_workflow
from core.pipeline.script_conversion import run_script_conversion_workflow


def test_chunking_workflow_writes_expected_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("第一段。\nSecond paragraph.\n", encoding="utf-8")

    result = run_chunking_workflow(
        source,
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )

    assert result.validation_report.exact_reconstruction_success is True
    assert len(result.chunks) == 1
    assert (tmp_path / "interim" / "fixture_project" / "chunks.json").exists()
    assert (
        tmp_path / "interim" / "fixture_project" / "chunks" / "chunk_0001.txt"
    ).exists()


def test_script_conversion_workflow_writes_ir_with_response_path(
    tmp_path: Path,
) -> None:
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

    result = run_script_conversion_workflow(
        chunk,
        "fixture_project",
        "chunk_0001",
        response_path=response,
        workspace_root=tmp_path / "interim",
    )

    output_root = tmp_path / "interim" / "fixture_project" / "ir" / "script"
    assert result.exact_reconstruction_success is True
    assert result.artifact.processed_chunk_count == 1
    assert (output_root / "chunk_0001_script.json").exists()
    assert (output_root / "chunk_0001_validation_report.json").exists()
    assert (output_root / "chunk_0001" / "attempt_01_raw_response.json").exists()
