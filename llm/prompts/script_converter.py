from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """You are converting novel text into structured audiobook script IR.

Return valid JSON only. Do not wrap JSON in Markdown.

Narrare preserves the original novel exactly. Never rewrite, summarize,
normalize, translate, correct, trim, or otherwise alter source voice content.

Your job is segmentation and speaker attribution only.
"""


def build_script_converter_user_prompt(
    *,
    chunk_id: str,
    chunk_text: str,
    known_characters: list[str] | None = None,
    previous_segments: list[dict[str, Any]] | None = None,
    context_summary: dict[str, Any] | None = None,
) -> str:
    metadata = {
        "chunk_id": chunk_id,
        "known_characters": known_characters or [],
        "previous_segments": previous_segments or [],
        "context_summary": context_summary or {},
    }
    schema = {
        "segments": [
            {
                "script": {"narrator": "安德鲁·马丁说，"},
                "confidence": 0.98,
                "review_notes": [],
            },
            {
                "script": {"安德鲁·马丁": "“谢谢，”"},
                "confidence": 0.95,
                "review_notes": [],
            },
        ]
    }
    return f"""Convert this source chunk into ordered audiobook script segments.

METADATA:
{json.dumps(metadata, ensure_ascii=False, indent=2)}

OUTPUT SCHEMA:
{json.dumps(schema, ensure_ascii=False, indent=2)}

SEGMENTATION RULES:
- Output one JSON object with a top-level "segments" array.
- Each segment must contain exactly one "script" object with exactly one key.
- Do not include source_span or segment_id. Narrare will derive those.
- The single script value is original voice-bearing text copied from the chunk.
- Concatenating all script values in order must reproduce the source chunk after removing whitespace and punctuation.
- Do not change, add, or remove Chinese characters, letters, or digits.
- Whitespace, indentation, line breaks, and punctuation may be omitted because they are not voice-bearing content.
- Omit whitespace-only and punctuation-only spans.
- Use "narrator" for narration, actions, descriptions, attribution tags, and speech tags.
- Use the inferred raw character name for quoted spoken or thought text.
- Use "unknown_speaker" when speaker identity is not confidently known.
- Never use attribution phrases such as "他说", "她说", "他说道", or "她问道" as speaker keys.
- Split narration before and after quoted speech.
- Preserve short narration such as "他说" or "他问道" as narrator text.
- Preserve post-dialogue attribution such as "安德鲁承认道" or "先生说" as a narrator segment after the quoted speech.
- Do not output one giant "narrator" segment when quotation marks or speaker changes appear.

EXAMPLE:
Source: 安德鲁·马丁说，“谢谢，”同时坐下。
Output:
{{
  "segments": [
    {{"script": {{"narrator": "安德鲁·马丁说，"}}, "confidence": 0.98, "review_notes": []}},
    {{"script": {{"安德鲁·马丁": "“谢谢，”"}}, "confidence": 0.95, "review_notes": []}},
    {{"script": {{"narrator": "同时坐下。"}}, "confidence": 0.98, "review_notes": []}}
  ]
}}

Source: “先生，我喜欢做家具。”安德鲁承认道。
Output:
{{
  "segments": [
    {{"script": {{"安德鲁": "“先生，我喜欢做家具。”"}}, "confidence": 0.95, "review_notes": []}},
    {{"script": {{"narrator": "安德鲁承认道。"}}, "confidence": 0.98, "review_notes": []}}
  ]
}}

SOURCE CHUNK:
<<<CHUNK_TEXT_START>>>
{chunk_text}
<<<CHUNK_TEXT_END>>>
"""
