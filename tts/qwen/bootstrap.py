from __future__ import annotations

import hashlib
import importlib.util
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.models.voice import QwenBootstrapManifest
from core.pipeline.voice_assets import import_qwen_voice_assets
from storage.json_store import write_json
from tts.qwen.paths import (
    QWEN_BOOTSTRAP_MANIFEST_PATH,
    QWEN_DEFAULT_MODEL_ID,
    QWEN_MODELS_ROOT,
    QWEN_VENDOR_ROOT,
)

REQUIRED_DEPENDENCIES = [
    "torch",
    "soundfile",
    "librosa",
    "transformers",
    "safetensors",
    "accelerate",
    "numpy",
    "onnxruntime",
    "torchaudio",
    "einops",
]


def bootstrap_qwen_assets(
    *,
    source_root: str | Path,
    model_id: str = QWEN_DEFAULT_MODEL_ID,
    vendor_root: str | Path = QWEN_VENDOR_ROOT,
    models_root: str | Path = QWEN_MODELS_ROOT,
) -> QwenBootstrapManifest:
    source = Path(source_root)
    if not source.exists():
        raise RuntimeError(f"Qwen source folder not found: {source}")

    source_package = _find_source_package(source)
    source_model = source / "models" / model_id
    if not source_model.exists():
        raise RuntimeError(f"Qwen model not found: {source_model}")

    vendor_root_path = Path(vendor_root)
    vendor_package = vendor_root_path / "qwen_tts"
    _copy_tree(source_package, vendor_package)
    _patch_vendor_runtime(vendor_package)
    _copy_qwen_metadata(source, vendor_root_path)

    model_dest = Path(models_root) / model_id
    _copy_tree(source_model, model_dest)

    voice_inventory = import_qwen_voice_assets(
        prompt_source_dir=source / "voices",
        sample_source_dirs=[Path("data/voices")],
    )
    manifest = QwenBootstrapManifest(
        created_at=datetime.now(timezone.utc),
        source_root=str(source),
        model_id=model_id,
        vendor_path=str(vendor_root_path),
        model_path=str(model_dest),
        voice_inventory_path="data/voices/qwen/voice_profiles.json",
        copied_package_files=_count_files(vendor_package),
        copied_model_files=_count_files(model_dest),
        copied_voice_profiles=len(voice_inventory.profiles),
        missing_dependencies=missing_qwen_dependencies(),
        source_only_metadata={
            "source_sha256": _folder_fingerprint(source_package),
            "model_sha256": _folder_fingerprint(model_dest),
        },
    )
    write_json(QWEN_BOOTSTRAP_MANIFEST_PATH, manifest)
    return manifest


def missing_qwen_dependencies() -> list[str]:
    return [
        dependency
        for dependency in REQUIRED_DEPENDENCIES
        if importlib.util.find_spec(dependency) is None
    ]


def _find_source_package(source: Path) -> Path:
    candidates = [
        source / "runtime_env" / "lib" / "python3.10" / "site-packages" / "qwen_tts",
        source / "qwen_tts",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError("qwen_tts package not found in Qwen source folder")


def _copy_qwen_metadata(source: Path, vendor_root: Path) -> None:
    vendor_root.mkdir(parents=True, exist_ok=True)
    dist_info = (
        source
        / "runtime_env"
        / "lib"
        / "python3.10"
        / "site-packages"
        / "qwen_tts-0.1.1.dist-info"
    )
    if dist_info.exists():
        _copy_tree(dist_info, vendor_root / dist_info.name)


def _copy_tree(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def _patch_vendor_runtime(vendor_package: Path) -> None:
    speech_vq = vendor_package / "core" / "tokenizer_25hz" / "vq" / "speech_vq.py"
    if not speech_vq.exists():
        return
    text = speech_vq.read_text(encoding="utf-8")
    text = text.replace("import sox\n", "")
    if "import numpy as np\n" not in text:
        text = text.replace("import copy\n", "import copy\nimport numpy as np\n")
    text = text.replace(
        "\n        self.tfm = sox.Transformer()\n        self.tfm.norm(db_level=-6)\n",
        "\n",
    )
    text = text.replace(
        "    def sox_norm(self, audio):\n"
        "        wav_norm = self.tfm.build_array(input_array=audio, sample_rate_in=16000)\n"
        "        return wav_norm\n",
        "    def sox_norm(self, audio):\n"
        "        peak = np.max(np.abs(audio))\n"
        "        if peak <= 0:\n"
        "            return audio\n"
        "        return audio / peak * (10 ** (-6 / 20))\n",
    )
    speech_vq.write_text(text, encoding="utf-8")


def _count_files(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file()) if path.exists() else 0


def _folder_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        digest.update(str(file_path.relative_to(path)).encode("utf-8"))
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()
