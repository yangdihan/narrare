from __future__ import annotations

import json
import re
from typing import Any


_THINKING_TAG_RE = re.compile(
    r"<(?:think|thinking|reflection|reasoning)>[\s\S]*?</(?:think|thinking|reflection|reasoning)>",
    re.IGNORECASE,
)
_UNCLOSED_THINKING_TAG_RE = re.compile(
    r"<(?:think|thinking|reflection|reasoning)>[\s\S]*$",
    re.IGNORECASE,
)


def clean_json_response(text: str) -> str:
    cleaned = _THINKING_TAG_RE.sub("", text)
    cleaned = _UNCLOSED_THINKING_TAG_RE.sub("", cleaned).strip()

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("could not find a JSON object in LLM response")

    return cleaned[start : end + 1]


def parse_json_object_response(text: str) -> dict[str, Any]:
    cleaned = clean_json_response(text)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON must be an object")
    return parsed
