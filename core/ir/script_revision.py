from __future__ import annotations

import hashlib
import json

from core.models.ir import ScriptArtifact, ScriptSegment


def script_artifact_revision(artifact: ScriptArtifact) -> str:
    """Return a stable identity for the script structure and its spoken text."""
    return script_segments_revision(artifact.segments)


def script_segments_revision(segments: list[ScriptSegment]) -> str:
    payload = [
        {
            "segment_id": segment.segment_id,
            "source_span": {
                "start": segment.source_span.start,
                "end": segment.source_span.end,
            },
            "speaker": segment.speaker,
            "text": segment.text,
        }
        for segment in segments
    ]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
