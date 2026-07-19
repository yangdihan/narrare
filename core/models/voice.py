from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class VoiceProfile(BaseModel):
    profile_id: str
    display_name: str
    prompt_path: str
    prompt_sha256: str
    sample_path: str | None = None
    sample_sha256: str | None = None
    source_prompt_path: str | None = None
    source_sample_path: str | None = None
    adapter: Literal["qwen_voice_clone_prompt"] = "qwen_voice_clone_prompt"


class VoiceInventoryArtifact(BaseModel):
    artifact_type: Literal["voice_inventory"] = "voice_inventory"
    created_at: datetime
    voice_root: str
    profiles: list[VoiceProfile] = Field(default_factory=list)


class QwenBootstrapManifest(BaseModel):
    artifact_type: Literal["qwen_bootstrap"] = "qwen_bootstrap"
    created_at: datetime
    source_root: str
    model_id: str
    vendor_path: str
    model_path: str
    voice_inventory_path: str
    copied_package_files: int = Field(ge=0)
    copied_model_files: int = Field(ge=0)
    copied_voice_profiles: int = Field(ge=0)
    missing_dependencies: list[str] = Field(default_factory=list)
    source_only_metadata: dict[str, str] = Field(default_factory=dict)


class VoiceAssignment(BaseModel):
    speaker: str
    voice_profile_id: str | None = None
    representative_segment_id: str | None = None
    representative_text: str = ""
    summary: str | None = None
    sample_take_path: str | None = None
    confirmed: bool = False


class VoiceAssignmentArtifact(BaseModel):
    artifact_type: Literal["voice_assignments"] = "voice_assignments"
    project_id: str
    script_artifact_path: str
    created_at: datetime
    updated_at: datetime
    assignments: list[VoiceAssignment] = Field(default_factory=list)


class AudioTakeManifest(BaseModel):
    artifact_type: Literal["audio_take"] = "audio_take"
    project_id: str
    segment_id: str
    speaker: str
    text: str
    voice_profile_id: str
    voice_prompt_path: str
    script_artifact_path: str
    adapter: str
    model_path: str | None = None
    parameters: dict[str, object] = Field(default_factory=dict)
    output_path: str
    created_at: datetime
