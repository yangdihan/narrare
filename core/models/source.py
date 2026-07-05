from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SourceSpan(BaseModel):
    """A half-open character span into immutable source text."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @property
    def length(self) -> int:
        return self.end - self.start


class SourceDocument(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    encoding: str
    text: str

    @property
    def character_count(self) -> int:
        return len(self.text)


class SourceManifest(BaseModel):
    project_id: str
    source_path: str
    encoding: str
    sha256: str
    character_count: int = Field(ge=0)
    estimated_token_count: int = Field(ge=0)
    created_at: str
