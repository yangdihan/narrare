from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

import soundfile as sf
import torch

from tts.base import SynthesisRequest, SynthesisResult
from tts.qwen.audio import convert_audio
from tts.qwen.paths import QWEN_DEFAULT_MODEL_PATH, QWEN_VENDOR_PACKAGE, QWEN_VENDOR_ROOT


class QwenTTSAdapter:
    adapter_name = "qwen"

    def __init__(
        self,
        *,
        model_path: str | Path | None = None,
        vendor_root: str | Path = QWEN_VENDOR_ROOT,
        device: str | None = None,
    ) -> None:
        self.vendor_root = Path(vendor_root)
        self.model_path = Path(
            model_path or os.environ.get("NARRARE_QWEN_MODEL", "") or QWEN_DEFAULT_MODEL_PATH
        )
        self.device = device or os.environ.get("NARRARE_QWEN_DEVICE", "auto")
        self._model: Any | None = None
        self._voice_item_cls: Any | None = None

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        model_path = request.model_path or self.model_path
        self._validate_ready(model_path)
        output_path = request.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        target_path = output_path
        temp_wav: Path | None = None
        if output_path.suffix.lower() != ".wav":
            temp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            temp.close()
            temp_wav = Path(temp.name)
            target_path = temp_wav

        model = self._load_model(model_path)
        prompt_items = self._load_voice_prompt(request.voice_prompt_path)
        wavs, sample_rate = model.generate_voice_clone(
            text=request.text,
            language=request.language,
            voice_clone_prompt=prompt_items,
            **request.parameters,
        )
        sf.write(target_path, wavs[0], sample_rate)
        if temp_wav is not None:
            convert_audio(temp_wav, output_path)
            temp_wav.unlink(missing_ok=True)

        return SynthesisResult(
            output_path=output_path,
            sample_rate=sample_rate,
            adapter=self.adapter_name,
            model_path=model_path,
            parameters=request.parameters,
        )

    def create_voice_prompt(
        self,
        *,
        sample_path: str | Path,
        transcript: str,
        output_path: str | Path,
        x_vector_only_mode: bool = False,
    ) -> Path:
        self._validate_ready(self.model_path)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        model = self._load_model(self.model_path)
        items = model.create_voice_clone_prompt(
            ref_audio=str(sample_path),
            ref_text=transcript,
            x_vector_only_mode=x_vector_only_mode,
        )
        payload = {"items": [asdict(item) for item in items]}
        torch.save(payload, output)
        return output

    def _validate_ready(self, model_path: Path) -> None:
        vendor_package = self.vendor_root / "qwen_tts"
        if not vendor_package.exists():
            raise RuntimeError(f"vendored qwen_tts package not found: {vendor_package}")
        if not model_path.exists():
            raise RuntimeError(f"Qwen model path not found: {model_path}")

    def _load_model(self, model_path: Path):
        self._ensure_vendor_import_path()
        if self._model is not None:
            return self._model

        _disable_transformers_optional_sklearn()
        from qwen_tts import Qwen3TTSModel, VoiceClonePromptItem

        self._voice_item_cls = VoiceClonePromptItem
        device = _resolve_device(self.device)
        self._model = Qwen3TTSModel.from_pretrained(
            str(model_path),
            device_map=device,
            dtype=torch.float32 if device in {"cpu", "mps"} else torch.bfloat16,
        )
        return self._model

    def _load_voice_prompt(self, voice_prompt_path: Path):
        if self._voice_item_cls is None:
            self._load_model(self.model_path)
        try:
            prompt_payload = torch.load(
                voice_prompt_path,
                map_location="cpu",
                weights_only=True,
            )
        except TypeError:
            prompt_payload = torch.load(voice_prompt_path, map_location="cpu")
        allowed_fields = {field.name for field in fields(self._voice_item_cls)}
        return [
            self._voice_item_cls(
                **{key: value for key, value in item.items() if key in allowed_fields}
            )
            for item in prompt_payload["items"]
        ]

    def _ensure_vendor_import_path(self) -> None:
        vendor_path = str(self.vendor_root.resolve())
        if vendor_path not in sys.path:
            sys.path.insert(0, vendor_path)


def build_tts_adapter(name: str | None = None) -> QwenTTSAdapter:
    adapter_name = name or os.environ.get("NARRARE_TTS_ADAPTER", "qwen")
    if adapter_name != "qwen":
        raise RuntimeError(f"Unsupported Qwen adapter name: {adapter_name}")
    return QwenTTSAdapter()


def qwen_status() -> dict[str, object]:
    return {
        "vendor_package_exists": QWEN_VENDOR_PACKAGE.exists(),
        "model_path": str(QWEN_DEFAULT_MODEL_PATH),
        "model_exists": QWEN_DEFAULT_MODEL_PATH.exists(),
        "vendor_root": str(QWEN_VENDOR_ROOT),
    }


def _disable_transformers_optional_sklearn() -> None:
    try:
        from transformers.utils import import_utils
    except ImportError:
        return
    import_utils._sklearn_available = False
    import_utils._torchvision_available = False
    import_utils._torchvision_version = "N/A"


def _resolve_device(device: str) -> str:
    normalized = device.lower()
    if normalized == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    if normalized == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("Qwen device cuda requested, but CUDA is not available")
    if normalized == "mps":
        if not getattr(torch.backends, "mps", None) or not torch.backends.mps.is_available():
            raise RuntimeError("Qwen device mps requested, but MPS is not available")
    if normalized not in {"cpu", "cuda", "mps"}:
        raise RuntimeError("Qwen device must be one of: auto, cpu, cuda, mps")
    return normalized
