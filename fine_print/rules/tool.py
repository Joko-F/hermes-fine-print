"""Tool-driver rules: facts visible in a tool's own output (``transform_tool_result``), before core
spills, wraps or budgets it. Each rule is gated by tool name and parses structure only (P2)."""

from __future__ import annotations

import math
import os
import re
import zipfile
from typing import Any, Iterator, Optional

from .. import constants as C
from ..engine import Obs, Recover, ToolEvent, rule
from ..signals import (basename, data_url_info, iter_image_urls, iter_text_parts, parse_scale_note,
                       parse_web_footer, url_host)


def _num(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── web_extract ────────────────────────────────────────────────────────────────────────────────────
@rule("web_extract.head_tail_truncation", driver="tool", tools={"web_extract"}, core_tells="model",
      status="verified")
def web_cut(ev: ToolEvent) -> Iterator[Obs]:
    """A page over the char budget is cut to head + tail; the footer at the end of ``content`` gives
    the numbers and the saved copy. Core tells the model; nobody tells the user."""
    results = (ev.json or {}).get("results")
    if not isinstance(results, list):
        return
    asked_limit = _num(ev.args.get("char_limit"), 0)
    for item in results:
        if not isinstance(item, dict):
            continue
        foot = parse_web_footer(item.get("content"))
        if not foot:
            continue
        url = str(item.get("url") or "")
        shown, total = foot["head"] + foot["tail"], foot["total"]
        saved = basename(foot["path"]) if foot["path"] else ""
        recover = ()
        if saved:
            recover += (Recover.names(saved),)
        if url:
            recover += (Recover.refetch_bigger(url, max(asked_limit, shown)),)
        yield Obs("web_extract.head_tail_truncation",
                  numbers={"shown": shown, "total": total, "head": foot["head"], "tail": foot["tail"]},
                  subject=url_host(url) or saved, cause=url or saved, recover=recover,
                  severity="card" if shown < 0.8 * total else "digest")


@rule("web_extract.url_cap_5", driver="tool", tools={"web_extract"}, core_tells="nobody", status="fixture")
def web_url_cap(ev: ToolEvent) -> Iterator[Obs]:
    """The handler slices ``urls[:5]``: later URLs are never fetched and get no error entry."""
    urls = ev.args.get("urls")
    if not isinstance(urls, list) or len(urls) <= C.WEB_EXTRACT_URL_CAP:
        return
    dropped = [str(u) for u in urls[C.WEB_EXTRACT_URL_CAP:]]
    n = len(urls)
    yield Obs("web_extract.url_cap_5",
              numbers={"asked": n, "fetched": C.WEB_EXTRACT_URL_CAP, "dropped": len(dropped)},
              subject=f"{n} URLs", cause="|".join(dropped), recover=(Recover.refetch(dropped),),
              note=(f"web_extract fetched only the first {C.WEB_EXTRACT_URL_CAP} of {n} URLs (v0.23.0 cap); "
                    f"URLs {C.WEB_EXTRACT_URL_CAP + 1}-{n} were not fetched and have no result entry. "
                    "A second call can fetch them."))


# ── web_search ─────────────────────────────────────────────────────────────────────────────────────
@rule("web_search.rescue_nested_error", driver="tool", tools={"web_search"}, core_tells="model",
      status="verified")
def search_rescued(ev: ToolEvent) -> Iterator[Obs]:
    """The configured backend failed and the keyless free tier answered; the result still says
    success. The marker is the ``data.rescued_from`` / ``data.fallback_from`` KEY, never a substring."""
    data = (ev.json or {}).get("data")
    if not isinstance(data, dict):
        return
    failed = data.get("rescued_from") or data.get("fallback_from")
    if not isinstance(failed, str) or not failed:
        return
    served = data.get("served_by")
    served = served if isinstance(served, str) and served else "the keyless free tier"
    web = data.get("web")
    count = len(web) if isinstance(web, list) else -1
    yield Obs("web_search.rescue_nested_error",
              numbers={"results": count, "failed": failed[:40], "served_by": served[:40]},
              subject=f"{failed[:40]} -> {served[:40]}", cause=failed)


# ── search_files ───────────────────────────────────────────────────────────────────────────────────
@rule("search-files-silent-page-cap", driver="tool", tools={"search_files"}, core_tells="nobody",
      status="verified")
def search_page_full(ev: ToolEvent) -> Iterator[Obs]:
    """Content search reads exactly offset+limit lines, so a full page carries no truncated flag and
    ``total_count`` is the page size, not the total. Only target='files' fetches one extra row."""
    args = ev.args
    if (args.get("target") or "content") == "files" or _num(args.get("context"), 0) > 0:
        return
    obj = ev.json
    if obj is None or "truncated" in obj or "total_count_is_lower_bound" in obj:
        return
    limit = _num(args.get("limit"), C.SEARCH_DEFAULT_LIMIT)
    offset = _num(args.get("offset"), 0)
    if limit <= 0:
        return
    mode = args.get("output_mode") or "content"
    total = obj.get("total_count")
    # every content mode reports total_count = rows read so far = offset + rows on this page
    if not isinstance(total, int) or isinstance(total, bool) or total != offset + limit:
        return
    pattern, path = str(args.get("pattern", "")), str(args.get("path", "") or "")
    seen = offset + limit
    # A small page the model asked for itself is usually a deliberate sample: log it and tell the
    # model, but keep the card for pages of the default size or larger.
    yield Obs("search-files-silent-page-cap",
              numbers={"shown": limit, "offset": offset, "mode": str(mode)[:12]},
              subject=str(mode)[:12], cause=f"{pattern}\0{path}\0{args.get('file_glob', '')}",
              recover=(Recover.search_more(pattern, path, seen),),
              severity="card" if limit >= C.SEARCH_DEFAULT_LIMIT else "digest",
              note=(f"search_files returned a full page ({limit} entries from offset {offset}). v0.23.0 sets no "
                    f"truncated flag here, so the real total is unknown and more may exist; offset={seen} "
                    "continues the listing."))


# ── images ─────────────────────────────────────────────────────────────────────────────────────────
def _halvings(long_edge: int) -> int:
    n = 0
    while long_edge > C.EMBED_MAX_DIMENSION and n < 4:
        long_edge //= 2
        n += 1
    return n


@rule("native-embed-downscale-1568", driver="tool", tools={"vision_analyze"}, core_tells="model",
      status="verified")
def image_downscaled(ev: ToolEvent) -> Iterator[Obs]:
    """An image the model loads through a tool is halved until its long edge is at most 1568 px and
    its data URL fits ``vision.embed_target_bytes``. Core adds a note about coordinates only."""
    texts = [ev.text] if ev.text is not None else list(iter_text_parts(ev.result))
    note = None
    for text in texts:
        note = parse_scale_note(text)
        if note:
            break
    if not note:
        return
    ow, oh, w, h = note["orig_w"], note["orig_h"], note["w"], note["h"]
    ratio = (ow * oh) / max(1, w * h)
    # which trigger fired: the hardcoded edge, the configurable byte budget, or both (P5)
    by_edge = _halvings(max(ow, oh))
    steps = max(0, round(math.log2(max(ow, oh) / max(1, max(w, h)))))
    numbers = {"orig_w": ow, "orig_h": oh, "w": w, "h": h, "ratio": round(ratio, 1),
               "edge_cap": int(by_edge > 0), "byte_cap": int(steps > by_edge)}
    for url in iter_image_urls(ev.result):
        info = data_url_info(url, C.IMAGE_HEADER_MAX_B64)
        if info:
            numbers["sent_bytes"] = info["bytes"]
            numbers["mime"] = info["mime"]
        break
    image = str(ev.args.get("image_url") or "")
    yield Obs("native-embed-downscale-1568", numbers=numbers, subject=basename(image) if image else "image",
              cause=image, recover=(Recover.region(image),) if image else (),
              severity="card" if ratio >= 3.9 else "digest")


# ── office files ───────────────────────────────────────────────────────────────────────────────────
_DIMENSION = re.compile(rb"<dimension\s+ref=\"[A-Z]+\d+(?::([A-Z]+)(\d+))?\"")
_SHEET = re.compile(rb"<sheet\b[^>]*>")
_STATE = re.compile(rb"\bstate=\"(hidden|veryHidden)\"")
_RID = re.compile(rb"\br:id=\"([^\"]+)\"")
_REL = re.compile(rb"<Relationship\b[^>]*>")
_REL_ID = re.compile(rb"\bId=\"([^\"]+)\"")
_REL_TARGET = re.compile(rb"\bTarget=\"([^\"]+)\"")
_ROW = re.compile(rb"<row[\s>]")
_SCAN_MAX = 32 * 1024 * 1024          # decompressed bytes scanned per file at most


def _col_index(letters: bytes) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ch - 64)
    return n


