"""The limits reference: 76 catalogued limits of Hermes v0.23.0 (``catalog.json``, built from the
research catalog and checked by ``dev/check_catalog.py``)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

TOPICS = ("images", "web", "files", "tools", "context", "model", "settings")


@lru_cache(maxsize=1)
def load() -> dict:
    try:
        data = json.loads((Path(__file__).parent / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": "", "entries": []}
    entries = data.get("entries") if isinstance(data, dict) else None
    return {"version": str(data.get("version", "")), "entries": entries if isinstance(entries, list) else []}


def entries() -> list[dict]:
    return load()["entries"]


def get(limit_id: str) -> Optional[dict]:
    wanted = limit_id.strip().lower()
    for entry in entries():
        if str(entry.get("id", "")).lower() == wanted:
            return entry
    matches = [e for e in entries() if wanted and wanted in str(e.get("id", "")).lower()]
    return matches[0] if len(matches) == 1 else None


def by_topic(topic: str) -> list[dict]:
    return [e for e in entries() if e.get("topic") == topic]
