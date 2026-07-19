from __future__ import annotations

import math
import wave

from tts.base import SynthesisRequest, SynthesisResult


class DummyTTSAdapter:
    adapter_name = "dummy"

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 16_000
        duration_seconds = max(0.25, min(2.0, len(request.text) / 20))
        frame_count = int(sample_rate * duration_seconds)
        frequency = 220 + (sum(ord(char) for char in request.text) % 220)
        amplitude = 0.15
        with wave.open(str(request.output_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            frames = bytearray()
            for index in range(frame_count):
                value = int(
                    32767
                    * amplitude
                    * math.sin(2 * math.pi * frequency * index / sample_rate)
                )
                frames.extend(value.to_bytes(2, byteorder="little", signed=True))
            wav.writeframes(bytes(frames))
        return SynthesisResult(
            output_path=request.output_path,
            sample_rate=sample_rate,
            adapter=self.adapter_name,
            model_path=request.model_path,
            parameters=request.parameters,
        )
