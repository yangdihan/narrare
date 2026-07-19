from __future__ import annotations

from pathlib import Path

QWEN_VENDOR_ROOT = Path("tts/qwen/vendor")
QWEN_VENDOR_PACKAGE = QWEN_VENDOR_ROOT / "qwen_tts"
QWEN_MODELS_ROOT = Path("data/models/qwen")
QWEN_DEFAULT_MODEL_ID = "Qwen3-TTS-12Hz-1.7B-Base"
QWEN_DEFAULT_MODEL_PATH = QWEN_MODELS_ROOT / QWEN_DEFAULT_MODEL_ID
QWEN_BOOTSTRAP_MANIFEST_PATH = Path("data/models/qwen/bootstrap_manifest.json")
OLD_QWEN_FOLDER_MARKER = "Qwen3-Audiobook-Studio"
