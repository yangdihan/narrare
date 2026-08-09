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
    assert 'id="global-job-progress"' in template
    assert 'id="generate-all-button"' in template
    assert 'id="play-all-button"' in template
    assert 'id="global-audio-player"' in template
    assert "chunk it" in script
    assert "overview chunks" in script
    assert "feed to LLM" in script
    assert "auto unify characters" in script
    assert "generate sample" in script
    assert "Rename" in script
    assert "save edit" in script
    assert "Original sample" in script
    assert "Generated sample" in script
    assert "sample-button" in script
    assert 'all.textContent = "all"' in script
    assert "/audio/jobs/active" in script
    assert "resumeActiveTtsJob" in script
    assert "narrare.projectId" in script
    assert "/audio/playlist" in script
    assert "startGlobalAudioGeneration" in script
    assert "startGlobalPlayback" in script
    assert "characterAudioGenerationFooter" not in script


def test_web_reports_active_tts_job_for_page_reload(tmp_path: Path) -> None:
    app = create_app(raw_dir=tmp_path / "raw", workspace_root=tmp_path / "interim")
    client = TestClient(app)
    job = app.state.jobs.create("fixture_project", "tts", "all")

    def mark_running(active_job) -> None:
        active_job.status = "running"
        active_job.total_chunks = 12
        active_job.completed_chunks = 5
        active_job.current_chunk_id = "seg_000006"

    app.state.jobs.update(job.job_id, mark_running)
    active = client.get(
        "/api/projects/fixture_project/audio/jobs/active"
    ).json()["job"]
    assert active["job_id"] == job.job_id
    assert active["status"] == "running"
    assert active["total_segments"] == 12
    assert active["completed_segments"] == 5

    app.state.jobs.update(
        job.job_id,
        lambda completed_job: setattr(completed_job, "status", "complete"),
    )
    assert client.get(
        "/api/projects/fixture_project/audio/jobs/active"
    ).json() == {"job": None}


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
        "characters",
        "scripts",
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


