from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.models.voice import AudioTakeManifest, VoiceInventoryArtifact, VoiceProfile
from core.pipeline.voice_assets import load_voice_inventory
from storage.json_store import write_json
from tts.base import SynthesisRequest
from tts.qwen.adapter import QwenTTSAdapter, qwen_status
from tts.qwen.bootstrap import bootstrap_qwen_assets, missing_qwen_dependencies
from tts.qwen.paths import (
    OLD_QWEN_FOLDER_MARKER,
    QWEN_BOOTSTRAP_MANIFEST_PATH,
    QWEN_DEFAULT_MODEL_PATH,
    QWEN_VENDOR_PACKAGE,
)


def run_qwen_bootstrap_workflow(
    *,
    source: str | Path,
    model: str,
) -> dict[str, object]:
    manifest = bootstrap_qwen_assets(source_root=source, model_id=model)
    return {
        "manifest": manifest,
        "manifest_path": str(QWEN_BOOTSTRAP_MANIFEST_PATH),
        "status": qwen_status(),
    }


def create_qwen_voice_prompt(
    *,
    sample_path: str | Path,
    transcript: str,
    profile_id: str,
    output_dir: str | Path = "data/voices/qwen/prompts",
    voice_inventory_path: str | Path = "data/voices/qwen/voice_profiles.json",
    adapter: QwenTTSAdapter | None = None,
) -> dict[str, object]:
    output_path = Path(output_dir) / f"{_safe_id(profile_id)}.pt"
    adapter = adapter or QwenTTSAdapter()
    prompt_path = adapter.create_voice_prompt(
        sample_path=sample_path,
        transcript=transcript,
        output_path=output_path,
    )
    artifact = _upsert_voice_profile(
        voice_inventory_path=voice_inventory_path,
        profile=VoiceProfile(
            profile_id=_safe_id(profile_id),
            display_name=profile_id,
            prompt_path=str(prompt_path),
            prompt_sha256=_file_sha256(prompt_path),
            sample_path=str(sample_path),
            sample_sha256=_file_sha256(Path(sample_path)),
        ),
    )
    return {
        "profile_id": _safe_id(profile_id),
        "prompt_path": str(prompt_path),
        "voice_inventory_path": str(voice_inventory_path),
        "profile_count": len(artifact.profiles),
    }


def generate_qwen_clip(
    *,
    text: str,
    voice_profile_id: str,
    output_path: str | Path,
    language: str = "Chinese",
    device: str = "auto",
    voice_inventory_path: str | Path = "data/voices/qwen/voice_profiles.json",
    adapter: QwenTTSAdapter | None = None,
) -> dict[str, object]:
    profile = _voice_profile(voice_inventory_path, voice_profile_id)
    adapter = adapter or QwenTTSAdapter(device=device)
    result = adapter.synthesize(
        SynthesisRequest(
            text=text,
            voice_prompt_path=Path(profile.prompt_path),
            output_path=Path(output_path),
            language=language,
        )
    )
    manifest_path = Path(output_path).with_suffix(Path(output_path).suffix + ".json")
    write_json(
        manifest_path,
        AudioTakeManifest(
            project_id="qwen_smoke",
            segment_id=Path(output_path).stem,
            speaker=voice_profile_id,
            text=text,
            voice_profile_id=profile.profile_id,
            voice_prompt_path=profile.prompt_path,
            script_artifact_path="manual",
            adapter=result.adapter,
            model_path=str(result.model_path) if result.model_path else None,
            parameters=result.parameters,
            output_path=str(result.output_path),
            created_at=datetime.now(timezone.utc),
        ),
    )
    return {
        "output_path": str(result.output_path),
        "manifest_path": str(manifest_path),
        "sample_rate": result.sample_rate,
    }


