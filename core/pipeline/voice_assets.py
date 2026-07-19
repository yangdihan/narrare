from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.models.voice import VoiceInventoryArtifact, VoiceProfile
from storage.json_store import write_json

VOICE_ROOT = Path("data/voices/qwen")
VOICE_INVENTORY_PATH = VOICE_ROOT / "voice_profiles.json"
AUDIO_SUFFIXES = {".wav", ".m4a", ".mp3", ".flac", ".ogg"}


def import_qwen_voice_assets(
    *,
    prompt_source_dir: str | Path,
    sample_source_dirs: list[str | Path] | None = None,
    voice_root: str | Path = VOICE_ROOT,
) -> VoiceInventoryArtifact:
    prompt_source = Path(prompt_source_dir)
    if not prompt_source.exists():
        raise RuntimeError(f"voice prompt source not found: {prompt_source}")

    root = Path(voice_root)
    prompt_dest = root / "prompts"
    sample_dest = root / "samples"
    prompt_dest.mkdir(parents=True, exist_ok=True)
    sample_dest.mkdir(parents=True, exist_ok=True)

    copied_samples = _copy_sample_files(sample_source_dirs or [Path("data/voices")], sample_dest)
    profiles = []
    for prompt_path in sorted(prompt_source.glob("*.pt")):
        copied_prompt = prompt_dest / prompt_path.name
        if prompt_path.resolve() != copied_prompt.resolve():
            shutil.copy2(prompt_path, copied_prompt)
        sample_path = _matching_sample(copied_samples, prompt_path.stem)
        profiles.append(
            VoiceProfile(
                profile_id=_safe_id(prompt_path.stem),
                display_name=prompt_path.stem,
                prompt_path=str(copied_prompt),
                prompt_sha256=_file_sha256(copied_prompt),
                sample_path=str(sample_path) if sample_path is not None else None,
                sample_sha256=_file_sha256(sample_path) if sample_path is not None else None,
                source_prompt_path=str(prompt_path),
                source_sample_path=(
                    str(_source_sample_for(copied_samples, sample_path))
                    if sample_path is not None
                    else None
                ),
            )
        )

    artifact = VoiceInventoryArtifact(
        created_at=datetime.now(timezone.utc),
        voice_root=str(root),
        profiles=profiles,
    )
    write_json(root / "voice_profiles.json", artifact)
    return artifact


def load_voice_inventory(
    voice_inventory_path: str | Path = VOICE_INVENTORY_PATH,
) -> VoiceInventoryArtifact:
    path = Path(voice_inventory_path)
    if not path.exists():
        raise RuntimeError(f"voice inventory not found: {path}")
    return VoiceInventoryArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _copy_sample_files(
    source_dirs: list[str | Path],
    sample_dest: Path,
) -> dict[Path, Path]:
    copied: dict[Path, Path] = {}
    sample_dest_resolved = sample_dest.resolve()
    for source_dir in source_dirs:
        source = Path(source_dir)
        if not source.exists():
            continue
        for sample in sorted(source.iterdir()):
            if not sample.is_file() or sample.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            if sample_dest_resolved in sample.resolve().parents:
                continue
            output = sample_dest / sample.name
            if sample.resolve() != output.resolve():
                shutil.copy2(sample, output)
            copied[output] = sample
    return copied


def _matching_sample(copied_samples: dict[Path, Path], prompt_stem: str) -> Path | None:
    for sample in copied_samples:
        if sample.stem == prompt_stem:
            return sample
    return None


def _source_sample_for(copied_samples: dict[Path, Path], sample_path: Path | None) -> Path | None:
    if sample_path is None:
        return None
    return copied_samples.get(sample_path)


def _file_sha256(path: Path) -> str:
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
