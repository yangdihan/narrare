from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.source import SourceSpan
from core.models.voice import VoiceInventoryArtifact, VoiceProfile
from core.pipeline.qwen_tts import (
    create_qwen_voice_prompt,
    generate_qwen_clip,
    qwen_delete_readiness_report,
)
from core.pipeline.voice_assets import import_qwen_voice_assets
from core.pipeline.voice_assignment import (
    build_voice_assignment_artifact,
    generate_voice_sample,
    run_audio_generation_workflow,
    save_voice_assignments,
)
from storage.json_store import write_json
from storage.workspace import Workspace
from tts.base import SynthesisRequest, SynthesisResult
from tts.dummy import DummyTTSAdapter
from tts.qwen.bootstrap import bootstrap_qwen_assets


def test_import_qwen_voice_assets_copies_prompts_and_matching_samples(
    tmp_path: Path,
) -> None:
    prompts = tmp_path / "qwen" / "voices"
    prompts.mkdir(parents=True)
    samples = tmp_path / "samples"
    samples.mkdir()
    (prompts / "m新闻播报.pt").write_bytes(b"prompt")
    (samples / "m新闻播报.wav").write_bytes(b"sample")

    result = import_qwen_voice_assets(
        prompt_source_dir=prompts,
        sample_source_dirs=[samples],
        voice_root=tmp_path / "data" / "voices" / "qwen",
    )

    assert len(result.profiles) == 1
    profile = result.profiles[0]
    assert profile.profile_id == "m新闻播报"
    assert Path(profile.prompt_path).read_bytes() == b"prompt"
    assert profile.sample_path is not None
    assert Path(profile.sample_path).read_bytes() == b"sample"


def test_qwen_bootstrap_copies_package_model_and_voice_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "Qwen3-Audiobook-Studio-v1.0-lite"
    package = (
        source
        / "runtime_env"
        / "lib"
        / "python3.10"
        / "site-packages"
        / "qwen_tts"
    )
    model = source / "models" / "Qwen3-TTS-12Hz-1.7B-Base"
    voices = source / "voices"
    package.mkdir(parents=True)
    model.mkdir(parents=True)
    voices.mkdir(parents=True)
    (package / "__init__.py").write_text("class Qwen3TTSModel: pass\n", encoding="utf-8")
    (model / "config.json").write_text("{}", encoding="utf-8")
    (voices / "teacher.pt").write_bytes(b"prompt")

    manifest = bootstrap_qwen_assets(source_root=source)

    assert Path("tts/qwen/vendor/qwen_tts/__init__.py").exists()
    assert Path("data/models/qwen/Qwen3-TTS-12Hz-1.7B-Base/config.json").exists()
    assert Path("data/voices/qwen/prompts/teacher.pt").exists()
    assert Path("data/models/qwen/bootstrap_manifest.json").exists()
    assert manifest.copied_package_files == 1
    assert manifest.copied_model_files == 1
    assert manifest.copied_voice_profiles == 1


def test_qwen_bootstrap_rejects_missing_package_or_model(tmp_path: Path) -> None:
    source = tmp_path / "Qwen3-Audiobook-Studio-v1.0-lite"
    source.mkdir()

    try:
        bootstrap_qwen_assets(source_root=source)
    except RuntimeError as exc:
        assert "qwen_tts package not found" in str(exc)
    else:
        raise AssertionError("bootstrap should reject a source without qwen_tts")


def test_create_qwen_voice_prompt_writes_pt_and_inventory(tmp_path: Path) -> None:
    sample = tmp_path / "sample.m4a"
    sample.write_bytes(b"audio")
    inventory = tmp_path / "voices" / "voice_profiles.json"

    result = create_qwen_voice_prompt(
        sample_path=sample,
        transcript="这是一段样本文本。",
        profile_id="teacher one",
        output_dir=tmp_path / "voices" / "prompts",
        voice_inventory_path=inventory,
        adapter=_PromptAdapter(),
    )

    assert result["profile_id"] == "teacher_one"
    assert Path(result["prompt_path"]).read_bytes() == b"prompt"
    artifact = VoiceInventoryArtifact.model_validate_json(
        inventory.read_text(encoding="utf-8")
    )
    assert [profile.profile_id for profile in artifact.profiles] == ["teacher_one"]