def qwen_delete_readiness_report(
    *,
    voice_inventory_path: str | Path = "data/voices/qwen/voice_profiles.json",
) -> dict[str, object]:
    status = qwen_status()
    report: dict[str, object] = {
        **status,
        "bootstrap_manifest_exists": QWEN_BOOTSTRAP_MANIFEST_PATH.exists(),
        "voice_inventory_exists": Path(voice_inventory_path).exists(),
        "missing_prompt_files": [],
        "missing_dependencies": missing_qwen_dependencies(),
        "old_path_references": [],
        "safe_to_delete_qwen_folders": False,
        "notes": [],
    }
    if Path(voice_inventory_path).exists():
        inventory = load_voice_inventory(voice_inventory_path)
        report["missing_prompt_files"] = [
            profile.prompt_path
            for profile in inventory.profiles
            if not Path(profile.prompt_path).exists()
        ]
    old_refs = _old_path_references()
    notes = []
    if old_refs:
        notes.append("Old Qwen folder references are still present.")
    if report["missing_dependencies"]:
        notes.append("Python dependencies are missing from Narrare .venv.")
    if report["missing_prompt_files"]:
        notes.append("Some copied voice prompt files are missing.")
    if not report["bootstrap_manifest_exists"]:
        notes.append("Qwen bootstrap manifest is missing.")
    if not status["vendor_package_exists"]:
        notes.append("Vendored qwen_tts package is missing.")
    if not status["model_exists"]:
        notes.append("Narrare-owned Qwen model path is missing.")
    report["old_path_references"] = old_refs
    report["notes"] = notes
    report["safe_to_delete_qwen_folders"] = (
        status["vendor_package_exists"]
        and status["model_exists"]
        and report["bootstrap_manifest_exists"]
        and report["voice_inventory_exists"]
        and not report["missing_prompt_files"]
        and not report["missing_dependencies"]
        and not old_refs
    )
    return report


def _voice_profile(voice_inventory_path: str | Path, profile_id: str) -> VoiceProfile:
    inventory = load_voice_inventory(voice_inventory_path)
    for profile in inventory.profiles:
        if profile.profile_id == profile_id:
            return profile
    raise RuntimeError(f"voice profile not found: {profile_id}")


def _upsert_voice_profile(
    *,
    voice_inventory_path: str | Path,
    profile: VoiceProfile,
) -> VoiceInventoryArtifact:
    path = Path(voice_inventory_path)
    if path.exists():
        artifact = load_voice_inventory(path)
        profiles = [item for item in artifact.profiles if item.profile_id != profile.profile_id]
        artifact = artifact.model_copy(
            update={
                "created_at": artifact.created_at,
                "profiles": [*profiles, profile],
            }
        )
    else:
        artifact = VoiceInventoryArtifact(
            created_at=datetime.now(timezone.utc),
            voice_root=str(path.parent),
            profiles=[profile],
        )
    artifact = artifact.model_copy(
        update={"profiles": sorted(artifact.profiles, key=lambda item: item.profile_id)}
    )
    write_json(path, artifact)
    return artifact


def _old_path_references() -> list[str]:
    refs = []
    for item in [
        *sys.path,
        str(QWEN_DEFAULT_MODEL_PATH),
        str(QWEN_VENDOR_PACKAGE),
    ]:
        if OLD_QWEN_FOLDER_MARKER in item:
            refs.append(item)
    for path in [
        QWEN_BOOTSTRAP_MANIFEST_PATH,
        Path("data/voices/qwen/voice_profiles.json"),
    ]:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for key_path, reference in _walk_strings(data):
            if OLD_QWEN_FOLDER_MARKER in reference and not _is_source_only_reference(
                key_path
            ):
                refs.append(f"{path}: {reference}")
    return sorted(set(refs))


def _walk_strings(value: object, key_path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    if isinstance(value, str):
        return [(key_path, value)]
    if isinstance(value, dict):
        return [
            item
            for key, child in value.items()
            for item in _walk_strings(child, (*key_path, str(key)))
        ]
    if isinstance(value, list):
        return [
            item
            for index, child in enumerate(value)
            for item in _walk_strings(child, (*key_path, str(index)))
        ]
    return []


def _is_source_only_reference(key_path: tuple[str, ...]) -> bool:
    source_only_keys = {
        "source_root",
        "source_only_metadata",
        "source_prompt_path",
        "source_sample_path",
    }
    return any(key in source_only_keys for key in key_path)


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_id(value: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in value.strip()
    )
    return cleaned.strip("_") or "voice"
