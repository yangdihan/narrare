from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """You review audiobook script speaker keys.

Return valid JSON only. Do not wrap JSON in Markdown.

Narrare preserves the original novel exactly. You may decide whether a script
object key should be kept, replaced, or marked uncertain. Never rewrite,
summarize, normalize, trim, or otherwise alter script values.
"""


def build_speaker_key_reviewer_user_prompt(
    *,
    segment: dict[str, Any],
    previous_segment: dict[str, Any] | None,
    next_segment: dict[str, Any] | None,
    scene_context: dict[str, Any],
    relevant_characters: list[dict[str, Any]],
    allowed_replacement_keys: list[str],
    confidence_threshold: float,
) -> str:
    payload = {
        "candidate_segment": segment,
        "previous_segment": previous_segment,
        "next_segment": next_segment,
        "stage1_scene_context": scene_context,
        "relevant_character_debriefs": relevant_characters,
        "allowed_replacement_keys": allowed_replacement_keys,
        "auto_apply_confidence_threshold": confidence_threshold,
    }
    schema = {
        "segment_id": "seg_000042",
        "current_key": "马丁先生",
        "decision": "replace",
        "replacement_key": "安德鲁·马丁",
        "confidence": 0.91,
        "evidence": ["Stage 1 scene context locally maps 马丁先生 to 安德鲁·马丁."],
        "review_notes": [],
    }
    return f"""Review one script segment speaker key.

INPUT:
{json.dumps(payload, ensure_ascii=False, indent=2)}

OUTPUT SCHEMA:
{json.dumps(schema, ensure_ascii=False, indent=2)}

DECISION RULES:
- Output one JSON object matching the schema.
- Review only the script object key. Never rewrite the script object value.
- The candidate script value must be treated as immutable source-derived text.
- The final speaker key should be the canonical character name whenever the speaker can be identified unambiguously.
- A non-canonical alias is not an acceptable final key merely because it correctly identifies the speaker.
- If current_key exactly matches stable_aliases for exactly one character, use "replace" with that character's canonical_name.
- If current_key is a contextual reference for exactly one character and local segment evidence confirms that identity, use "replace" with that character's canonical_name.
- Use "keep" only when current_key should remain unchanged as a final key, or when no allowed replacement is better supported.
- Use "uncertain" when multiple characters plausibly match or the evidence does not reliably identify one character.
- If decision is "replace", replacement_key must be exactly one entry from allowed_replacement_keys.
- If decision is "keep" or "uncertain", replacement_key must be null.
- Allowed replacement keys are canonical character names, "narrator", and "unknown_speaker".
- Do not invent character names.
- Do not output character_id values as speaker keys.
- Stage 1 scene context and character debriefs are speaker-attribution evidence only.
- Stage 1 context is not a source for script values.
- Contextual references such as honorifics, role titles, relationship titles, pronouns, and descriptive phrases are local evidence, not globally safe aliases.
- If multiple characters could match the current key, return "uncertain" or replace with "unknown_speaker" only when that is better than the current key.
- Before returning "keep", verify that the evidence does not establish current_key as a stable alias of exactly one canonical character.
- If the evidence establishes that current_key is a stable alias of exactly one character, the decision must be "replace" and replacement_key must be that character's canonical_name.
- Keep evidence concise and quote only short identifying clues.

Return valid JSON only.
"""
