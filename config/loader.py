from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from config.models import AppConfig


def load_config(path: str | Path = "config/default.yaml") -> AppConfig:
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    llm_data = data.get("llm")
    if isinstance(llm_data, dict) and "chunking" in llm_data and "chunking" not in data:
        chunking_data = dict(llm_data["chunking"] or {})
        for key in ("provider", "model", "context_window_tokens"):
            if key in llm_data and key not in chunking_data:
                chunking_data[key] = llm_data[key]
        data["chunking"] = chunking_data
    return AppConfig.model_validate(data)
