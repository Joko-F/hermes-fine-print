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
from ..signals import (basename, clean_token, data_url_info, iter_image_urls, iter_text_parts, parse_scale_note,
                       parse_web_footer, safe_label, url_host)


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
            # the budget is the call's char_limit, else the configured default; never below what was shown
            budget = max(asked_limit or C.WEB_EXTRACT_CHAR_LIMIT, shown)
            recover += (Recover.refetch_bigger(url, budget),)
        yield Obs("web_extract.head_tail_truncation",
                  numbers={"shown": shown, "total": total, "head": foot["head"], "tail": foot["tail"],
                           "saved": int(bool(saved))},
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
    rescued, fallback = data.get("rescued_from"), data.get("fallback_from")
    if isinstance(rescued, str) and rescued:
        kind, failed = "keyless", clean_token(rescued)       # the keyless free ring answered
    elif isinstance(fallback, str) and fallback:
        kind, failed = "managed", clean_token(fallback)      # managed primary -> managed fallback (billed)
    else:
        return
    served = clean_token(data.get("served_by")) or "another backend"
    web = data.get("web")
    count = len(web) if isinstance(web, list) else -1
    yield Obs("web_search.rescue_nested_error",
              numbers={"results": count, "failed": failed, "served_by": served, "kind": kind},
              subject=f"{failed} -> {served}", cause=f"{kind}:{failed}")


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
    # core reads offset+limit lines and never one more; what fills a page depends on the mode
    # (tools/file_operations_search.py:460-476): files_only slices the page out of the lines read,
    # count returns one entry per file read (total_count is the SUM of matches there, not a page size),
    # content reports total_count = matches read so far.
    if mode == "files_only":
        files = obj.get("files")
        full = isinstance(files, list) and len(files) == limit
    elif mode == "count":
        counts = obj.get("counts")
        full = isinstance(counts, dict) and len(counts) == offset + limit
    else:
        total = obj.get("total_count")
        full = isinstance(total, int) and not isinstance(total, bool) and total == offset + limit
    if not full:
        return
    pattern, path = str(args.get("pattern", "")), str(args.get("path", "") or "")
    glob = str(args.get("file_glob", "") or "")
    seen = offset + limit
    # A small page the model asked for itself is usually a deliberate sample: log it and tell the
    # model, but keep the card for pages of the default size or larger.
    yield Obs("search-files-silent-page-cap",
              numbers={"shown": limit, "offset": offset, "mode": str(mode)[:12]},
              subject=str(mode)[:12], cause=f"{pattern}\0{path}\0{glob}",
              recover=(Recover.search_more(pattern, path, seen, glob, "content"),),
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
    yield Obs("native-embed-downscale-1568", numbers=numbers, subject=safe_label(image) or "image",
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
_FIRST_ROW = re.compile(rb"<row[\s>].*?</row>", re.S)
_CELL_REF = re.compile(rb"<c\b[^>]*?\br=\"([A-Z]+)\d+\"")
_CELL = re.compile(rb"<c[\s>/]")
_SCAN_MAX = 32 * 1024 * 1024          # decompressed bytes scanned per office file at most (all members)
_META_MAX = 4 * 1024 * 1024           # workbook.xml / its rels above this are not read at all


def _col_index(letters: bytes) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ch - 64)
    return n


class _Budget:
    """One decompressed-bytes allowance per file, so a detector never does unbounded work in a hook."""

    def __init__(self, total: int = _SCAN_MAX) -> None:
        self.left = total

    def take(self, zf: zipfile.ZipFile, name: str) -> bool:
        size = zf.getinfo(name).file_size
        if size > self.left:
            return False
        self.left -= size
        return True


def _scan(zf: zipfile.ZipFile, name: str, pattern: "re.Pattern[bytes]", budget: _Budget) -> Optional[list]:
    """Every match of ``pattern`` in a zip member, streamed in one pass; None when the member does
    not fit the file's budget (the caller then stays silent instead of guessing)."""
    if not budget.take(zf, name):
        return None
    found, tail = [], b""
    with zf.open(name) as fh:
        while True:
            chunk = fh.read(1 << 20)
            data = tail + chunk
            if not chunk:
                found.extend(m.group(0) for m in pattern.finditer(data))
                return found
            cut = max(0, len(data) - 64)        # a match starting in the last 64 bytes is taken next round
            found.extend(m.group(0) for m in pattern.finditer(data) if m.start() < cut)
            tail = data[cut:]


def _small(zf: zipfile.ZipFile, name: str, members: set) -> bytes:
    if name not in members or zf.getinfo(name).file_size > _META_MAX:
        return b""
    return zf.read(name)


def _first_row_cols(zf: zipfile.ZipFile, name: str) -> int:
    """Columns of a sheet that records no <dimension>: the widest cell reference in its first row
    (the header), from the first 256 KB only. 0 means unknown."""
    with zf.open(name) as fh:
        head = fh.read(262_144)
    m = _FIRST_ROW.search(head)
    if not m:
        return 0
    refs = _CELL_REF.findall(m.group(0))
    return max(map(_col_index, refs)) if refs else len(_CELL.findall(m.group(0)))


def probe_xlsx(path: str) -> Optional[dict]:
    """Rows and columns of the largest VISIBLE sheet and the number of hidden sheets, from the
    workbook's own XML (read-only). Rows come from ``<dimension>`` when the writer recorded one, else
    from counting ``<row>`` elements. None when the file is unreadable or too large to count."""
    try:
        with zipfile.ZipFile(path) as zf:
            members = set(zf.namelist())
            book = _small(zf, "xl/workbook.xml", members)
            rels = _small(zf, "xl/_rels/workbook.xml.rels", members)
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
            budget = _Budget()
            for name in visible[:64]:
                with zf.open(name) as fh:
                    head = fh.read(4096)
                m = _DIMENSION.search(head)
                if m and m.group(2):
                    n_rows, n_cols = int(m.group(2)), _col_index(m.group(1))
                else:
                    counted = _scan(zf, name, _ROW, budget)
                    if counted is None:
                        continue
                    n_rows, n_cols = len(counted), _first_row_cols(zf, name)
                known = True
                rows, cols = max(rows, n_rows), max(cols, n_cols)
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        return None
    return {"rows": rows, "cols": cols, "hidden_sheets": hidden, "sheets": len(visible)} if known else None


_DOCX_TAGS = {b"<w:tbl>": "tables", b"<w:drawing>": "images", b"<w:pict>": "images",
              b"<w:footnoteReference": "footnotes", b"<w:endnoteReference": "endnotes",
              b"<w:commentReference": "comments"}
_DOCX_ANY = re.compile(rb"<w:tbl>|<w:drawing>|<w:pict>|<w:footnoteReference\b|<w:endnoteReference\b|"
                       rb"<w:commentReference\b")


def probe_docx(path: str) -> Optional[dict]:
    """What read_file's paragraph-only extraction leaves out of a .docx: table structure, images,
    footnotes, endnotes, comments, headers and footers. One pass over the document's own XML."""
    try:
        with zipfile.ZipFile(path) as zf:
            members = zf.namelist()
            if "word/document.xml" not in members:
                return None
            hits = _scan(zf, "word/document.xml", _DOCX_ANY, _Budget())
            if hits is None:
                return None
            out = {"tables": 0, "images": 0, "footnotes": 0, "endnotes": 0, "comments": 0}
            for hit in hits:
                out[_DOCX_TAGS[hit]] += 1
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
