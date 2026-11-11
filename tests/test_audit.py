"""Regression tests for the findings of the independent audit (2026-11-11): follow-up inference,
ledger privacy, formats the first version missed, honest delivery state."""

import json
import zipfile

from fine_print.engine import REGISTRY, Call, CommitRow, Recover, ToolEvent
from fine_print.rules.tool import probe_docx, probe_xlsx
from fine_print.signals import decode_stored, parse_lost_tail, parse_persisted, parse_scale_note, safe_label
from test_engine import run_tool, web_result


def tool_obs(tool, args, result):
    return REGISTRY.run("tool", tool, ToolEvent(tool, args, result))


# ── 1. a follow-up must be a successful call of a tool that can fetch the missing part ─────────────
def test_naming_the_saved_file_in_a_search_or_a_failed_read_is_not_a_follow_up():
    names = Recover.names("example.com-7cac558ea3.md")
    assert not names.satisfied([Call("web_search", {"query": "example.com-7cac558ea3.md"})])
    assert not names.satisfied([Call("read_file", {"path": "/c/example.com-7cac558ea3.md"}, ok=False)])
    assert names.satisfied([Call("read_file", {"path": "/c/example.com-7cac558ea3.md"})])
    assert names.satisfied([Call("terminal", {"command": "sed -n 100,200p /c/example.com-7cac558ea3.md"})])


def test_a_different_search_is_not_a_continuation():
    more = Recover.search_more("needle", "/repo", 50, "*.py", "content")
    assert not more.satisfied([Call("search_files", {"pattern": "needle", "path": "/repo", "offset": 50})])
    assert not more.satisfied([Call("search_files", {"pattern": "needle", "path": "/repo", "file_glob": "*.py",
                                                     "target": "files", "offset": 50})])
    assert more.satisfied([Call("search_files", {"pattern": "needle", "path": "/repo", "file_glob": "*.py",
                                                 "offset": 50})])


def test_refetch_must_clearly_exceed_the_budget_that_cut_the_page():
    again = Recover.refetch_bigger("https://example.com/a", 15_000)
    assert not again.satisfied([Call("web_extract", {"urls": ["https://example.com/a"], "char_limit": 14_984})])
    assert not again.satisfied([Call("web_extract", {"urls": ["https://example.com/a"], "char_limit": 15_500})])
    assert again.satisfied([Call("web_extract", {"urls": ["https://example.com/a"], "char_limit": 40_000})])


def test_a_failed_read_of_the_saved_copy_still_raises_the_card(fp, host):
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result(), n=1)
    run_tool(fp, "read_file", {"path": "/c/example.com-7cac558ea3.md"}, json.dumps({"error": "not found"}), n=2)
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli") is not None


# ── 2. the ledger keeps labels and numbers, never query strings or free text ───────────────────────
def test_image_url_query_never_reaches_the_ledger(fp, host):
    assert safe_label("https://example.com/shots/image.png?token=PRIVATE_QUERY#frag") == "image.png"
    assert safe_label("data:image/png;base64,AAAA") == "image"
    assert safe_label("/home/u/My Folder/scan 1.png") == "scan 1.png"
    text = ("Image loaded.\n\nQuestion: q\n\nNote: Image downscaled from 3840x2160 to 960x540 for vision; "
            "multiply any coordinates you report by 4.00 to map back to the original image.\n[screenshot]")
    run_tool(fp, "vision_analyze", {"image_url": "https://example.com/shots/image.png?token=PRIVATE_QUERY"}, text)
    fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    rows = fp.ledger._read("SELECT subject, cause_hash, numbers_json FROM events")
    assert rows and "PRIVATE_QUERY" not in json.dumps(rows) and rows[0][0] == "image.png"


def test_ledger_drops_free_text_from_numbers(fp, host):
    result = json.dumps({"success": True, "data": {"web": [], "served_by": "exa", "rescued_from": "tav ily; DROP <x>",
                                                   "backend_error": "secret text"}})
    run_tool(fp, "web_search", {"query": "private words"}, result)
    fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    blob = json.dumps(fp.ledger._read("SELECT subject, numbers_json FROM events"))
    assert "private words" not in blob and "secret text" not in blob and "<x>" not in blob and "DROP" in blob


# ── 5-7, 13. formats ──────────────────────────────────────────────────────────────────────────────
def test_count_mode_with_all_matches_in_one_file_is_not_a_full_page():
    one_file = json.dumps({"total_count": 50, "counts": {"one.txt": 50}})
    assert tool_obs("search_files", {"pattern": "x", "output_mode": "count"}, one_file) == []
    fifty = json.dumps({"total_count": 61, "counts": {f"f{i}.txt": 1 for i in range(49)} | {"g.txt": 12}})
    assert len(tool_obs("search_files", {"pattern": "x", "output_mode": "count"}, fifty)) == 1