def test_web_stage3_auto_unify_characters_job(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "tiny.txt"
    source.write_text("你好", encoding="utf-8")
    app = create_app(raw_dir=raw_dir, workspace_root=tmp_path / "interim")
    client = TestClient(app)
    client.post(
        "/api/chunk",
        json={"source_path": str(source), "project_id": "fixture_project"},
    )
    workspace = Workspace("fixture_project", root=tmp_path / "interim")
    write_json(
        workspace.character_registry_path,
        {
            "project_id": "fixture_project",
            "characters": [
                {
                    "character_id": "character_001",
                    "canonical_name": "安德鲁·马丁",
                    "stable_aliases": ["安德鲁"],
                    "contextual_references": [],
                    "aliases": ["安德鲁"],
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
    )
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
                "active_characters": ["安德鲁·马丁"],
                "aliases_observed": [
                    {
                        "text": "安德鲁",
                        "reference_type": "stable_name",
                        "likely_character_id": "character_001",
                        "confidence": 0.99,
                        "review_notes": [],
                    }
                ],
                "current_emotional_state": {},
                "unresolved_pronouns": [],
                "important_context": [],
                "confidence": 0.95,
                "review_notes": [],
            },
            "character_registry_updates": [],
        },
    )
    write_json(
        workspace.script_artifact_path("complete"),
        {
            "project_id": "fixture_project",
            "chunk_id": "complete",
            "chunk_source_path": str(workspace.chunks_path),
            "chunk_sha256": "unused",
            "llm_provider": "test",
            "llm_model": "test",
            "response_source": "assembled",
            "processed_chunk_count": 1,
            "segments": [
                {
                    "segment_id": "seg_000001",
                    "source_span": {"start": 0, "end": 2},
                    "script": {"安德鲁": "你好"},
                    "confidence": 0.9,
                    "review_notes": [],
                }
            ],
        },
    )
    response_dir = tmp_path / "stage3_responses"
    response_dir.mkdir()
    (response_dir / "seg_000001_response.json").write_text(
        json.dumps(
            {
                "segment_id": "seg_000001",
                "current_key": "安德鲁",
                "decision": "replace",
                "replacement_key": "安德鲁·马丁",
                "confidence": 0.99,
                "evidence": ["Unique stable alias match."],
                "review_notes": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    scripts_before = client.get(
        "/api/projects/fixture_project/views/scripts"
    ).json()
    assert scripts_before["stage3_enabled"] is True

    job = client.post(
        "/api/stage3/jobs",
        json={
            "project_id": "fixture_project",
            "response_dir": str(response_dir),
        },
    ).json()
    status = _wait_for_job(client, job["job_id"])

    assert status["phase"] == "stage3"
    assert status["status"] == "complete"
    assert status["completed_segments"] == 1
    assert status["artifact_path"].endswith("complete_key_reviewed_script.json")
    scripts_after = client.get(
        "/api/projects/fixture_project/views/scripts"
    ).json()
    assert scripts_after["segments"][0]["speaker"] == "安德鲁·马丁"


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
    original_sample = tmp_path / "voices" / "voice_a.m4a"
    original_sample.parent.mkdir()
    original_sample.write_bytes(b"original sample")
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
                    sample_path="voice_a.m4a",
                    sample_sha256="sample-hash",
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

    view = client.get("/api/projects/fixture_project/views/characters").json()
    assert view["available"] is True
    assert view["assignments"][0]["speaker"] == "narrator"
    assert view["assignments"][0]["representative_text"] == "他说你好。"
    assert view["voice_profiles"][0]["sample_url"] == (
        "/api/voice-profiles/voice_a/sample"
    )
    assert client.get(view["voice_profiles"][0]["sample_url"]).status_code == 200

    alias_view = client.get(
        "/api/projects/fixture_project/views/voice-assignment"
    ).json()
    assert alias_view["view_type"] == "characters"
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

    scripts = client.get("/api/projects/fixture_project/views/scripts").json()
    takes = scripts["segments"][0]["audio_takes"]
    assert [take["take_number"] for take in takes] == [1]
    assert takes[0]["selected"] is True
    assert client.get(takes[0]["audio_url"]).status_code == 200

    single_take_job = client.post(
        "/api/projects/fixture_project/audio/segment-jobs",
        json={"segment_id": scripts["segments"][0]["segment_id"]},
    ).json()
    single_take_status = _wait_for_job(client, single_take_job["job_id"])
    assert single_take_status["status"] == "complete"
    assert single_take_status["completed_segments"] == 1

    scripts = client.get("/api/projects/fixture_project/views/scripts").json()
    takes = scripts["segments"][0]["audio_takes"]
    assert [take["take_number"] for take in takes] == [1, 2]
    assert takes[1]["selected"] is True
    selection = client.post(
        f"/api/projects/fixture_project/audio-takes/{scripts['segments'][0]['segment_id']}/select",
        json={"take_number": 1},
    ).json()
    assert selection["selected_take_number"] == 1
    scripts = client.get("/api/projects/fixture_project/views/scripts").json()
    assert scripts["segments"][0]["audio_takes"][0]["selected"] is True

    original_segment = scripts["segments"][0]
    split_at = max(1, len(original_segment["text"]) // 2)
    edited = client.post(
        "/api/projects/fixture_project/script-edits",
        json={
            "updates": [
                {
                    "segment_id": original_segment["segment_id"],
                    "speaker": original_segment["speaker"],
                    "text": original_segment["text"][:split_at],
                    "chunk_id": scripts["selected_chunk_id"],
                }
            ],
            "inserts": [
                {
                    "after_segment_id": original_segment["segment_id"],
                    "speaker": original_segment["speaker"],
                    "text": original_segment["text"][split_at:],
                    "chunk_id": scripts["selected_chunk_id"],
                }
            ],
        },
    ).json()
    assert len(edited["segments"]) == 2
    assert edited["segments"][0]["audio_takes"] == []
    assert edited["segments"][0]["stale_audio_take_count"] == 2
    assert edited["segments"][1]["audio_takes"] == []

    regeneration_job = client.post(
        "/api/projects/fixture_project/audio/jobs",
        json={"assignments": {"narrator": "voice_a"}, "only_missing": True},
    ).json()
    regeneration_status = _wait_for_job(client, regeneration_job["job_id"])
    assert regeneration_status["status"] == "complete"
    assert regeneration_status["completed_segments"] == 2

    regenerated = client.get("/api/projects/fixture_project/views/scripts").json()
    assert [
        [take["take_number"] for take in segment["audio_takes"]]
        for segment in regenerated["segments"]
    ] == [[3], [1]]
    assert regenerated["segments"][0]["stale_audio_take_count"] == 2

    playlist = client.get(
        "/api/projects/fixture_project/audio/playlist"
    ).json()
    assert playlist["ready"] is True
    assert [item["segment_id"] for item in playlist["items"]] == [
        segment["segment_id"] for segment in regenerated["segments"]
    ]
    assert [item["take_number"] for item in playlist["items"]] == [3, 1]
    assert playlist["missing_segment_ids"] == []


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
                    "confirmed": True,
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
    assert scripts["speaker_options"] == ["narrator", "unknown_speaker", "安德鲁"]

    characters = client.post(
        "/api/projects/fixture_project/characters",
        json={"name": "法官"},
    ).json()
    assert [character["canonical_name"] for character in characters["characters"]] == [
        "安德鲁",
        "法官",
    ]

    scripts = client.post(
        "/api/projects/fixture_project/script-speaker-edits",
        json={
            "edits": [
                {
                    "segment_id": "seg_000001",
                    "speaker": "法官",
                    "chunk_id": "chunk_0001",
                }
            ]
        },
    ).json()
    assert scripts["segments"][0]["speaker"] == "法官"

    characters = client.post(
        "/api/projects/fixture_project/characters/merge",
        json={
            "source_character_id": "character_002",
            "target_character_id": "character_001",
        },
    ).json()
    assert [character["canonical_name"] for character in characters["characters"]] == [
        "安德鲁",
    ]
    scripts = client.get("/api/projects/fixture_project/views/scripts").json()
    assert scripts["segments"][0]["speaker"] == "安德鲁"

    characters = client.post(
        "/api/projects/fixture_project/character-edits",
        json={
            "additions": ["世界总统"],
            "renames": {"character_001": "安德鲁·马丁"},
            "merges": [],
        },
    ).json()
    assert [character["canonical_name"] for character in characters["characters"]] == [
        "安德鲁·马丁",
        "世界总统",
    ]
    scripts = client.get("/api/projects/fixture_project/views/scripts").json()
    assert scripts["segments"][0]["speaker"] == "安德鲁·马丁"
    scene = client.get("/api/projects/fixture_project/views/scene_summary").json()
    assert scene["sections"][0]["active_characters"] == ["安德鲁·马丁"]


def test_characters_curation_endpoint_removes_character_confirmation_gate(
    tmp_path: Path,
) -> None:
    app = create_app(raw_dir=tmp_path / "raw", workspace_root=tmp_path / "interim")
    client = TestClient(app)
    workspace = Workspace("fixture_project", root=tmp_path / "interim")
    workspace.ensure()
    write_json(
        workspace.chunks_path,
        {"chunks": [{"chunk_id": "chunk_0001", "text": "abcd"}]},
    )
    write_json(
        workspace.character_registry_path,
        {
            "project_id": "fixture_project",
            "characters": [
                {
                    "character_id": "character_001",
                    "canonical_name": "Alice",
                    "stable_aliases": ["Alice", "Dr. A"],
                    "confidence": 0.6,
                    "confirmed": False,
                },
                {
                    "character_id": "character_002",
                    "canonical_name": "Bob",
                    "stable_aliases": ["Bob", "Dr. A"],
                    "confidence": 0.9,
                    "confirmed": True,
                },
            ],
        },
    )
    write_json(
        workspace.context_artifact_path("chunk_0001"),
        {
            "chunk_id": "chunk_0001",
            "context": {
                "active_characters": ["Alice", "Bob"],
                "aliases_observed": [],
                "unresolved_pronouns": [{"text": "she", "candidates": ["Alice", "Bob"]}],
            },
        },
    )
    write_json(
        workspace.script_artifact_path("complete"),
        {
            "project_id": "fixture_project",
            "chunk_id": "complete",
            "chunk_source_path": str(workspace.chunks_path),
            "chunk_sha256": "unused",
            "llm_provider": "test",
            "llm_model": "test",
            "response_source": "assembled",
            "processed_chunk_count": 1,
            "segments": [
                {
                    "segment_id": "seg_000001",
                    "source_span": {"start": 0, "end": 2},
                    "script": {"Alice": "ab"},
                    "confidence": 0.9,
                },
                {
                    "segment_id": "seg_000002",
                    "source_span": {"start": 2, "end": 4},
                    "script": {"AliasKey": "cd"},
                    "confidence": 0.9,
                },
            ],
        },
    )

    before = client.get("/api/projects/fixture_project/views/characters").json()
    assert before["view_type"] == "characters"
    assert before["review"]["conflicts"][0]["alias"] == "Dr. A"
    assert "unresolved" not in before["review"]
    assert before["script_speaker_keys"] == ["Alice", "AliasKey"]

    response = client.post(
        "/api/projects/fixture_project/characters/curation",
        json={
            "updates": [
                {
                    "character_id": "character_001",
                    "canonical_name": "Alice",
                    "stable_aliases": ["Alice", "Dr. A"],
                    "persona_summary": "Reviewed protagonist",
                    "speaking_style": None,
                    "age_impression": None,
                    "voice_variant_notes": [],
                }
            ],
            "removals": ["character_002"],
            "script_speaker_merges": [
                {
                    "source_speaker": "AliasKey",
                    "target_character_id": "character_001",
                }
            ],
            "voice_profile_by_character_id": {"character_001": "voice_a"},
            "system_voice_assignments": {"narrator": "voice_narrator"},
        },
    )
    assert response.status_code == 200
    assert [character["canonical_name"] for character in response.json()["characters"]] == [
        "Alice"
    ]
    assert "confirmed" not in response.json()["characters"][0]

    scripts = client.get("/api/projects/fixture_project/views/scripts").json()
    assert scripts["speaker_options"] == ["narrator", "unknown_speaker", "Alice"]
    assert scripts["speaker_filter_options"] == ["Alice"]
    assert [segment["speaker"] for segment in scripts["segments"]] == ["Alice", "Alice"]
    assignments = client.get("/api/projects/fixture_project/views/characters").json()[
        "assignments"
    ]
    by_speaker = {assignment["speaker"]: assignment for assignment in assignments}
    assert by_speaker["Alice"]["voice_profile_id"] == "voice_a"
    assert by_speaker["narrator"]["voice_profile_id"] == "voice_narrator"
    assert by_speaker["narrator"]["representative_text"]
