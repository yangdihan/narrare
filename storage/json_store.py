from pathlib import Path
from typing import Any

from pydantic import BaseModel


def write_json(path: str | Path, payload: BaseModel | dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(payload, BaseModel):
        content = payload.model_dump_json(indent=2)
    else:
        import json

        content = json.dumps(payload, ensure_ascii=False, indent=2)

    output_path.write_text(content + "\n", encoding="utf-8")