def _count(zf: zipfile.ZipFile, name: str, pattern: "re.Pattern[bytes]") -> Optional[int]:
    """Occurrences of ``pattern`` in a zip member, streamed; None when the member is too large to
    scan within the hook's time budget (the caller then stays silent instead of guessing)."""
    if zf.getinfo(name).file_size > _SCAN_MAX:
        return None
    total, tail = 0, b""
    with zf.open(name) as fh:
        while True:
            chunk = fh.read(1 << 20)
            data = tail + chunk
            if not chunk:
                return total + sum(1 for _ in pattern.finditer(data))
            cut = max(0, len(data) - 32)        # a match starting in the last 32 bytes is counted next round
            total += sum(1 for m in pattern.finditer(data) if m.start() < cut)
            tail = data[cut:]


def probe_xlsx(path: str) -> Optional[dict]:
    """Rows and columns of the largest VISIBLE sheet and the number of hidden sheets, from the
    workbook's own XML (read-only). Rows come from ``<dimension>`` when the writer recorded one, else
    from counting ``<row>`` elements. None when the file is unreadable or too large to count."""
    try:
        with zipfile.ZipFile(path) as zf:
            members = set(zf.namelist())
            book = zf.read("xl/workbook.xml") if "xl/workbook.xml" in members else b""
            rels = zf.read("xl/_rels/workbook.xml.rels") if "xl/_rels/workbook.xml.rels" in members else b""
            targets = {}
            for rel in _REL.findall(rels):
                rid, target = _REL_ID.search(rel), _REL_TARGET.search(rel)
                if rid and target:
                    t = target.group(1).decode("utf-8", "replace").lstrip("/")
                    targets[rid.group(1)] = t if t.startswith("xl/") else "xl/" + t
            hidden, visible = 0, []
            for tag in _SHEET.findall(book):
                rid = _RID.search(tag)
                name = targets.get(rid.group(1)) if rid else None
                if _STATE.search(tag):
                    hidden += 1
                elif name in members:
                    visible.append(name)
            if not visible:
                visible = sorted(n for n in members if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
            rows = cols = 0
            known = False
            for name in visible[:64]:
                with zf.open(name) as fh:
                    head = fh.read(4096)
                m = _DIMENSION.search(head)
                if m and m.group(2):
                    n_rows, n_cols = int(m.group(2)), _col_index(m.group(1))
                else:
                    n_rows, n_cols = _count(zf, name, _ROW), 0
                    if n_rows is None:
                        continue
                known = True
                rows, cols = max(rows, n_rows), max(cols, n_cols)
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        return None
    return {"rows": rows, "cols": cols, "hidden_sheets": hidden, "sheets": len(visible)} if known else None


_DOCX_PARTS = {"tables": re.compile(rb"<w:tbl>"), "images": re.compile(rb"<w:drawing>|<w:pict>"),
               "footnotes": re.compile(rb"<w:footnoteReference\b"), "endnotes": re.compile(rb"<w:endnoteReference\b"),
               "comments": re.compile(rb"<w:commentReference\b")}


def probe_docx(path: str) -> Optional[dict]:
    """What read_file's paragraph-only extraction leaves out of a .docx: table structure, images,
    footnotes, endnotes, comments, headers and footers. Counted from the document's own XML."""
    try:
        with zipfile.ZipFile(path) as zf:
            members = zf.namelist()
            if "word/document.xml" not in members:
                return None
            out = {}
            for key, pattern in _DOCX_PARTS.items():
                n = _count(zf, "word/document.xml", pattern)
                if n is None:
                    return None
                out[key] = n
            out["headers"] = sum(1 for n in members if re.fullmatch(r"word/(header|footer)\d*\.xml", n))
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        return None
    return out


def _office_path(ev: ToolEvent, suffix: str) -> Optional[str]:
    if ev.replay:
        return None                 # the file may be gone or changed; never guess from history
    path = os.path.expanduser(str(ev.args.get("path") or ""))
    if not path.lower().endswith(suffix) or not os.path.isabs(path):
        return None
    obj = ev.json
    if obj is None or obj.get("extracted_document") is not True:
        return None
    return path


@rule("office-extraction-silent-loss", driver="tool", tools={"read_file"}, core_tells="nobody",
      status="fixture")
def office_clipped(ev: ToolEvent) -> Iterator[Obs]:
    """read_file extracts .xlsx and .docx with stdlib parsers: 5,000 rows per sheet, 256 columns,
    hidden sheets skipped; .docx keeps paragraphs only. Nothing in the result says so; the file's own
    XML gives the real size."""
    path = _office_path(ev, ".xlsx")
    if path:
        yield from _xlsx(path)
        return
    path = _office_path(ev, ".docx")
    if path:
        yield from _docx(path)


def _xlsx(path: str) -> Iterator[Obs]:
    info = probe_xlsx(path)
    if not info:
        return
    rows, cols, hidden = info["rows"], info["cols"], info["hidden_sheets"]
    if rows <= C.XLSX_MAX_ROWS and cols <= C.XLSX_MAX_COLS and not hidden:
        return
    parts = []
    if rows > C.XLSX_MAX_ROWS:
        parts.append(f"only the first {C.XLSX_MAX_ROWS:,} of {rows:,} rows per sheet")
    if cols > C.XLSX_MAX_COLS:
        parts.append(f"only the first {C.XLSX_MAX_COLS} of {cols} columns")
    if hidden:
        parts.append(f"{hidden} hidden sheet{'s' if hidden != 1 else ''} skipped")
    name = basename(path)
    yield Obs("office-extraction-silent-loss",
              numbers={"kind": "xlsx", "rows": rows, "rows_kept": min(rows, C.XLSX_MAX_ROWS), "cols": cols,
                       "hidden_sheets": hidden},
              subject=name, cause=path, recover=(Recover.names(name, ("execute_code", "terminal")),),
              severity="card" if rows > C.XLSX_MAX_ROWS or cols > C.XLSX_MAX_COLS else "digest",
              note=("read_file applied its v0.23.0 spreadsheet caps: " + "; ".join(parts)
                    + ". The text is partial; code execution can load the whole workbook."))


def _docx(path: str) -> Iterator[Obs]:
    info = probe_docx(path)
    if not info or not any(info.values()):
        return
    dropped = info["images"] + info["footnotes"] + info["endnotes"] + info["comments"] + info["headers"]
    parts = [f"{info[key]} {label}" for key, label in (
        ("tables", "tables flattened"), ("images", "images dropped"), ("footnotes", "footnotes dropped"),
        ("endnotes", "endnotes dropped"), ("comments", "comments dropped"),
        ("headers", "header/footer parts dropped")) if info[key]]
    name = basename(path)
    yield Obs("office-extraction-silent-loss", numbers={"kind": "docx", **info},
              subject=name, cause=path, recover=(Recover.names(name, ("execute_code", "terminal")),),
              severity="card" if dropped else "digest",
              note=("read_file kept paragraphs only (v0.23.0): " + "; ".join(parts)
                    + ". Code execution can read the rest."))
