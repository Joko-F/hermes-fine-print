"""Structural parsers for the exact v0.23.0 output formats.

Rule P2: never scan content for a substring. Every parser here is anchored to the position and
shape core writes (a footer at the END of a page, a block at the START of a committed row, top-level
JSON keys). Anything that does not match returns None / an empty list; callers treat that as "no
signal", never as an error.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

# ── web_extract footer (tools/web_tools_truncate.py:86-128) ────────────────────────────────────────
# ──────── [TRUNCATED] ────────
# Showing 11,250 chars (head) + 3,733 chars (tail) of 24,116 total clean characters.
# Full text saved to: /path/host-sha10.md
# To read the omitted middle: read_file path="/path" offset=115 limit=200  (...)
# ─────────────────────────────
_WEB_FOOTER = re.compile(
    r"\n\n─{8} \[TRUNCATED\] ─{8}\n"
    r"Showing ([\d,]+) chars \(head\) \+ ([\d,]+) chars \(tail\) of ([\d,]+) total clean characters\.\n"
    r"(?:Full text saved to: ([^\n]+)\n"
    r"To read the omitted middle: read_file path=\"([^\"\n]+)\" offset=(\d+) limit=200  "
    r"\(the file is the complete page; raise/lower offset to page through it\)\.\n"
    r"|Full text could not be stored; re-run web_extract on a more specific URL or open it in the browser "
    r"for the complete page\.\n)"
    r"─{29}\s*\Z"
)
_WEB_MIDDLE = "\n\n[... middle omitted — see footer ...]\n\n"
_WEB_COPY_NAME = re.compile(r"(?:\A|[/\\])[A-Za-z0-9._-]+-([0-9a-f]{10})\.md\Z")


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def parse_web_footer(content: Any, url: str = "") -> Optional[dict]:
    """The head+tail footer at the very end of one page's ``content``; None when the page was not cut.

    The footer is text inside the page's own content, so a page could print one itself. Everything a
    tool result allows is therefore checked: the complete v0.23.0 layout, the omission marker sitting
    exactly between a head and a tail of the stated lengths, the saved copy named twice the same way,
    and its file name carrying the first ten hex digits of sha256(url) as core builds it
    (tools/web_tools_truncate.py:69). A page that reproduces all of that for its own URL would still
    pass: there is no out-of-band truncation flag in v0.23.0, and the model reads the same footer."""
    if not isinstance(content, str) or len(content) < 120:
        return None
    window = content[-2500:]
    m = _WEB_FOOTER.search(window)
    if not m:
        return None
    head, tail, total = _int(m.group(1)), _int(m.group(2)), _int(m.group(3))
    if total <= 0 or head + tail > total:
        return None
    body_len = len(content) - (len(window) - m.start())
    if body_len != head + len(_WEB_MIDDLE) + tail or content[head: head + len(_WEB_MIDDLE)] != _WEB_MIDDLE:
        return None
    path = (m.group(4) or "").strip()
    if path:
        name = _WEB_COPY_NAME.search(path)
        if not name or path != m.group(5):
            return None
        if url and name.group(1) != hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()[:10]:
            return None
    return {"head": head, "tail": tail, "total": total, "path": path,
            "offset": int(m.group(6)) if m.group(6) else None}


# ── JSON head ──────────────────────────────────────────────────────────────────────────────────────
def json_object(text: Any) -> Optional[dict]:
    """Parse ``text`` as one JSON object, only when it starts with ``{``. Never raises."""
    if not isinstance(text, str) or not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


# ── committed-row shapes ───────────────────────────────────────────────────────────────────────────
_UNTRUSTED_OPEN = re.compile(r"\A<untrusted_tool_result source=\"([a-z0-9_.:-]+)\">\n")
_UNTRUSTED_CLOSE = "\n</untrusted_tool_result>"


def strip_untrusted(text: Any) -> Any:
    """Committed web_extract / web_search / browser / mcp rows are wrapped as untrusted data with a
    fixed preamble paragraph. Return the inner payload, or ``text`` unchanged when it is not wrapped."""
    if not isinstance(text, str):
        return text
    m = _UNTRUSTED_OPEN.match(text)
    if not m:
        return text
    inner = text[m.end():]
    stripped = inner.rstrip()
    if stripped.endswith(_UNTRUSTED_CLOSE.strip()):
        inner = stripped[: -len(_UNTRUSTED_CLOSE.strip())].rstrip("\n")
    # the preamble is one paragraph, then a blank line, then the payload
    sep = inner.find("\n\n")
    if sep != -1 and inner.startswith("The following content was retrieved from an external source."):
        inner = inner[sep + 2:]
    return inner


# <persisted-output>
# This tool result was too large (150,000 characters, 146.5 KB).
# Full output saved to: /path
# ...
# Preview (first 1500 chars):
_PERSISTED = re.compile(
    r"\A<persisted-output>\s*This tool result was too large \(([\d,]+) characters[^)]*\)\.\s*"
    r"Full output saved to: ([^\n]+)\n"
)
_PREVIEW = re.compile(r"Preview \(first ([\d,]+) chars\):")


def parse_persisted(content: Any) -> Optional[dict]:
    """A committed tool row that was spilled to a file: ``{total, path, preview}``; else None."""
    if not isinstance(content, str) or not content.startswith("<persisted-output>"):
        return None
    m = _PERSISTED.match(content[:800])
    if not m:
        return None
    p = _PREVIEW.search(content[:1600])
    return {"total": _int(m.group(1)), "path": m.group(2).strip(), "preview": _int(p.group(1)) if p else None}


# The fallback when the full output could not be saved anywhere: the row keeps a head and ends with
#   [Truncated: tool response was 120,000 chars. Full output could not be saved to sandbox.]
_LOST_TAIL = re.compile(
    r"\n\n\[Truncated: tool response was ([\d,]+) chars\. Full output could not be saved to sandbox\.\]\s*\Z"
)


def parse_lost_tail(content: Any) -> Optional[dict]:
    """A committed row cut inline with nothing saved: ``{total, shown}``; else None."""
    if not isinstance(content, str) or not content.rstrip().endswith("]"):
        return None
    m = _LOST_TAIL.search(content[-200:])
    if not m:
        return None
    total = _int(m.group(1))
    shown = len(content) - (len(content[-200:]) - m.start())
    return {"total": total, "shown": max(0, shown)} if total > shown else None


# ── vision scale note (tools/vision_tools.py:595-616) ──────────────────────────────────────────────
# The note is its own line, after the echoed question:
#   Note: Image downscaled from 2752x3975 to 688x993 for vision; multiply any coordinates you report by 4.00 ...
_SCALE_NOTE = re.compile(
    r"^(?:\[)?Note: Image downscaled from (\d+)x(\d+) to (\d+)x(\d+) for vision; "
    r"multiply any (?:coordinates you report by [\d.]+|x coordinates you report by [\d.]+ "
    r"and any y coordinates by [\d.]+) to map back", re.M
)


def parse_scale_note(text: Any) -> Optional[dict]:
    if not isinstance(text, str):
        return None
    m = _SCALE_NOTE.search(text[:20_000])
    if not m:
        return None
    ow, oh, nw, nh = (int(m.group(i)) for i in range(1, 5))
    if min(ow, oh, nw, nh) <= 0 or nw > ow or nh > oh:
        return None
    return {"orig_w": ow, "orig_h": oh, "w": nw, "h": nh}


# ── image headers from data URLs ───────────────────────────────────────────────────────────────────
_DATA_URL = re.compile(r"\Adata:(image/[a-z0-9.+-]+);base64,", re.I)


def data_url_info(url: Any, max_b64: int = 87_384) -> Optional[dict]:
    """``{mime, bytes, w, h}`` of a base64 image data URL, decoding at most ``max_b64`` base64 chars
    (64 KB). ``w``/``h`` are None when the header is not within that window or the format is unknown."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL.match(url[:64])
    if not m:
        return None
    b64_len = len(url) - m.end()
    pad = url[-2:].count("=")
    size = max(0, b64_len * 3 // 4 - pad)
    chunk = url[m.end(): m.end() + max_b64]
    chunk = chunk[: len(chunk) - len(chunk) % 4]
    try:
        raw = base64.b64decode(chunk, validate=False)
    except (ValueError, TypeError):
        raw = b""
    w, h = image_size(raw)
    return {"mime": m.group(1).lower(), "bytes": size, "w": w, "h": h}


def image_size(raw: bytes) -> tuple[Optional[int], Optional[int]]:
    """Pixel size from the first bytes of a PNG, JPEG, GIF or WebP file; (None, None) when unknown."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
            return struct.unpack(">II", raw[16:24])
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", raw[6:10])
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            kind = raw[12:16]
            if kind == b"VP8X":
                return (int.from_bytes(raw[24:27], "little") + 1, int.from_bytes(raw[27:30], "little") + 1)
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", raw[26:30])
                return (w & 0x3FFF, h & 0x3FFF)
            if kind == b"VP8L":
                bits = int.from_bytes(raw[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if raw[:2] == b"\xff\xd8":
            i, n = 2, len(raw)
            while i + 9 < n:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5: i + 9])
                    return (w, h)
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", raw[i + 2: i + 4])[0]
    except (struct.error, IndexError):
        pass
    return (None, None)


# ── identifiers that are safe to keep (metadata only) ──────────────────────────────────────────────
def short_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:12]


def url_host(url: Any) -> str:
    try:
        return (urlsplit(str(url)).hostname or "")[:80]
    except ValueError:
        return ""


def basename(path: Any) -> str:
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1][:120]


def safe_label(value: Any) -> str:
    """A short label for a file or URL that is safe to show and store: the last path segment only.
    A URL loses its query, fragment and user info; a data URL becomes just "image"."""
    text = str(value or "")
    if text[:5].lower() == "data:":
        return "image"
    if "://" in text[:12]:
        try:
            text = urlsplit(text).path
        except ValueError:
            return ""
    else:
        text = text.split("?", 1)[0].split("#", 1)[0]
    return basename(text)[:80]


_TOKEN = re.compile(r"[^A-Za-z0-9_.:+-]")


def clean_token(value: Any, limit: int = 40) -> str:
    """A backend or model name copied from a tool result, reduced to a plain identifier."""
    return _TOKEN.sub("", str(value or ""))[:limit]


_STORED_JSON = "\x00json:"


def decode_stored(content: Any) -> Any:
    """Hermes stores structured message content (a list of parts) as ``\\x00json:`` + JSON. Return the
    text of its text parts joined by newlines, so the history driver sees what a text row would hold;
    anything else comes back unchanged. Image payloads are never decoded."""
    if not isinstance(content, str) or not content.startswith(_STORED_JSON):
        return content
    try:
        parts = json.loads(content[len(_STORED_JSON):])
    except (ValueError, RecursionError):
        return content
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return content
    return "\n".join(p["text"] for p in parts
                      if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))


def iter_text_parts(envelope: Any) -> Iterator[str]:
    """Text parts of a multimodal tool envelope ``{'_multimodal': True, 'content': [...]}``."""
    if isinstance(envelope, dict):
        for part in envelope.get("content") or ():
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                yield part["text"]


def iter_image_urls(envelope: Any) -> Iterator[str]:
    if isinstance(envelope, dict):
        for part in envelope.get("content") or ():
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = part.get("image_url")
                url = url.get("url") if isinstance(url, dict) else url
                if isinstance(url, str):
                    yield url