def test_scale_note_with_separate_x_and_y_factors():
    note = ("Note: Image downscaled from 3001x1999 to 750x499 for vision; multiply any x coordinates you report by "
            "4.00 and any y coordinates by 4.01 to map back to the original image.")
    assert parse_scale_note(note) == {"orig_w": 3001, "orig_h": 1999, "w": 750, "h": 499}
    assert parse_scale_note("1|Note: Image downscaled from 10x10 to 5x5 for vision; multiply any coordinates") is None


def test_managed_fallback_is_not_called_keyless(fp, host):
    result = json.dumps({"success": True, "data": {"web": [{"url": "u"}], "served_by": "firecrawl",
                                                   "fallback_from": "managed_primary"}})
    obs = tool_obs("web_search", {"query": "q"}, result)[0]
    assert obs.numbers["kind"] == "managed"
    run_tool(fp, "web_search", {"query": "q"}, result)
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert "managed fallback (firecrawl)" in card and "keyless" not in card


def test_spill_path_with_spaces_and_the_lost_tail_count():
    block = ("<persisted-output>\nThis tool result was too large (150,000 characters, 146.5 KB).\n"
             "Full output saved to: /home/user/My Folder/output 1.txt\nUse the read_file tool.\n\n"
             "Preview (first 1500 chars):\nabc\n</persisted-output>")
    assert parse_persisted(block)["path"] == "/home/user/My Folder/output 1.txt"
    lost = "abc\n\n[Truncated: tool response was 120,000 chars. Full output could not be saved to sandbox.]"
    assert parse_lost_tail(lost) == {"total": 120_000, "shown": 3}
    obs = REGISTRY.run("commit", "terminal", CommitRow("terminal", {}, block, 150_000, "c1"))[0]
    assert obs.recover[0].key == "output 1.txt"


# ── 3. stored structured content ──────────────────────────────────────────────────────────────────
def test_stored_multimodal_row_is_decoded_for_replay():
    parts = [{"type": "text", "text": "Image loaded.\n\nNote: Image downscaled from 3840x2160 to 960x540 for vision; "
                                      "multiply any coordinates you report by 4.00 to map back to the original image."},
             {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]
    stored = "\x00json:" + json.dumps(parts)
    text = decode_stored(stored)
    assert isinstance(text, str) and "base64" not in text
    assert len(tool_obs("vision_analyze", {"image_url": "/tmp/a.png"}, text)) == 1
    assert decode_stored("plain") == "plain" and decode_stored("\x00json:{broken") == "\x00json:{broken"


# ── 9, 16. delivery state is recorded as it happened ──────────────────────────────────────────────
def test_a_card_that_could_not_be_posted_is_not_recorded_as_shown(fp, host):
    host.post = lambda text: False
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result())
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli") is None
    assert not fp.ledger.session_events("s1")[0]["surfaced"]
    host.post = lambda text: True
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result(), turn="t2")
    assert fp.turn_end(session_id="s1", turn_id="t2", platform="cli") is not None      # shown now, not suppressed


def test_one_bullet_for_five_pages_uses_one_card_of_the_quota(fp, host):
    pages = [{"url": f"https://example.com/{i}", "title": "t", "content": json.loads(web_result())["results"][0]["content"],
              "error": None} for i in range(5)]
    run_tool(fp, "web_extract", {"urls": [p["url"] for p in pages]}, json.dumps({"results": pages}))
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert "(+4 more like it)" in card
    assert fp.ledger.surfaced_counts("s1") == {"web_extract.head_tail_truncation": 1}
    assert len(fp.ledger.surfaced_causes("s1")) == 5


def test_note_budget_spent_means_the_card_does_not_claim_a_note(fp, host):
    page = json.dumps({"total_count": 50, "matches": [1] * 50})
    for i, pattern in enumerate(("a", "b", "c")):
        out = run_tool(fp, "search_files", {"pattern": pattern}, page, n=i)
        assert (out is not None) == (i < 2)                       # two notes per turn
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert "(+2 more like it)" in card
    assert fp.ledger.session_counts("s1") == {"events": 3, "shown": 3, "recovered": 0}


def test_a_hook_that_arrives_after_the_turn_closed_is_logged_not_lost(fp, host):
    st = fp.buffer.get("s1", "t1")
    fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert st.closed
    fp.buffer._turns[("s1", "t1")] = st          # the late hook still holds the old state
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result())
    assert len(fp.ledger.session_events("s1")) == 1 and host.posted == []


