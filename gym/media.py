"""Locally served, licensed exercise references; no external requests at runtime."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def exercise_media() -> dict[str, Any]:
    with (
        Path(__file__).with_name("exercise_media.json").open(encoding="utf-8") as source
    ):
        return dict(json.load(source))
