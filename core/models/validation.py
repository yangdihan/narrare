from pydantic import BaseModel, Field


class ValidationReport(BaseModel):
    project_id: str
    exact_reconstruction_success: bool
    chunk_count: int = Field(ge=0)
    source_character_count: int = Field(ge=0)
    reconstructed_character_count: int = Field(ge=0)
    source_hash: str
    reconstructed_hash: str
    errors: list[str]
