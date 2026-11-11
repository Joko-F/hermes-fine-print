"""The limits sheet: a short block frozen into each NEW session's system prompt.

It carries only facts the model needs inside a turn where no per-call note is possible (a tool
result with images cannot be annotated without dropping the images). Numbers come from
``constants`` and the live config. ``/limits sheet off`` removes it from the next new session.
"""

from __future__ import annotations

from typing import Any, Callable

from . import constants as C

SECTION_ID = "fine-print.sheet"


def build_sheet(cfg: Callable[[str, Any], Any]) -> str:
    def num(key: str, default: int) -> int:
        value = cfg(key, default)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    budget = num("vision.embed_target_bytes", C.EMBED_TARGET_BYTES_DEFAULT)
    page = num("web.extract_char_limit", C.WEB_EXTRACT_CHAR_LIMIT)
    text = (
        f"Hermes v{C.HERMES_VERSION} limits that are easy to miss (from the Fine Print plugin):\n"
        f"- Images loaded through tools are halved until the long edge is at most {C.EMBED_MAX_DIMENSION} px "
        f"and the data URL fits {budget:,} bytes, so fine print, digits and small UI text can be unreadable. "
        f"A vision_analyze region crop keeps more detail: at most {C.EMBED_MAX_DIMENSION} px on its long edge "
        "it is sent unscaled when it also fits the byte budget; the scale note states what arrived. "
        "Browser screenshots are downscaled the same way and carry no scale note.\n"
        f"- search_files content search stops reading at offset+limit (default {C.SEARCH_DEFAULT_LIMIT}) and "
        "sets no truncated flag; a full page means more matches may exist.\n"
        f"- web_extract fetches at most {C.WEB_EXTRACT_URL_CAP} URLs per call and cuts each page to "
        f"{page:,} chars (head and tail); its footer names the saved full copy.\n"
        f"- read_file reads .xlsx up to {C.XLSX_MAX_ROWS:,} rows and {C.XLSX_MAX_COLS} columns per sheet and "
        "skips hidden sheets without a marker."
    )
    return text[: C.SHEET_MAX_CHARS]
