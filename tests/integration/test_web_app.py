import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from storage.json_store import write_json
from storage.workspace import Workspace
from ui.web.app import create_app


def test_web_api_lists_loads_and_chunks_source(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "tiny.txt"
    source.write_text("第一段。\nSecond paragraph.\n", encoding="utf-8")
    app = create_app(raw_dir=raw_dir, workspace_root=tmp_path / "interim")
    client = TestClient(app)

    assert "Narrare Pipeline" in client.get("/").text

    sources = client.get("/api/sources").json()["sources"]
    assert sources[0]["name"] == "tiny.txt"

    loaded = client.get("/api/source", params={"path": sources[0]["path"]}).json()
    assert loaded["text"] == "第一段。\nSecond paragraph.\n"
    assert loaded["default_project_id"] == "tiny"

    chunked = client.post(
        "/api/chunk",
        json={"source_path": sources[0]["path"], "project_id": "fixture_project"},
    ).json()
    assert chunked["validation_report"]["exact_reconstruction_success"] is True
    assert chunked["chunks"][0]["chunk_id"] == "chunk_0001"

    chunks = client.get("/api/projects/fixture_project/chunks").json()
    assert chunks["chunks"][0]["text"] == "第一段。\nSecond paragraph.\n"


def test_web_stage2_job_and_script_endpoint(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "tiny.txt"
    source.write_text("他说，“你好。”\n她点头。", encoding="utf-8")
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "segments": [
                    {"script": {"narrator": "他说"}, "confidence": 0.99},
                    {"script": {"安德鲁": "，“你好。"}, "confidence": 0.8},
                    {"script": {"narrator": "”\n她点头。"}, "confidence": 0.95},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = create_app(raw_dir=raw_dir, workspace_root=tmp_path / "interim")
    client = TestClient(app)
    client.post(
        "/api/chunk",
        json={"source_path": str(source), "project_id": "fixture_project"},
    )

    job = client.post(
        "/api/stage2/jobs",
        json={
            "project_id": "fixture_project",
            "chunk_id": "chunk_0001",
            "response_path": str(response),
        },
    ).json()

    status = job
    for _ in range(20):
        status = client.get(f"/api/stage2/jobs/{job['job_id']}").json()
        if status["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)

    assert status["status"] == "complete"
    assert status["processed_windows"] == 1

    script = client.get("/api/projects/fixture_project/script/chunk_0001").json()
    assert script["validation_report"]["exact_reconstruction_success"] is True
    assert [segment["validation_status"] for segment in script["segments"]] == [
        "passed",
        "passed",
        "passed",
    ]


def test_script_endpoint_marks_bad_segment_red_payload(tmp_path: Path) -> None:
    app = create_app(raw_dir=tmp_path / "raw", workspace_root=tmp_path / "interim")
    client = TestClient(app)
    workspace = Workspace("fixture_project", root=tmp_path / "interim")
    workspace.ensure()
    chunk_path = workspace.chunk_text_path(0)
    chunk_path.write_text("abc", encoding="utf-8")
    write_json(
        workspace.script_artifact_path("chunk_0001"),
        {
            "project_id": "fixture_project",
            "chunk_id": "chunk_0001",
            "chunk_source_path": str(chunk_path),
            "chunk_sha256": "unused",
            "llm_provider": "test",
            "llm_model": "test",
            "response_source": "response_path",
            "processed_window_count": 1,
            "segments": [
                {
                    "segment_id": "seg_000001",
                    "source_span": {"start": 0, "end": 3},
                    "script": {"narrator": "axc"},
                    "confidence": 0.5,
                    "review_notes": [],
                }
            ],
        },
    )
    write_json(
        workspace.script_validation_report_path("chunk_0001"),
        {
            "project_id": "fixture_project",
            "chunk_id": "chunk_0001",
            "exact_reconstruction_success": False,
            "segment_count": 1,
            "source_character_count": 3,
            "reconstructed_character_count": 3,
            "source_hash": "unused",
            "reconstructed_hash": "unused",
            "errors": ["seg_000001 script text does not match source span"],
        },
    )

    script = client.get("/api/projects/fixture_project/script/chunk_0001").json()

    assert script["segments"][0]["validation_status"] == "failed"
    assert "script text does not match source span" in "; ".join(
        script["segments"][0]["validation_errors"]
    )