def test_generate_qwen_clip_resolves_voice_profile_and_writes_manifest(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / "voice.pt"
    prompt.write_bytes(b"prompt")
    inventory = tmp_path / "voices" / "voice_profiles.json"
    write_json(
        inventory,
        VoiceInventoryArtifact(
            created_at=datetime.now(timezone.utc),
            voice_root=str(inventory.parent),
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
    output = tmp_path / "clip.wav"

    result = generate_qwen_clip(
        text="只要不违背第一条规则或第二条规则，机器人必须保护它自身的生存。",
        voice_profile_id="voice_a",
        output_path=output,
        voice_inventory_path=inventory,
        adapter=_ClipAdapter(),
    )

    assert Path(result["output_path"]).read_bytes() == b"clip"
    manifest = Path(result["manifest_path"])
    assert manifest.exists()
    assert '"voice_profile_id": "voice_a"' in manifest.read_text(encoding="utf-8")


def test_qwen_delete_readiness_ignores_source_only_old_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("core.pipeline.qwen_tts.missing_qwen_dependencies", lambda: [])
    Path("tts/qwen/vendor/qwen_tts").mkdir(parents=True)
    Path("data/models/qwen/Qwen3-TTS-12Hz-1.7B-Base").mkdir(parents=True)
    prompt = Path("data/voices/qwen/prompts/teacher.pt")
    prompt.parent.mkdir(parents=True)
    prompt.write_bytes(b"prompt")
    write_json(
        Path("data/models/qwen/bootstrap_manifest.json"),
        {
            "source_root": "Qwen3-Audiobook-Studio-v1.0-lite",
            "model_path": "data/models/qwen/Qwen3-TTS-12Hz-1.7B-Base",
        },
    )
    write_json(
        Path("data/voices/qwen/voice_profiles.json"),
        VoiceInventoryArtifact(
            created_at=datetime.now(timezone.utc),
            voice_root="data/voices/qwen",
            profiles=[
                VoiceProfile(
                    profile_id="teacher",
                    display_name="Teacher",
                    prompt_path=str(prompt),
                    prompt_sha256="hash",
                    source_prompt_path="Qwen3-Audiobook-Studio-v1.0-lite/voices/teacher.pt",
                )
            ],
        ),
    )

    report = qwen_delete_readiness_report()

    assert report["old_path_references"] == []
    assert report["safe_to_delete_qwen_folders"] is True


class _PromptAdapter:
    def create_voice_prompt(
        self,
        *,
        sample_path: str | Path,
        transcript: str,
        output_path: str | Path,
        x_vector_only_mode: bool = False,
    ) -> Path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"prompt")
        return output


class _ClipAdapter:
    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"clip")
        return SynthesisResult(
            output_path=request.output_path,
            sample_rate=24000,
            adapter="fake_qwen",
            model_path=Path("data/models/qwen/Qwen3-TTS-12Hz-1.7B-Base"),
            parameters=request.parameters,
        )


def test_voice_assignment_selects_representative_text_and_generates_takes(
    tmp_path: Path,
) -> None:
    workspace = _write_voice_fixture(tmp_path)
    inventory_path = _write_inventory(tmp_path)

    artifact = build_voice_assignment_artifact(
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )
    speakers = [assignment.speaker for assignment in artifact.assignments]
    assert speakers == ["narrator", "安德鲁"]
    andrew = next(item for item in artifact.assignments if item.speaker == "安德鲁")
    assert andrew.representative_segment_id == "seg_000002"
    assert andrew.representative_text == "“你好，先生。”"

    generate_voice_sample(
        "fixture_project",
        "安德鲁",
        "voice_a",
        workspace_root=tmp_path / "interim",
        voice_inventory_path=inventory_path,
        adapter=DummyTTSAdapter(),
    )
    assert workspace.voice_sample_path("安德鲁").exists()

    save_voice_assignments(
        "fixture_project",
        {"narrator": "voice_a", "安德鲁": "voice_a"},
        workspace_root=tmp_path / "interim",
    )
    result = run_audio_generation_workflow(
        "fixture_project",
        workspace_root=tmp_path / "interim",
        voice_inventory_path=inventory_path,
        adapter=DummyTTSAdapter(),
    )
    assert result["generated_count"] == 3
    assert workspace.audio_take_path("seg_000001").exists()
    assert workspace.audio_take_manifest_path("seg_000003").exists()


def _write_voice_fixture(tmp_path: Path) -> Workspace:
    workspace = Workspace("fixture_project", root=tmp_path / "interim")
    workspace.ensure()
    write_json(
        workspace.script_artifact_path("complete"),
        ScriptArtifact(
            project_id="fixture_project",
            chunk_id="complete",
            chunk_source_path="source.txt",
            chunk_sha256="hash",
            llm_provider="test",
            llm_model="test",
            response_source="assembled",
            processed_chunk_count=1,
            segments=[
                ScriptSegment(
                    segment_id="seg_000001",
                    source_span=SourceSpan(start=0, end=2),
                    script={"narrator": "他说"},
                    confidence=0.9,
                ),
                ScriptSegment(
                    segment_id="seg_000002",
                    source_span=SourceSpan(start=2, end=9),
                    script={"安德鲁": "“你好，先生。”"},
                    confidence=0.9,
                ),
                ScriptSegment(
                    segment_id="seg_000003",
                    source_span=SourceSpan(start=9, end=13),
                    script={"narrator": "他点头。"},
                    confidence=0.9,
                ),
            ],
        ),
    )
    write_json(
        workspace.character_registry_path,
        {
            "project_id": "fixture_project",
            "characters": [
                {
                    "character_id": "character_001",
                    "canonical_name": "安德鲁",
                    "stable_aliases": [],
                    "persona_summary": "礼貌的机器人。",
                    "speaking_style": "措辞克制。",
                    "age_impression": None,
                    "voice_variant_notes": [],
                    "confidence": 0.9,
                    "contextual_references": [],
                }
            ],
        },
    )
    return workspace


def _write_inventory(tmp_path: Path) -> Path:
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
    return inventory_path
