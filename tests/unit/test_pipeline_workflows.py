import json
from pathlib import Path

from core.models.chunk import ChunkingConfig
from core.models.character import (
    AliasEvidence,
    CharacterRecord,
    CharacterRegistryArtifact,
)
from core.models.ir import ScriptArtifact, ScriptSegment
from core.models.source import SourceSpan
from core.pipeline.chunking import run_chunking_workflow
from core.pipeline.chunk_context_profiler import (
    ContextProfileProgress,
    build_stage1_context_hint,
    run_chunk_context_profiler_workflow,
)
from core.pipeline.script_assembly import run_script_assembly_workflow
from core.pipeline.script_conversion import run_script_conversion_workflow
from core.pipeline.speaker_key_normalization import (
    run_speaker_key_normalization_workflow,
)
from core.pipeline.speaker_key_review import (
    SpeakerKeyReviewProgress,
    extract_speaker_key_review_candidates,
    run_speaker_key_review_workflow,
)
from core.validation.script_integrity import sha256_text
from llm.prompts.speaker_key_reviewer import build_speaker_key_reviewer_user_prompt
from llm.schemas import LlmCompletion
from llm.prompts.script_converter import build_script_converter_user_prompt
from storage.json_store import write_json


class SequentialLlmService:
    def __init__(self, responses: list[dict[str, object] | str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> LlmCompletion:
        self.prompts.append(user_prompt)
        response = self.responses.pop(0)
        if isinstance(response, str):
            return LlmCompletion(content=response)
        return LlmCompletion(content=json.dumps(response, ensure_ascii=False))


def write_stage3_review_fixture(
    tmp_path: Path,
    *,
    project_id: str = "fixture_project",
    speaker_key: str = "马丁先生",
) -> tuple[Path, Path]:
    source = tmp_path / "source.txt"
    source.write_text("他说你好他答", encoding="utf-8")
    chunk_result = run_chunking_workflow(
        source,
        project_id,
        workspace_root=tmp_path / "interim",
    )
    workspace = chunk_result.workspace
    write_json(
        workspace.character_registry_path,
        CharacterRegistryArtifact(
            project_id=project_id,
            characters=[
                CharacterRecord(
                    character_id="character_001",
                    canonical_name="安德鲁·马丁",
                    stable_aliases=["安德鲁"],
                    contextual_references=[
                        AliasEvidence(
                            alias="马丁先生",
                            reference_type="honorific",
                            evidence_text="马丁先生在本场景中指安德鲁。",
                            confidence=0.96,
                        )
                    ],
                    speaking_style="简短。",
                    confidence=0.95,
                )
            ],
        ),
    )
    write_json(
        workspace.context_artifact_path("chunk_0001"),
        {
            "project_id": project_id,
            "chunk_id": "chunk_0001",
            "llm_provider": "test",
            "llm_model": "test",
            "response_source": "response_path",
            "context": {
                "scene_summary": "马丁先生在本场景中指安德鲁。",
                "active_characters": ["安德鲁·马丁"],
                "aliases_observed": [
                    {
                        "text": "马丁先生",
                        "reference_type": "honorific",
                        "likely_character_id": "character_001",
                        "confidence": 0.96,
                        "review_notes": [],
                    }
                ],
                "current_emotional_state": {},
                "unresolved_pronouns": [],
                "important_context": ["本段只有安德鲁被称为马丁先生。"],
                "confidence": 0.9,
                "review_notes": [],
            },
            "character_registry_updates": [],
        },
    )
    write_json(
        workspace.script_artifact_path("complete"),
        ScriptArtifact(
            project_id=project_id,
            chunk_id="complete",
            chunk_source_path=str(workspace.chunks_path),
            chunk_sha256=sha256_text("他说你好他答"),
            llm_provider="test",
            llm_model="test",
            response_source="assembled",
            processed_chunk_count=1,
            segments=[
                ScriptSegment(
                    segment_id="seg_000001",
                    source_span=SourceSpan(start=0, end=2),
                    script={"narrator": "他说"},
                    confidence=0.91,
                ),
                ScriptSegment(
                    segment_id="seg_000002",
                    source_span=SourceSpan(start=2, end=4),
                    script={speaker_key: "你好"},
                    confidence=0.82,
                    review_notes=["raw speaker key from Stage 2"],
                ),
                ScriptSegment(
                    segment_id="seg_000003",
                    source_span=SourceSpan(start=4, end=6),
                    script={"narrator": "他答"},
                    confidence=0.93,
                ),
            ],
        ),
    )
    response_dir = tmp_path / "stage3_responses"
    response_dir.mkdir()
    return tmp_path / "interim", response_dir


def test_chunking_workflow_writes_expected_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("第一段。\nSecond paragraph.\n", encoding="utf-8")

    result = run_chunking_workflow(
        source,
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )

    assert result.validation_report.exact_reconstruction_success is True
    assert len(result.chunks) == 1
    assert (tmp_path / "interim" / "fixture_project" / "chunks.json").exists()
    assert (
        tmp_path / "interim" / "fixture_project" / "chunks" / "chunk_0001.txt"
    ).exists()


def test_script_conversion_workflow_writes_ir_with_response_path(
    tmp_path: Path,
) -> None:
    chunk = tmp_path / "chunk_0001.txt"
    chunk.write_text("他说，“你好。”\n她点头。", encoding="utf-8")
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "script": {"narrator": "他说"},
                        "confidence": 0.99,
                        "review_notes": [],
                    },
                    {
                        "script": {"安德鲁": "，“你好。"},
                        "confidence": 0.8,
                        "review_notes": ["Speaker inferred from context."],
                    },
                    {
                        "script": {"narrator": "”\n她点头。"},
                        "confidence": 0.95,
                        "review_notes": [],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_script_conversion_workflow(
        chunk,
        "fixture_project",
        "chunk_0001",
        response_path=response,
        workspace_root=tmp_path / "interim",
    )

    output_root = tmp_path / "interim" / "fixture_project" / "ir" / "script"
    assert result.exact_reconstruction_success is True
    assert result.artifact.processed_chunk_count == 1
    assert (output_root / "chunk_0001_script.json").exists()
    assert (output_root / "chunk_0001_validation_report.json").exists()
    assert (output_root / "chunk_0001" / "attempt_01_raw_response.json").exists()


def test_script_conversion_repairs_misaligned_paragraph_with_shrinking_retry(
    tmp_path: Path,
) -> None:
    chunk = tmp_path / "chunk_0001.txt"
    chunk.write_text("甲甲。\n乙乙。\n丙丙。", encoding="utf-8")
    service = SequentialLlmService(
        [
            {
                "segments": [
                    {"script": {"narrator": "甲甲"}, "confidence": 0.9},
                    {"script": {"narrator": "乙错"}, "confidence": 0.9},
                    {"script": {"narrator": "丙丙"}, "confidence": 0.9},
                ]
            },
            {
                "segments": [
                    {"script": {"narrator": "乙乙"}, "confidence": 0.95},
                ]
            },
        ]
    )

    result = run_script_conversion_workflow(
        chunk,
        "fixture_project",
        "chunk_0001",
        max_retries=1,
        workspace_root=tmp_path / "interim",
        llm_service=service,
    )

    output_root = tmp_path / "interim" / "fixture_project" / "ir" / "script"
    assert result.exact_reconstruction_success is True
    assert len(service.prompts) == 2
    assert "Repair one source span" in service.prompts[1]
    assert "乙乙。" in service.prompts[1]
    assert result.artifact.segments == [
            ScriptSegment(
                segment_id="seg_000001",
                source_span=SourceSpan(start=0, end=len("甲甲。\n乙乙。\n丙丙。")),
            script={"narrator": "甲甲乙乙丙丙"},
            confidence=0.9,
            review_notes=[
                "Merged consecutive same-speaker segments deterministically.",
                "Merged consecutive same-speaker segments deterministically.",
            ],
        )
    ]
    assert (
        output_root
        / "chunk_0001"
        / "attempt_01_repair_01_raw_response.json"
    ).exists()
    assert (output_root / "chunk_0001" / "attempt_01_repair_01_script.json").exists()
    assert (
        output_root
        / "chunk_0001"
        / "attempt_01_repair_01_validation_report.json"
    ).exists()


def test_invalid_script_json_retries_whole_chunk_without_repair(
    tmp_path: Path,
) -> None:
    chunk = tmp_path / "chunk_0001.txt"
    chunk.write_text("甲甲。", encoding="utf-8")
    service = SequentialLlmService(
        [
            "{not json",
            {
                "segments": [
                    {"script": {"narrator": "甲甲"}, "confidence": 0.9},
                ]
            },
        ]
    )

    result = run_script_conversion_workflow(
        chunk,
        "fixture_project",
        "chunk_0001",
        max_retries=2,
        workspace_root=tmp_path / "interim",
        llm_service=service,
    )

    output_root = tmp_path / "interim" / "fixture_project" / "ir" / "script"
    assert result.exact_reconstruction_success is True
    assert len(service.prompts) == 2
    assert "Repair one source span" not in service.prompts[0]
    assert "Repair one source span" not in service.prompts[1]
    assert not (
        output_root / "chunk_0001" / "attempt_01_repair_01_raw_response.json"
    ).exists()


def test_chunk_context_profiler_writes_context_and_registry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("安德鲁说话。\n", encoding="utf-8")
    chunk_result = run_chunking_workflow(
        source,
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )
    response_dir = tmp_path / "responses"
    response_dir.mkdir()
    (response_dir / "chunk_0001_response.json").write_text(
        json.dumps(
            {
                "context": {
                    "scene_summary": "安德鲁正在说话。",
                    "active_characters": ["安德鲁"],
                    "aliases_observed": [
                        {
                            "text": "安德鲁",
                            "reference_type": "stable_name",
                            "likely_character_id": "character_001",
                            "confidence": 0.95,
                            "review_notes": [],
                        },
                        {
                            "text": "马丁先生",
                            "reference_type": "honorific",
                            "likely_character_id": "character_001",
                            "confidence": 0.9,
                            "review_notes": [],
                        }
                    ],
                    "current_emotional_state": {
                        "character_001": "calm",
                    },
                    "unresolved_pronouns": [],
                    "important_context": ["安德鲁是本段活动角色。"],
                    "confidence": 0.92,
                    "review_notes": [],
                },
                "character_registry_updates": [
                    {
                        "character_id": "character_001",
                        "canonical_name": "安德鲁·马丁",
                        "stable_aliases": ["安德鲁"],
                        "contextual_references": [
                            {
                                "alias": "马丁先生",
                                "reference_type": "honorific",
                                "evidence_text": "“马丁先生，你好。”",
                                "source": "current_chunk",
                                "confidence": 0.9,
                                "review_notes": [],
                            }
                        ],
                        "alias_evidence": [
                            {
                                "alias": "安德鲁",
                                "reference_type": "stable_name",
                                "evidence_text": "安德鲁说话",
                                "source": "current_chunk",
                                "confidence": 0.95,
                                "review_notes": [],
                            }
                        ],
                        "persona_summary": "礼貌。",
                        "speaking_style": "简短。",
                        "age_impression": None,
                        "voice_variant_notes": [],
                        "confidence": 0.95,
                        "review_notes": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    progress_events: list[ContextProfileProgress] = []
    result = run_chunk_context_profiler_workflow(
        "fixture_project",
        response_dir=response_dir,
        workspace_root=tmp_path / "interim",
        progress_callback=progress_events.append,
    )

    workspace = chunk_result.workspace
    assert len(result.artifacts) == 1
    assert len(result.registry.characters) == 1
    assert result.registry.characters[0].canonical_name == "安德鲁·马丁"
    assert result.registry.characters[0].stable_aliases == [
        "安德鲁·马丁",
        "安德鲁",
    ]
    assert result.registry.characters[0].aliases == ["安德鲁·马丁", "安德鲁"]
    assert result.registry.characters[0].contextual_references[0].alias == "马丁先生"
    assert result.registry.characters[0].contextual_references[0].reference_type == (
        "honorific"
    )
    assert [event.status for event in progress_events] == [
        "running",
        "chunk_started",
        "chunk_complete",
        "complete",
    ]
    assert progress_events[-1].processed_chunks == 1
    assert progress_events[-1].total_chunks == 1
    assert workspace.context_artifact_path("chunk_0001").exists()
    assert workspace.character_registry_path.exists()


def test_script_converter_prompt_does_not_include_stage1_metadata() -> None:
    prompt = build_script_converter_user_prompt(
        chunk_id="chunk_0001",
        chunk_text="“你好。”",
    )

    assert "known_characters" not in prompt
    assert "context_summary" not in prompt
    assert "Stage 1" not in prompt
    assert "Prefer a stable speaker key" not in prompt
    assert "The source chunk is the only source for script values." in prompt


def test_script_assembly_merges_same_speaker_chunk_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("甲甲\n乙乙\n", encoding="utf-8")
    chunk_result = run_chunking_workflow(
        source,
        "fixture_project",
        config=ChunkingConfig(
            target_chunk_tokens=4,
            min_chunk_chars=1,
            target_chunk_chars=3,
            max_chunk_chars=4,
            overlap_tokens=0,
        ),
        workspace_root=tmp_path / "interim",
    )
    workspace = chunk_result.workspace

    for chunk in chunk_result.chunks:
        write_json(
            workspace.script_artifact_path(chunk.chunk_id),
            ScriptArtifact(
                project_id="fixture_project",
                chunk_id=chunk.chunk_id,
                chunk_source_path=str(workspace.chunk_text_path(chunk.index)),
                chunk_sha256=sha256_text(chunk.text),
                llm_provider="test",
                llm_model="test",
                response_source="response_path",
                processed_chunk_count=1,
                segments=[
                    ScriptSegment(
                        segment_id="seg_000001",
                        source_span=SourceSpan(start=0, end=len(chunk.text)),
                        script={"narrator": chunk.text},
                        confidence=0.9,
                    )
                ],
            ),
        )

    result = run_script_assembly_workflow(
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )

    output_root = tmp_path / "interim" / "fixture_project" / "ir" / "script"
    assert result.exact_reconstruction_success is True
    assert result.boundary_merge_count == 1
    assert result.artifact.processed_chunk_count == 2
    assert len(result.artifact.segments) == 1
    assert result.artifact.segments[0].script == {"narrator": "甲甲\n乙乙\n"}
    assert result.artifact.segments[0].source_span == SourceSpan(start=0, end=6)
    assert (output_root / "complete_script.json").exists()
    assert (output_root / "complete_validation_report.json").exists()


def test_speaker_key_normalization_renames_alias_without_touching_text_or_span(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("你好他说", encoding="utf-8")
    chunk_result = run_chunking_workflow(
        source,
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )
    workspace = chunk_result.workspace
    write_json(
        workspace.character_registry_path,
        CharacterRegistryArtifact(
            project_id="fixture_project",
            characters=[
                CharacterRecord(
                    character_id="character_001",
                    canonical_name="安德鲁·马丁",
                    stable_aliases=["安德鲁"],
                    alias_evidence=[
                        AliasEvidence(
                            alias="安德鲁",
                            reference_type="stable_name",
                            evidence_text="安德鲁说",
                            confidence=0.95,
                        )
                    ],
                    confidence=0.95,
                )
            ],
        ),
    )
    write_json(
        workspace.script_artifact_path("complete"),
        ScriptArtifact(
            project_id="fixture_project",
            chunk_id="complete",
            chunk_source_path=str(workspace.chunks_path),
            chunk_sha256=sha256_text("你好他说"),
            llm_provider="test",
            llm_model="test",
            response_source="assembled",
            processed_chunk_count=1,
            segments=[
                ScriptSegment(
                    segment_id="seg_000001",
                    source_span=SourceSpan(start=0, end=2),
                    script={"安德鲁": "你好"},
                    confidence=0.9,
                ),
                ScriptSegment(
                    segment_id="seg_000002",
                    source_span=SourceSpan(start=2, end=4),
                    script={"narrator": "他说"},
                    confidence=0.9,
                ),
            ],
        ),
    )

    result = run_speaker_key_normalization_workflow(
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )

    first = result.artifact.segments[0]
    assert result.exact_reconstruction_success is True
    assert result.renamed_count == 1
    assert first.script == {"安德鲁·马丁": "你好"}
    assert first.raw_script_key == "安德鲁"
    assert first.source_span == SourceSpan(start=0, end=2)
    assert first.segment_id == "seg_000001"
    assert (workspace.normalized_script_artifact_path("complete")).exists()
    assert (workspace.speaker_key_normalization_report_path("complete")).exists()


def test_speaker_key_normalization_does_not_rename_contextual_reference(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("你好他说", encoding="utf-8")
    chunk_result = run_chunking_workflow(
        source,
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )
    workspace = chunk_result.workspace
    write_json(
        workspace.character_registry_path,
        CharacterRegistryArtifact(
            project_id="fixture_project",
            characters=[
                CharacterRecord(
                    character_id="character_001",
                    canonical_name="安德鲁·马丁",
                    stable_aliases=["安德鲁"],
                    contextual_references=[
                        AliasEvidence(
                            alias="马丁先生",
                            reference_type="honorific",
                            evidence_text="“马丁先生，你好。”",
                            confidence=0.96,
                        )
                    ],
                    confidence=0.95,
                )
            ],
        ),
    )
    write_json(
        workspace.script_artifact_path("complete"),
        ScriptArtifact(
            project_id="fixture_project",
            chunk_id="complete",
            chunk_source_path=str(workspace.chunks_path),
            chunk_sha256=sha256_text("你好他说"),
            llm_provider="test",
            llm_model="test",
            response_source="assembled",
            processed_chunk_count=1,
            segments=[
                ScriptSegment(
                    segment_id="seg_000001",
                    source_span=SourceSpan(start=0, end=2),
                    script={"马丁先生": "你好"},
                    confidence=0.9,
                ),
                ScriptSegment(
                    segment_id="seg_000002",
                    source_span=SourceSpan(start=2, end=4),
                    script={"narrator": "他说"},
                    confidence=0.9,
                ),
            ],
        ),
    )

    result = run_speaker_key_normalization_workflow(
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )

    first = result.artifact.segments[0]
    assert result.renamed_count == 0
    assert result.unresolved_count == 1
    assert first.script == {"马丁先生": "你好"}
    assert first.raw_script_key is None


def test_martin_family_honorific_ambiguity_is_not_normalized(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("你好", encoding="utf-8")
    chunk_result = run_chunking_workflow(
        source,
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )
    workspace = chunk_result.workspace
    write_json(
        workspace.character_registry_path,
        CharacterRegistryArtifact(
            project_id="fixture_project",
            characters=[
                CharacterRecord(
                    character_id="character_001",
                    canonical_name="安德鲁·马丁",
                    stable_aliases=["安德鲁"],
                    contextual_references=[
                        AliasEvidence(
                            alias="马丁先生",
                            reference_type="honorific",
                            evidence_text="马丁先生 addressed Andrew here.",
                            confidence=0.96,
                        )
                    ],
                    confidence=0.95,
                ),
                CharacterRecord(
                    character_id="character_002",
                    canonical_name="杰拉尔德·马丁",
                    stable_aliases=["杰拉尔德·马丁"],
                    contextual_references=[
                        AliasEvidence(
                            alias="马丁先生",
                            reference_type="honorific",
                            evidence_text="马丁先生 addressed Gerald here.",
                            confidence=0.96,
                        )
                    ],
                    confidence=0.95,
                ),
            ],
        ),
    )
    write_json(
        workspace.script_artifact_path("complete"),
        ScriptArtifact(
            project_id="fixture_project",
            chunk_id="complete",
            chunk_source_path=str(workspace.chunks_path),
            chunk_sha256=sha256_text("你好"),
            llm_provider="test",
            llm_model="test",
            response_source="assembled",
            processed_chunk_count=1,
            segments=[
                ScriptSegment(
                    segment_id="seg_000001",
                    source_span=SourceSpan(start=0, end=2),
                    script={"马丁先生": "你好"},
                    confidence=0.9,
                )
            ],
        ),
    )

    result = run_speaker_key_normalization_workflow(
        "fixture_project",
        workspace_root=tmp_path / "interim",
    )

    assert result.renamed_count == 0
    assert result.artifact.segments[0].script == {"马丁先生": "你好"}


def test_speaker_key_review_candidate_extraction_skips_canonical_and_narrator() -> None:
    segments = [
        ScriptSegment(
            segment_id="seg_000001",
            source_span=SourceSpan(start=0, end=2),
            script={"安德鲁·马丁": "你好"},
            confidence=0.9,
        ),
        ScriptSegment(
            segment_id="seg_000002",
            source_span=SourceSpan(start=2, end=4),
            script={"narrator": "他说"},
            confidence=0.9,
        ),
        ScriptSegment(
            segment_id="seg_000003",
            source_span=SourceSpan(start=4, end=6),
            script={"安德鲁": "再见"},
            confidence=0.9,
        ),
        ScriptSegment(
            segment_id="seg_000004",
            source_span=SourceSpan(start=6, end=8),
            script={"马丁先生": "可以"},
            confidence=0.9,
        ),
        ScriptSegment(
            segment_id="seg_000005",
            source_span=SourceSpan(start=8, end=10),
            script={"unknown_speaker": "嗯"},
            confidence=0.5,
        ),
    ]

    candidates = extract_speaker_key_review_candidates(
        segments,
        canonical_names={"安德鲁·马丁"},
    )

    assert [candidate.segment.segment_id for candidate in candidates] == [
        "seg_000003",
        "seg_000004",
        "seg_000005",
    ]


def test_speaker_key_review_applies_high_confidence_key_only_change(
    tmp_path: Path,
) -> None:
    workspace_root, response_dir = write_stage3_review_fixture(tmp_path)
    (response_dir / "seg_000002_response.json").write_text(
        json.dumps(
            {
                "segment_id": "seg_000002",
                "current_key": "马丁先生",
                "decision": "replace",
                "replacement_key": "安德鲁·马丁",
                "confidence": 0.96,
                "evidence": ["Stage 1 says this local 马丁先生 is 安德鲁。"],
                "review_notes": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_speaker_key_review_workflow(
        "fixture_project",
        response_dir=response_dir,
        workspace_root=workspace_root,
    )

    workspace = result.workspace
    reviewed = result.artifact.segments[1]
    assert result.exact_reconstruction_success is True
    assert result.reviewed_count == 1
    assert result.changed_count == 1
    assert reviewed.script == {"安德鲁·马丁": "你好"}
    assert reviewed.raw_script_key == "马丁先生"
    assert reviewed.speaker_key_review is not None
    assert reviewed.speaker_key_review["to"] == "安德鲁·马丁"
    assert reviewed.segment_id == "seg_000002"
    assert reviewed.source_span == SourceSpan(start=2, end=4)
    assert reviewed.confidence == 0.82
    assert reviewed.review_notes == ["raw speaker key from Stage 2"]
    assert workspace.key_reviewed_script_artifact_path("complete").exists()
    assert workspace.speaker_key_review_report_path("complete").exists()
    assert workspace.speaker_key_review_raw_response_path("seg_000002").exists()


def test_speaker_key_review_emits_progress_events(
    tmp_path: Path,
) -> None:
    workspace_root, response_dir = write_stage3_review_fixture(tmp_path)
    (response_dir / "seg_000002_response.json").write_text(
        json.dumps(
            {
                "segment_id": "seg_000002",
                "current_key": "马丁先生",
                "decision": "keep",
                "replacement_key": None,
                "confidence": 0.7,
                "evidence": [],
                "review_notes": ["No strong replacement."],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    progress_events: list[SpeakerKeyReviewProgress] = []

    run_speaker_key_review_workflow(
        "fixture_project",
        response_dir=response_dir,
        workspace_root=workspace_root,
        progress_callback=progress_events.append,
    )

    assert [event.status for event in progress_events] == [
        "running",
        "candidate_started",
        "candidate_complete",
        "complete",
    ]
    assert progress_events[1].segment_id == "seg_000002"
    assert progress_events[1].current_key == "马丁先生"
    assert progress_events[2].processed_candidates == 1
    assert progress_events[-1].total_candidates == 1


def test_speaker_key_review_reports_low_confidence_without_change(
    tmp_path: Path,
) -> None:
    workspace_root, response_dir = write_stage3_review_fixture(
        tmp_path,
        speaker_key="安德鲁",
    )
    (response_dir / "seg_000002_response.json").write_text(
        json.dumps(
            {
                "segment_id": "seg_000002",
                "current_key": "安德鲁",
                "decision": "replace",
                "replacement_key": "安德鲁·马丁",
                "confidence": 0.6,
                "evidence": ["Alias match exists but the local scene is unclear."],
                "review_notes": ["Needs human review."],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_speaker_key_review_workflow(
        "fixture_project",
        response_dir=response_dir,
        workspace_root=workspace_root,
    )
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert result.changed_count == 0
    assert result.artifact.segments[1].script == {"安德鲁": "你好"}
    assert result.artifact.segments[1].raw_script_key is None
    assert report["events"][0]["status"] == "low_confidence"


def test_stage1_context_hint_keeps_contextual_references_local(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "interim"
    project_root = workspace_root / "fixture_project"
    context_root = project_root / "ir" / "context"
    context_root.mkdir(parents=True)
    write_json(
        context_root / "chunk_0001_context.json",
        {
            "project_id": "fixture_project",
            "chunk_id": "chunk_0001",
            "llm_provider": "test",
            "llm_model": "test",
            "response_source": "response_path",
            "context": {
                "scene_summary": "马丁先生在本场景中指安德鲁。",
                "active_characters": ["安德鲁·马丁"],
                "aliases_observed": [
                    {
                        "text": "马丁先生",
                        "reference_type": "honorific",
                        "likely_character_id": "character_001",
                        "confidence": 0.96,
                        "review_notes": [],
                    }
                ],
                "current_emotional_state": {},
                "unresolved_pronouns": [],
                "important_context": [],
                "confidence": 0.9,
                "review_notes": [],
            },
            "character_registry_updates": [],
        },
    )
    write_json(
        project_root / "characters.json",
        CharacterRegistryArtifact(
            project_id="fixture_project",
            characters=[
                CharacterRecord(
                    character_id="character_001",
                    canonical_name="安德鲁·马丁",
                    stable_aliases=["安德鲁"],
                    contextual_references=[
                        AliasEvidence(
                            alias="马丁先生",
                            reference_type="honorific",
                            evidence_text="local address",
                            confidence=0.96,
                        )
                    ],
                    confidence=0.95,
                )
            ],
        ),
    )

    hint = build_stage1_context_hint(
        "fixture_project",
        "chunk_0001",
        workspace_root=workspace_root,
    )

    assert hint is not None
    assert hint.known_characters == [
        {
            "character_id": "character_001",
            "canonical_name": "安德鲁·马丁",
            "stable_aliases": ["安德鲁·马丁", "安德鲁"],
        }
    ]
    assert hint.contextual_references[0]["text"] == "马丁先生"
    assert hint.contextual_references[0]["canonical_name"] == "安德鲁·马丁"


def test_speaker_key_reviewer_prompt_includes_context_without_rewrite_request() -> None:
    prompt = build_speaker_key_reviewer_user_prompt(
        segment={
            "segment_id": "seg_000002",
            "source_span": {"start": 2, "end": 4},
            "script": {"马丁先生": "你好"},
            "confidence": 0.8,
            "review_notes": [],
        },
        previous_segment={
            "segment_id": "seg_000001",
            "source_span": {"start": 0, "end": 2},
            "script": {"narrator": "他说"},
            "confidence": 0.9,
            "review_notes": [],
        },
        next_segment={
            "segment_id": "seg_000003",
            "source_span": {"start": 4, "end": 6},
            "script": {"narrator": "道"},
            "confidence": 0.9,
            "review_notes": [],
        },
        scene_context={
            "covered_chunks": [
                {
                    "chunk_id": "chunk_0001",
                    "scene_summary": "马丁先生在本场景中指安德鲁。",
                }
            ]
        },
        relevant_characters=[
            {
                "canonical_name": "安德鲁·马丁",
                "stable_aliases": ["安德鲁"],
                "contextual_references": [
                    {
                        "alias": "马丁先生",
                        "reference_type": "honorific",
                        "evidence_text": "local address",
                    }
                ],
                "speaking_style": "简短。",
                "review_notes": [],
            }
        ],
        allowed_replacement_keys=["安德鲁·马丁", "narrator", "unknown_speaker"],
        confidence_threshold=0.85,
    )

    assert "candidate_segment" in prompt
    assert "previous_segment" in prompt
    assert "next_segment" in prompt
    assert "马丁先生在本场景中指安德鲁" in prompt
    assert "安德鲁·马丁" in prompt
    assert "Never rewrite the script object value." in prompt
