from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AssemblyClipSelection(BaseModel):
    segment_id: str
    speaker: str
    take_number: int = Field(ge=1)
    voice_profile_id: str
    text_sha256: str
    audio_path: str
    take_manifest_path: str


class AssemblyManifest(BaseModel):
    artifact_type: Literal["audiobook_assembly"] = "audiobook_assembly"
    project_id: str
    script_artifact_path: str
    created_at: datetime
    clips: list[AssemblyClipSelection] = Field(default_factory=list)


class NormalizedClipResult(AssemblyClipSelection):
    input_sample_rate: int = Field(gt=0)
    output_sample_count: int = Field(ge=0)
    input_active_rms_dbfs: float | None = None
    applied_gain_db: float
    output_peak_dbfs: float | None = None


class FinalAudiobookManifest(BaseModel):
    artifact_type: Literal["final_audiobook"] = "final_audiobook"
    project_id: str
    assembly_manifest_path: str
    output_path: str
    sample_rate: int = Field(gt=0)
    channels: Literal[1] = 1
    subtype: str
    target_active_rms_dbfs: float
    peak_ceiling_dbfs: float
    clip_count: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    created_at: datetime
    clips: list[NormalizedClipResult] = Field(default_factory=list)
