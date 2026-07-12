from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """You are profiling a novel chunk for an audiobook pipeline.

Return valid JSON only. Do not wrap JSON in Markdown.

Narrare preserves the original novel exactly. Never rewrite, summarize,
normalize, translate, correct, trim, or otherwise alter source text.

Your output is metadata only: scene context and evidence-backed character
registry updates.
"""


def build_chunk_context_profiler_user_prompt(
    *,
    chunk_id: str,
    previous_summary: str | None,
    previous_context: str,
    chunk_text: str,
    next_context: str,
    character_registry: list[dict[str, Any]],
) -> str:
    metadata = {
        "chunk_id": chunk_id,
        "previous_summary": previous_summary,
        "existing_character_registry": character_registry,
    }
    schema = {
        "context": {
            "scene_summary": "Concise scene summary for this chunk only.",
            "active_characters": ["canonical or observed names"],
            "aliases_observed": [
                {
                    "text": "observed reference or name",
                    "reference_type": "stable_name",
                    "likely_character_id": "character_001",
                    "confidence": 0.92,
                    "review_notes": [],
                }
            ],
            "current_emotional_state": {"character_001": "brief state"},
            "unresolved_pronouns": [
                {
                    "text": "he",
                    "candidates": ["character_001", "character_002"],
                    "review_note": "Why it remains ambiguous.",
                }
            ],
            "important_context": ["facts useful for speaker attribution"],
            "confidence": 0.88,
            "review_notes": [],
        },
        "character_registry_updates": [
            {
                "character_id": "character_001",
                "proposed_character_id": None,
                "canonical_name": "canonical display name",
                "stable_aliases": ["canonical display name", "short stable name"],
                "contextual_references": [
                    {
                        "alias": "local honorific or title",
                        "reference_type": "honorific",
                        "evidence_text": "short exact supporting quote",
                        "source": "current_chunk",
                        "confidence": 0.9,
                        "review_notes": [],
                    }
                ],
                "alias_evidence": [
                    {
                        "alias": "short stable name",
                        "reference_type": "stable_name",
                        "evidence_text": "short exact supporting quote",
                        "source": "current_chunk",
                        "confidence": 0.9,
                        "review_notes": [],
                    }
                ],
                "persona_summary": "brief evidence-backed profile",
                "speaking_style": "brief evidence-backed style",
                "age_impression": None,
                "voice_variant_notes": [],
                "confidence": 0.9,
                "review_notes": [],
            }
        ],
    }
    return f"""Profile this source chunk before script conversion.

METADATA:
{json.dumps(metadata, ensure_ascii=False, indent=2)}

OUTPUT SCHEMA:
{json.dumps(schema, ensure_ascii=False, indent=2)}

RULES:
- Output one JSON object with "context" and "character_registry_updates".
- Treat previous and next context as reasoning context only.
- Do not include overlap text as audiobook script text.
- Keep summaries concise and evidence-backed.
- Use existing character_id when the registry already contains the identity.
- Use proposed_character_id only for a newly observed identity.
- Put only globally stable identity names in stable_aliases.
- stable_aliases are names that can safely identify the same character outside this scene, such as full names and stable short names.
- Put local forms of address, titles, pronouns, relationship labels, role labels, and descriptive phrases in contextual_references, not stable_aliases.
- Examples of contextual references: "马丁先生", "先生", "小先生", "我的当事人", "那个自由的机器人", "公司首脑", "他".
- Each observed reference must include reference_type: stable_name, honorific, role_title, relationship_title, pronoun, or descriptive_phrase.
- Only reference_type=stable_name can be used later for global deterministic speaker-key normalization.
- Contextual references are local speaker-attribution hints for this chunk only.
- Do not merge uncertain identities. Use lower confidence and review_notes instead.
- Include alias_evidence only for stable aliases that deterministic code may normalize later.
- Never attach final speaker labels to script segments here. Stage 2 does that.
- If an identity may need child/adult/old voice variants, add voice_variant_notes instead of splitting automatically.

PREVIOUS CONTEXT:
<<<PREVIOUS_CONTEXT_START>>>
{previous_context}
<<<PREVIOUS_CONTEXT_END>>>

CURRENT CHUNK:
<<<CHUNK_TEXT_START>>>
{chunk_text}
<<<CHUNK_TEXT_END>>>

NEXT CONTEXT:
<<<NEXT_CONTEXT_START>>>
{next_context}
<<<NEXT_CONTEXT_END>>>
"""
