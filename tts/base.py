from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    voice_prompt_path: Path
    output_path: Path
    language: str = "Auto"
    model_path: Path | None = None
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisResult:
    output_path: Path
    sample_rate: int
    adapter: str
    model_path: Path | None = None
    parameters: dict[str, object] = field(default_factory=dict)


class TTSAdapter(Protocol):
    adapter_name: str

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize one text segment into one audio file."""