# ── 12, 14. office probes ─────────────────────────────────────────────────────────────────────────
def _col(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def test_xlsx_without_dimension_still_reports_its_columns(tmp_path):
    cells = "".join(f'<c r="{_col(i)}1" t="inlineStr"><is><t>h{i}</t></is></c>' for i in range(1, 301))
    sheet = f'<worksheet><sheetData><row r="1">{cells}</row><row r="2"><c r="A2"><v>1</v></c></row></sheetData></worksheet>'
    path = tmp_path / "wide.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    info = probe_xlsx(str(path))
    assert info["rows"] == 2 and info["cols"] == 300 and info["hidden_sheets"] == 0
    result = json.dumps({"content": "1|x", "extracted_document": True})
    obs = tool_obs("read_file", {"path": str(path)}, result)
    assert obs and obs[0].numbers["cols"] == 300 and "256 of 300 columns" in obs[0].note


def test_office_probe_declines_a_file_over_its_scan_budget(tmp_path, monkeypatch):
    import fine_print.rules.tool as tool
    path = tmp_path / "big.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<w:tbl>" * 1000)
    assert probe_docx(str(path))["tables"] == 1000
    monkeypatch.setattr(tool, "_SCAN_MAX", 100)
    monkeypatch.setattr(tool._Budget.__init__, "__defaults__", (100,))
    assert probe_docx(str(path)) is None


def test_hook_work_stays_small_on_large_results():
    """P6: a 5 MB image envelope and a 300 KB JSON result must cost milliseconds, not a parse of megabytes."""
    import base64
    import time
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (960).to_bytes(4, "big") + (540).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    url = "data:image/png;base64," + base64.b64encode(png + b"\x00" * 3_700_000).decode()
    envelope = {"_multimodal": True, "content": [
        {"type": "text", "text": "Note: Image downscaled from 3840x2160 to 960x540 for vision; multiply any "
                                 "coordinates you report by 4.00 to map back to the original image."},
        {"type": "image_url", "image_url": {"url": url}}]}
    t0 = time.perf_counter()
    obs = tool_obs("vision_analyze", {"image_url": "/tmp/a.png"}, envelope)
    image_ms = (time.perf_counter() - t0) * 1000
    assert obs[0].numbers["sent_bytes"] > 3_000_000 and obs[0].numbers["w"] == 960
    big = json.dumps({"total_count": 50, "matches_text": "x" * 300_000})
    t0 = time.perf_counter()
    assert len(tool_obs("search_files", {"pattern": "x"}, big)) == 1
    json_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    assert tool_obs("terminal", {"command": "x"}, "y" * 5_000_000) == []
    plain_ms = (time.perf_counter() - t0) * 1000
    assert image_ms < 50 and json_ms < 50 and plain_ms < 5, (image_ms, json_ms, plain_ms)


def test_limit_advice_depends_on_the_spill_threshold_of_the_session(fp, host):
    from fine_print.cards import spill_threshold
    assert spill_threshold(0) == 38_400 and spill_threshold(64_000) == 38_400
    assert spill_threshold(1_000_000) == 100_000 and spill_threshold(8_000) == 8_000
    long_page = web_result().replace("24,116 total", "50,000 total")
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, long_page)
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert "would push the result over the spill threshold" in card and "config set" not in card
    fp.api_response(session_id="s2", turn_id="t1", model="m", finish_reason="stop", context_length=1_000_000)
    run_tool(fp, "web_extract", {"urls": ["https://example.com/b"]}, long_page.replace("/a", "/b"), session="s2")
    card = fp.turn_end(session_id="s2", turn_id="t1", platform="cli")
    assert "hermes config set web.extract_char_limit 55000" in card


def test_a_line_states_the_fact_and_only_the_card_bullet_speaks_of_follow_ups(fp, host):
    """/limits tags a hit with its state, so the line itself must not say "no follow-up" (a hit the
    model did follow up would then contradict its own tag)."""
    from fine_print.cards import describe
    numbers = {"shown": 14_983, "total": 24_116, "head": 11_000, "tail": 3_983, "saved": 1}
    line, _fix = describe("web_extract.head_tail_truncation", numbers, "example.com", lambda key, default: default)
    assert "follow-up" not in line and line.endswith("the middle was cut.")
    line, _fix = describe("tool_result.spill_trap", {"shown": 1_500, "total": 150_000}, "terminal", lambda k, d: d)
    assert "seen" not in line
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result())
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert "the middle was cut. No follow-up was seen this turn. Hermes told the model, not you." in card
    page = json.dumps({"total_count": 50, "matches": [1] * 50})
    run_tool(fp, "search_files", {"pattern": "a"}, page, session="s2")
    card = fp.turn_end(session_id="s2", turn_id="t1", platform="cli")
    assert card.count("No follow-up was seen this turn.") == 1
