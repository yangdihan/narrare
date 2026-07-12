from pathlib import Path


class Workspace:
    def __init__(self, project_id: str, root: str | Path = "data/interim") -> None:
        self.project_id = project_id
        self.root = Path(root)
        self.project_root = self.root / project_id
        self.chunks_dir = self.project_root / "chunks"
        self.context_ir_dir = self.project_root / "ir" / "context"
        self.script_ir_dir = self.project_root / "ir" / "script"

    def ensure(self) -> None:
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self.context_ir_dir.mkdir(parents=True, exist_ok=True)
        self.script_ir_dir.mkdir(parents=True, exist_ok=True)

    @property
    def source_manifest_path(self) -> Path:
        return self.project_root / "source_manifest.json"

    @property
    def chunks_path(self) -> Path:
        return self.project_root / "chunks.json"

    @property
    def validation_report_path(self) -> Path:
        return self.project_root / "validation_report.json"

    @property
    def character_registry_path(self) -> Path:
        return self.project_root / "characters.json"

    def chunk_text_path(self, index: int) -> Path:
        return self.chunks_dir / f"chunk_{index + 1:04d}.txt"

    def context_artifact_path(self, chunk_id: str) -> Path:
        return self.context_ir_dir / f"{chunk_id}_context.json"

    def context_raw_response_path(self, chunk_id: str) -> Path:
        return self.context_ir_dir / f"{chunk_id}_raw_response.json"

    def script_raw_response_path(self, chunk_id: str) -> Path:
        return self.script_ir_dir / f"{chunk_id}_raw_response.json"

    def script_artifact_path(self, chunk_id: str) -> Path:
        return self.script_ir_dir / f"{chunk_id}_script.json"

    def normalized_script_artifact_path(self, chunk_id: str) -> Path:
        return self.script_ir_dir / f"{chunk_id}_normalized_script.json"

    def speaker_key_normalization_report_path(self, chunk_id: str) -> Path:
        return self.script_ir_dir / f"{chunk_id}_speaker_key_normalization_report.json"

    def key_reviewed_script_artifact_path(self, chunk_id: str) -> Path:
        return self.script_ir_dir / f"{chunk_id}_key_reviewed_script.json"

    def speaker_key_review_report_path(self, chunk_id: str) -> Path:
        return self.script_ir_dir / f"{chunk_id}_speaker_key_review_report.json"

    @property
    def speaker_key_review_dir(self) -> Path:
        return self.script_ir_dir / "speaker_key_review"

    def speaker_key_review_raw_response_path(self, segment_id: str) -> Path:
        return self.speaker_key_review_dir / f"{segment_id}_raw_response.json"

    def script_validation_report_path(self, chunk_id: str) -> Path:
        return self.script_ir_dir / f"{chunk_id}_validation_report.json"

    def script_chunk_dir(self, chunk_id: str) -> Path:
        return self.script_ir_dir / chunk_id

    def script_attempt_raw_response_path(self, chunk_id: str, attempt: int) -> Path:
        return self.script_chunk_dir(chunk_id) / f"attempt_{attempt:02d}_raw_response.json"

    def script_attempt_artifact_path(self, chunk_id: str, attempt: int) -> Path:
        return self.script_chunk_dir(chunk_id) / f"attempt_{attempt:02d}_script.json"

    def script_attempt_validation_report_path(self, chunk_id: str, attempt: int) -> Path:
        return (
            self.script_chunk_dir(chunk_id)
            / f"attempt_{attempt:02d}_validation_report.json"
        )

    def script_repair_raw_response_path(
        self, chunk_id: str, attempt: int, repair_attempt: int
    ) -> Path:
        return (
            self.script_chunk_dir(chunk_id)
            / f"attempt_{attempt:02d}_repair_{repair_attempt:02d}_raw_response.json"
        )

    def script_repair_artifact_path(
        self, chunk_id: str, attempt: int, repair_attempt: int
    ) -> Path:
        return (
            self.script_chunk_dir(chunk_id)
            / f"attempt_{attempt:02d}_repair_{repair_attempt:02d}_script.json"
        )

    def script_repair_validation_report_path(
        self, chunk_id: str, attempt: int, repair_attempt: int
    ) -> Path:
        return (
            self.script_chunk_dir(chunk_id)
            / f"attempt_{attempt:02d}_repair_{repair_attempt:02d}_validation_report.json"
        )
