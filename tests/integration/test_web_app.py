import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from core.models.voice import VoiceInventoryArtifact, VoiceProfile
from storage.json_store import write_json
from storage.workspace import Workspace
from ui.web.app import create_app


def _wait_for_job(client: TestClient, job_id: str) -> dict:
    status = {}
    for _ in range(30):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)
    return status


def _write_stage1_response(path: Path, scene_summary: str = "安德鲁正在说话。") -> None:
    path.write_text(
        json.dumps(
            {
                "context": {
                    "scene_summary": scene_summary,
                    "active_characters": ["安德鲁"],
                    "aliases_observed": [],
                    "current_emotional_state": {},
                    "unresolved_pronouns": [],
                    "important_context": ["开场对话。"],
                    "confidence": 0.92,
                    "review_notes": [],
                },
                "character_registry_updates": [
                    {
                        "character_id": "character_001",
                        "canonical_name": "安德鲁",
                        "stable_aliases": ["安德鲁"],
                        "contextual_references": [],
                        "alias_evidence": [],
                        "persona_summary": "平静。",
                        "speaking_style": "礼貌。",
                        "age_impression": None,
                        "voice_variant_notes": [],
                        "confidence": 0.95,
                        "review_notes": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_stage2_response(path: Path, source_text: str) -> None:
    path.write_text(
        json.dumps(
            {
                "segments": [
                    {"script": {"narrator": source_text}, "confidence": 0.99},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_web_template_keeps_actions_panel_owned() -> None:
    template = Path("ui/web/templates/index.html").read_text(encoding="utf-8")
    script = Path("ui/web/static/app.js").read_text(encoding="utf-8")

    assert 'id="chunk-button"' not in template
    assert 'id="chunk-select"' not in template
    assert 'id="stage2-button"' not in template
    assert 'id="source-select"' in template
    assert 'id="project-id"' in template
    assert "chunk it" in script
    assert "overview chunks" in script
    assert "feed to LLM" in script
    assert 'all.textContent = "all"' in script


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

    options = client.get("/api/projects/fixture_project/artifact-options").json()
    assert [option["id"] for option in options["views"]] == [
        "original_text",
        "chunks",
        "scene_summary",
        "character_summary",
        "scripts",
        "voice_assignment",
    ]

    original_view = client.get(
        "/api/projects/fixture_project/views/original_text",
        params={"source_path": sources[0]["path"]},
    ).json()
    assert original_view["available"] is True
    assert original_view["source"]["text"] == "第一段。\nSecond paragraph.\n"

    chunks_view = client.get("/api/projects/fixture_project/views/chunks").json()
    assert chunks_view["available"] is True
    assert chunks_view["chunks"][0]["chunk_id"] == "chunk_0001"

    empty_context = client.get(
        "/api/projects/fixture_project/views/scene_summary"
    ).json()
    assert empty_context["available"] is False


def test_web_stage1_overview_job_processes_all_chunks(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "tiny.txt"
    source.write_text("他说，“你好。”\n她点头。", encoding="utf-8")
    response_dir = tmp_path / "stage1_responses"
    response_dir.mkdir()
    _write_stage1_response(response_dir / "chunk_0001_response.json")

    app = create_app(raw_dir=raw_dir, workspace_root=tmp_path / "interim")
    client = TestClient(app)
    client.post(
        "/api/chunk",
        json={"source_path": str(source), "project_id": "fixture_project"},
    )

    job = client.post(
        "/api/stage1/jobs",
        json={
            "project_id": "fixture_project",
            "response_dir": str(response_dir),
        },
    ).json()
    status = _wait_for_job(client, job["job_id"])

    assert status["phase"] == "stage1"
    assert status["status"] == "complete"
    assert status["completed_chunks"] == 1

    scene = client.get("/api/projects/fixture_project/views/scene_summary").json()
    assert scene["available"] is True
    assert scene["sections"][0]["scene_summary"] == "安德鲁正在说话。"

    characters = client.get(
        "/api/projects/fixture_project/views/character_summary"
    ).json()
    assert characters["characters"][0]["canonical_name"] == "安德鲁"


def test_web_stage2_selected_chunk_job_and_script_endpoint(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "tiny.txt"
    source_text = "他说，“你好。”\n她点头。"
    source.write_text(source_text, encoding="utf-8")
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
            "selection": "chunk_0001",
            "response_path": str(response),
        },
    ).json()
    status = _wait_for_job(client, job["job_id"])

    assert status["status"] == "complete"
    assert status["completed_chunks"] == 1

    script = client.get("/api/projects/fixture_project/script/chunk_0001").json()
    assert script["validation_report"]["exact_reconstruction_success"] is True
    assert [segment["validation_status"] for segment in script["segments"]] == [
        "passed",
        "passed",
        "passed",
    ]


def test_web_stage2_all_job_assembles_continuous_script(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "tiny.txt"
    source_text = "他说你好。"
    source.write_text(source_text, encoding="utf-8")
    response_dir = tmp_path / "stage2_responses"
    response_dir.mkdir()
    _write_stage2_response(response_dir / "chunk_0001_response.json", source_text)
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
            "selection": "all",
            "response_dir": str(response_dir),
        },
    ).json()
    status = _wait_for_job(client, job["job_id"])

    assert status["phase"] == "stage2"
    assert status["status"] == "complete"
    assert status["completed_chunks"] == 1
    assert status["artifact_path"].endswith("complete_script.json")

    scripts = client.get("/api/projects/fixture_project/views/scripts").json()
    assert scripts["script_source"] == "continuous"
    assert scripts["selected_chunk_id"] == "complete"
    assert scripts["segments"][0]["text"] == source_text


def test_web_voice_assignment_view_preview_and_audio_job(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "tiny.txt"
    source_text = "他说你好。"
    source.write_text(source_text, encoding="utf-8")
    response_dir = tmp_path / "stage2_responses"
    response_dir.mkdir()
    _write_stage2_response(response_dir / "chunk_0001_response.json", source_text)
    prompt = tmp_path / "voice.pt"
    prompt.write_bytes(b"prompt")
    inventory_path = tmp_path / "voices" / "voice_profiles.json"
    write_json(
        inventory_path,
        VoiceInventoryArtifact(
            created_at=datetime.now(timezone.utc),
            voice_root=str(inventory_path.parent),
            profiles=[
                VoiceProfile(
                    profile_id="voice_a",
                    display_name="Voice A",
                    prompt_path=str(prompt),
                    prompt_sha256="hash",
                )
            ],
        ),
    )

    app = create_app(
        raw_dir=raw_dir,
        workspace_root=tmp_path / "interim",
        voice_inventory_path=inventory_path,
        tts_adapter_name="dummy",
    )
    client = TestClient(app)
    client.post(
        "/api/chunk",
        json={"source_path": str(source), "project_id": "fixture_project"},
    )
    job = client.post(
        "/api/stage2/jobs",
        json={
            "project_id": "fixture_project",
            "selection": "all",
            "response_dir": str(response_dir),
        },
    ).json()
    assert _wait_for_job(client, job["job_id"])["status"] == "complete"

    view = client.get("/api/projects/fixture_project/views/voice_assignment").json()
    assert view["available"] is True
    assert view["assignments"][0]["speaker"] == "narrator"
    assert view["assignments"][0]["representative_text"] == "他说你好。"

    alias_view = client.get(
        "/api/projects/fixture_project/views/voice-assignment"
    ).json()
    assert alias_view["view_type"] == "voice_assignment"
    assert alias_view["available"] is True

    sample = client.post(
        "/api/projects/fixture_project/voice-samples",
        json={"speaker": "narrator", "voice_profile_id": "voice_a"},
    ).json()
    assert sample["sample_url"].endswith(".wav")
    assert client.get(sample["sample_url"]).status_code == 200

    audio_job = client.post(
        "/api/projects/fixture_project/audio/jobs",
        json={"assignments": {"narrator": "voice_a"}, "only_missing": True},
    ).json()
    status = _wait_for_job(client, audio_job["job_id"])
    assert status["phase"] == "tts"
    assert status["status"] == "complete"
    assert status["completed_segments"] == 1


def test_web_qwen_generation_is_disabled_until_cli_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ui.web.app.qwen_delete_readiness_report",
        lambda **_: {
            "safe_to_delete_qwen_folders": False,
            "notes": ["CLI smoke pending"],
        },
    )
    app = create_app(raw_dir=tmp_path / "raw", workspace_root=tmp_path / "interim")
    client = TestClient(app)

    sample_response = client.post(
        "/api/projects/fixture_project/voice-samples",
        json={"speaker": "narrator", "voice_profile_id": "voice_a"},
    )
    audio_response = client.post(
        "/api/projects/fixture_project/audio/jobs",
        json={"assignments": {"narrator": "voice_a"}, "only_missing": True},
    )

    assert sample_response.status_code == 409
    assert audio_response.status_code == 409
    assert "CLI smoke tests pass" in sample_response.json()["detail"]


def test_webapp_does_not_use_dummy_tts_from_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NARRARE_TTS_ADAPTER", "dummy")
    app = create_app(raw_dir=tmp_path / "raw", workspace_root=tmp_path / "interim")
    assert app.state.tts_adapter_name is None

    from ui.web.app import _app_tts_adapter

    assert _app_tts_adapter(app).adapter_name == "qwen"


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


def test_panel_views_return_context_characters_and_scripts(tmp_path: Path) -> None:
    app = create_app(raw_dir=tmp_path / "raw", workspace_root=tmp_path / "interim")
    client = TestClient(app)
    workspace = Workspace("fixture_project", root=tmp_path / "interim")
    workspace.ensure()
    chunk_path = workspace.chunk_text_path(0)
    chunk_path.write_text("他说你好", encoding="utf-8")
    write_json(
        workspace.context_artifact_path("chunk_0001"),
        {
            "project_id": "fixture_project",
            "chunk_id": "chunk_0001",
            "llm_provider": "test",
            "llm_model": "test",
            "response_source": "response_path",
            "context": {
                "scene_summary": "安德鲁说话。",
                "active_characters": ["安德鲁"],
                "aliases_observed": [],
                "current_emotional_state": {},
                "unresolved_pronouns": [],
                "important_context": ["开场对话。"],
                "confidence": 0.9,
                "review_notes": [],
            },
            "character_registry_updates": [],
        },
    )
    write_json(
        workspace.character_registry_path,
        {
            "project_id": "fixture_project",
            "characters": [
                {
                    "character_id": "character_001",
                    "canonical_name": "安德鲁",
                    "stable_aliases": ["安德鲁"],
                    "contextual_references": [],
                    "aliases": [],
                    "alias_evidence": [],
                    "persona_summary": "平静。",
                    "speaking_style": "礼貌。",
                    "age_impression": None,
                    "voice_variant_notes": [],
                    "confidence": 0.9,
                    "review_notes": [],
                }
            ],
        },
    )
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
            "processed_chunk_count": 1,
            "segments": [
                {
                    "segment_id": "seg_000001",
                    "source_span": {"start": 0, "end": 4},
                    "script": {"安德鲁": "他说你好"},
                    "raw_script_key": None,
                    "speaker_key_normalization": None,
                    "confidence": 0.9,
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
            "exact_reconstruction_success": True,
            "segment_count": 1,
            "source_character_count": 4,
            "reconstructed_character_count": 4,
            "source_hash": "unused",
            "reconstructed_hash": "unused",
            "errors": [],
        },
    )

    scene = client.get("/api/projects/fixture_project/views/scene_summary").json()
    assert scene["available"] is True
    assert scene["sections"][0]["scene_summary"] == "安德鲁说话。"

    characters = client.get(
        "/api/projects/fixture_project/views/character_summary"
    ).json()
    assert characters["characters"][0]["canonical_name"] == "安德鲁"

    scripts = client.get("/api/projects/fixture_project/views/scripts").json()
    assert scripts["script_source"] == "single_chunk"
    assert scripts["segments"][0]["validation_status"] == "passed"
