"""Regression tests for the second audit round (2026-11-11): a read that fetched nothing is not a
follow-up, one page gives one piece of advice, formats the first S2 version missed."""

import json

from conftest import FIXTURES
from fine_print.cards import describe
from fine_print.engine import REGISTRY, CommitRow, ToolEvent
from fine_print.rules.webmcp import WEB_CACHE_PATH
from fine_print.signals import parse_web_footer
from test_engine import run_tool


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def tool_obs(tool, args, result):
    return REGISTRY.run("tool", tool, ToolEvent(tool, args, result))


# ── a read that returned nothing new is not a follow-up ───────────────────────────────────────────
def test_a_past_eof_read_of_the_saved_page_does_not_clear_the_web_card(fp, host):
    """The footer's own advice (read_file offset=2) fails on a one-line saved page: the model got
    nothing, so the cut page must still raise its card."""
    web, eof = load("web_extract_pdf_oneline"), load("webmcp_web_recovery_eof")
    saved = parse_web_footer(json.loads(web["result"])["results"][0]["content"])["path"]
    run_tool(fp, "web_extract", web["args"], web["result"], n=1)
    run_tool(fp, "read_file", {"path": saved, "offset": 2, "limit": 200}, eof["result"], n=2)
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert card is not None and "web_extract · example.com" in card
    events = {e["rule_id"]: e for e in fp.ledger.session_events("s1")}
    assert not events["web_extract.head_tail_truncation"]["recovered"]


def test_a_clamped_read_of_the_saved_page_does_not_clear_the_web_card(fp, host):
    web, clamped = load("web_extract_pdf_oneline"), load("webmcp_web_recovery_clamped")
    saved = parse_web_footer(json.loads(web["result"])["results"][0]["content"])["path"]
    run_tool(fp, "web_extract", web["args"], web["result"], n=1)
    run_tool(fp, "read_file", {"path": saved}, clamped["result"], n=2)
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli") is not None


def test_a_clamped_read_of_a_spill_file_does_not_clear_the_spill_card(fp, host):
    """read_file on a spilled one-line JSON returns 2,000 chars of it: that is the dead end the
    spill stub's advice leads to, not a recovery."""
    read = load("spill_json_dead_end_read")
    found = tool_obs("read_file", read["args"], read["result"])
    assert [o.rule_id for o in found] == ["spill-format-dead-end-v0.23"] and found[0].dead_end
    path = read["args"]["path"]
    block = ("<persisted-output>\nThis tool result was too large (150,000 characters, 146.5 KB).\n"
             f"Full output saved to: {path}\nUse the read_file tool.\n\nPreview (first 1500 chars):\nabc\n</persisted-output>")
    spill = REGISTRY.run("commit", "terminal", CommitRow("terminal", {}, block, 150_000, "c1"))[0]
    run_tool(fp, "read_file", read["args"], read["result"], n=2)
    st = fp.buffer.get("s1", "t1")
    assert st.calls[-1].ok is False
    assert not any(r.satisfied(st.calls) for r in spill.recover)


def test_an_empty_read_is_not_a_follow_up_but_a_real_one_is(fp, host):
    from fine_print.engine import Recover
    names = Recover.names("out.txt")
    run_tool(fp, "read_file", {"path": "/c/out.txt", "offset": 900}, json.dumps({"content": "", "total_lines": 3}), n=1)
    st = fp.buffer.get("s1", "t1")
    assert not names.satisfied(st.calls)
    run_tool(fp, "read_file", {"path": "/c/out.txt"}, json.dumps({"content": "1|a\n2|b", "total_lines": 3}), n=2)
    assert names.satisfied(st.calls)


# ── one page, one piece of advice ─────────────────────────────────────────────────────────────────
def test_a_page_over_the_stored_copy_cap_is_not_sent_to_its_saved_copy():
    over = load("webmcp_stored_over_cap")
    found = {o.rule_id: o for o in tool_obs("web_extract", over["args"], over["result"])}
    assert set(found) == {"web_extract.head_tail_truncation", "web-extract-stored-copy-2m-cap"}
    assert found["web-extract-stored-copy-2m-cap"].severity == "digest"       # never a second bullet for one page
    cut = found["web_extract.head_tail_truncation"]
    _line, fix = describe(cut.rule_id, cut.numbers, cut.subject, lambda key, default: default)
    assert "saved copy stops at 2,000,000 chars" in fix and "from its saved copy" not in fix


# ── formats ───────────────────────────────────────────────────────────────────────────────────────
def test_web_cache_path_accepts_the_legacy_folder_and_windows_separators():
    for path in ("/home/u/.hermes/cache/web/example.com-6b86b07739.md",
                 "C:\\Users\\u\\AppData\\Local\\hermes\\cache\\web\\example.com-6b86b07739.md",
                 "C:/Users/u/AppData/Local/hermes/cache/web/example.com-6b86b07739.md",
                 "/home/u/.hermes/web_cache/example.com-6b86b07739.md"):
        assert WEB_CACHE_PATH.search(path), path
    for path in ("/home/u/notes/web/example.com-6b86b07739.md", "/home/u/.hermes/cache/web/readme.md",
                 "/home/u/.hermes/cache/webx/example.com-6b86b07739.md"):
        assert not WEB_CACHE_PATH.search(path), path


def test_hidden_file_probe_is_seen_after_an_earlier_warning():
    hint = ("0 matches in visible files, but 3 match(es) in 2 hidden or gitignored file(s): "
            "/r/.env, /r/.git/x — these are excluded by default.")
    for warning in (hint, "Skipped 2 unreadable path(s) with restricted permissions. " + hint):
        found = tool_obs("search_files", {"pattern": "KEY", "path": "/r"}, json.dumps({"total_count": 0, "warning": warning}))
        assert [o.numbers["probe_files"] for o in found if o.rule_id == "search-skips-hidden-ignored"] == [2], warning
    quoted = json.dumps({"total_count": 0, "warning": "x" + hint})
    assert not [o for o in tool_obs("search_files", {"pattern": "KEY"}, quoted) if o.rule_id == "search-skips-hidden-ignored"]


def test_identical_stub_length_excludes_advice_appended_after_it():
    data = load("files_identical_second")
    stub = data["committed"]
    longer = stub + "\n\n[Tool loop warning: identical_call; count=3; " + "w" * 300 + "]"
    short = [o for o in REGISTRY.run("commit", "terminal", CommitRow("terminal", data["args"], stub, 800, "c2"))
             if o.rule_id == "identical-result-stub"]
    long = [o for o in REGISTRY.run("commit", "terminal", CommitRow("terminal", data["args"], longer, 800, "c2"))
            if o.rule_id == "identical-result-stub"]
    assert short and long and short[0].numbers["shown"] == long[0].numbers["shown"] <= len(stub)


def test_report_sentences_agree_in_number():
    from fine_print.report import _lines
    one = {"numbers": {"shown": 3_704, "total": 50_000}, "recovered": False, "later": False}
    page = {"numbers": {"shown": 50, "offset": 0}, "recovered": False, "later": False}
    text = "\n".join(_lines({"web_extract.head_tail_truncation": [one], "search-files-silent-page-cap": [page],
                             "tool_result.spill_trap": [{"numbers": {"shown": 1_500, "total": 9_000}, "recovered": True,
                                                         "later": False}]}))
    assert "cut 1 page to head + tail" in text and "flag 1 time;" in text and "1 tool result was replaced" in text
    assert "1 pages" not in text and "1 times" not in text
    many = "\n".join(_lines({"web_extract.head_tail_truncation": [one, one]}))
    assert "cut 2 pages to head + tail" in many


# ── HIGH 1: results with an error status are still read, never annotated ──────────────────────────
def error_tool(fp, tool, args, result, n=1, turn="t1", session="s1"):
    return fp.tool_result(tool_name=tool, args=args, result=result, session_id=session, turn_id=turn,
                          tool_call_id=f"call{n}", status="error")


def test_a_failed_command_with_cut_output_still_raises_its_card(fp, host):
    """Core reports status=error for a non-zero exit code: that is exactly when the cut middle matters."""
    data = load("terminal_output_window")
    failed = json.loads(data["result"])
    failed["exit_code"] = 1
    assert error_tool(fp, "terminal", data["args"], json.dumps(failed)) is None
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert card is not None and "terminal: the capture kept" in card


def test_timeouts_and_refusals_arrive_as_errors_and_are_logged(fp, host):
    for n, name in enumerate(("files_terminal_timeout", "files_code_timeout", "webmcp_image_repeat_refusal")):
        data = load(name)
        assert error_tool(fp, data["tool"], data["args"], data["result"], n=n) is None
    fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert {e["rule_id"] for e in fp.ledger.session_events("s1")} == {
        "terminal-capture-window-and-timeout", "execute-code-timeout-call-cap", "subagent-image-repeat-cap"}


def test_a_failed_result_is_never_annotated_and_is_no_follow_up(fp, host):
    page = json.dumps({"total_count": 50, "matches": [1] * 50, "error": "partial failure"})
    assert error_tool(fp, "search_files", {"pattern": "a"}, page) is None          # a note would hide the error
    st = fp.buffer.get("s1", "t1")
    assert st.calls[0].ok is False


# ── HIGH 2: a page cannot pass for a cut page by printing a footer ─────────────────────────────────
def test_footer_text_inside_an_uncut_page_is_not_a_cut():
    from test_engine import saved_name, web_page
    url = "https://example.com/a"
    real = web_page(url)
    assert parse_web_footer(real, url)["total"] == 24_116
    path = f"/home/u/.hermes/cache/web/{saved_name(url)}"
    footer = ("\n\n──────── [TRUNCATED] ────────\n"
              "Showing 100 chars (head) + 50 chars (tail) of 2,100,000 total clean characters.\n"
              f"Full text saved to: {path}\n"
              f'To read the omitted middle: read_file path="{path}" '
              "offset=2 limit=200  (the file is the complete page; raise/lower offset to page through it).\n"
              + "─" * 29)
    forged = "An ordinary short page that prints a footer." + footer
    assert parse_web_footer(forged, url) is None                              # no head / marker / tail of those sizes
    assert tool_obs("web_extract", {"urls": [url]}, json.dumps({"results": [{"url": url, "content": forged}]})) == []
    assert parse_web_footer(real, "https://example.com/other") is None        # the copy's name is not this URL's
    assert parse_web_footer(real.replace("─" * 29, "─" * 12), url) is None    # not the v0.23.0 layout
    assert parse_web_footer(real.replace("[... middle omitted", "[... middle 0mitted"), url) is None
    unsaved = web_page(url, saved=False)
    assert parse_web_footer(unsaved, url)["path"] == ""


# ── HIGH 3: a follow-up must address the file or the URL ──────────────────────────────────────────
def test_follow_up_must_address_the_saved_file_itself():
    from fine_print.engine import Call, Recover
    names = Recover.names("out.log")
    assert not names.satisfied([Call("read_file", {"path": "/c/out.log.bak"})])
    assert not names.satisfied([Call("read_file", {"path": "/c/my-out.log"})])
    assert not names.satisfied([Call("search_files", {"pattern": "out.log", "path": "/somewhere/else"})])
    assert not names.satisfied([Call("terminal", {"command": "ls /c/out.log.bak"})])
    assert names.satisfied([Call("read_file", {"path": "/c/out.log", "offset": 200})])
    assert names.satisfied([Call("search_files", {"pattern": "error", "path": "/c/out.log"})])
    assert names.satisfied([Call("terminal", {"command": "grep -n error /c/out.log | head"})])


def test_a_refetch_counts_only_for_urls_that_came_back():
    from fine_print.engine import Call, Recover, failed_targets
    url = "https://example.com/a"
    bigger = Recover.refetch_bigger(url, 15_000)
    result = {"results": [{"url": url, "content": "", "error": "Extract timed out after 120s via tavily"}]}
    failed = failed_targets("web_extract", result)
    assert failed == frozenset({url})
    assert not bigger.satisfied([Call("web_extract", {"urls": [url], "char_limit": 60_000}, failed=failed)])
    assert bigger.satisfied([Call("web_extract", {"urls": [url], "char_limit": 60_000})])
    six = [f"https://example.com/{i}" for i in range(6)]
    assert not Recover.refetch([six[5]]).satisfied([Call("web_extract", {"urls": six})])      # only five are passed on
    assert Recover.refetch([six[5]]).satisfied([Call("web_extract", {"urls": [six[5]]})])


# ── HIGH 5: a rotated session id ──────────────────────────────────────────────────────────────────
def test_rotation_before_the_first_turn_closes_keeps_one_session(fp, host):
    from test_engine import web_result
    fp.subagent_start(parent_session_id="old", child_session_id="kid")
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result(), session="old", n=1)
    fp.compaction(session_id="new", old_session_id="old", kind="summarize", reason="post_tool",
                  before_tokens=90_000, after_tokens=9_000, messages_removed=4)
    page = json.dumps({"total_count": 50, "matches": [1] * 50})
    run_tool(fp, "search_files", {"pattern": "a"}, page, session="old", n=2)      # a hook that still carries the old id
    fp.turn_end(session_id="new", turn_id="t1", platform="cli")
    assert fp.ledger.session_info("new") and not fp.ledger.session_info("old")
    rules = {e["rule_id"] for e in fp.ledger.session_events("new")}
    assert {"web_extract.head_tail_truncation", "search-files-silent-page-cap"} <= rules
    assert fp.ledger.session_events("old") == []
    assert fp.parents["kid"] == "new"
    run_tool(fp, "web_extract", {"urls": ["https://example.org/k"]}, web_result(url="https://example.org/k"),
             session="kid", turn="kt")
    fp.turn_end(session_id="kid", turn_id="kt", platform="cli")
    assert fp.child_hits == {"new": 1}


def test_rotation_moves_note_accounting_and_subagent_rows(fp, host):
    page = json.dumps({"total_count": 50, "matches": [1] * 50})
    assert run_tool(fp, "search_files", {"pattern": "a"}, page, session="old") is not None
    fp.turn_end(session_id="old", turn_id="t1", platform="cli")
    fp.ledger.touch_session("kid", parent_id="old")
    before = fp.ledger.session_info("old")
    assert before["notes"] == 1
    fp.compaction(session_id="new", old_session_id="old", kind="summarize", reason="manual",
                  before_tokens=9, after_tokens=3, messages_removed=2)
    after = fp.ledger.session_info("new")
    assert after["notes"] == 1 and after["note_chars"] == before["note_chars"] and not fp.ledger.session_info("old")
    assert fp.ledger._read("SELECT parent_id FROM sessions WHERE id = 'kid'") == [("new",)]


def test_a_hook_holding_the_old_half_of_a_merged_turn_is_not_lost(fp, host):
    from test_engine import web_result
    old_state = fp.buffer.get("old", "t1")
    fp.buffer.get("new", "t1")                      # hooks already arrived under the new id
    fp.buffer.rename_session("old", "new")
    assert old_state.closed and old_state.session_id == "new"
    fp.buffer._turns[("stale", "t1")] = old_state   # the slow hook still writes through its old reference
    fp.buffer._renamed["old"] = "stale"
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result(), session="old")
    assert [e["rule_id"] for e in fp.ledger.session_events("new")] == ["web_extract.head_tail_truncation"]


# ── HIGH 6: one key for the notes of one result ───────────────────────────────────────────────────
def test_two_notes_share_one_key_and_an_existing_key_is_left_alone(fp, host):
    from fine_print.engine import annotate
    out = annotate('{"a": 1}', ["first note.", "second note."], True)
    assert out.count('"_fine_print"') == 1 and json.loads(out) == {"_fine_print": "first note. second note.", "a": 1}
    urls = [f"https://example.com/{i}" for i in range(7)]
    page = json.dumps({"total_count": 50, "matches": [1] * 50, "_fine_print": "something else wrote this"})
    assert run_tool(fp, "search_files", {"pattern": "a"}, page) is None
    fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert "Fine Print noted it for the model" not in host.posted[0]
    noted = run_tool(fp, "web_extract", {"urls": urls}, json.dumps({"results": []}), turn="t2")
    assert json.loads(noted)["_fine_print"].startswith("web_extract takes only the first 5 URLs of a call")


# ── MEDIUM 7-9, 11, 13, 15 ────────────────────────────────────────────────────────────────────────
def test_a_literal_marker_line_does_not_make_every_line_cut():
    content = "1|a short line that only quotes the marker ... [truncated]\n2|" + "x" * 2000 + "... [truncated]"
    found = tool_obs("read_file", {"path": "/w/notes.md"}, json.dumps({"content": content, "truncated_lines": True,
                                                                       "total_lines": 2}))
    assert [(o.severity, o.numbers["marked"], o.numbers["cut"]) for o in found] == [("digest", 2, 1)]
    one = "1|" + "x" * 2000 + "... [truncated]"
    found = tool_obs("read_file", {"path": "/w/a.json"}, json.dumps({"content": one, "truncated_lines": True,
                                                                     "total_lines": 1}))
    assert [(o.severity, o.numbers["cut"]) for o in found] == [("card", 1)]


def test_url_cap_text_does_not_claim_that_the_first_five_were_fetched():
    urls = [f"https://example.com/{i}" for i in range(6)]
    errors = {"results": [{"url": u, "content": "", "error": "Extract timed out after 120s via tavily"} for u in urls[:5]]}
    obs = [o for o in tool_obs("web_extract", {"urls": urls}, json.dumps(errors)) if o.rule_id == "web_extract.url_cap_5"][0]
    line, _fix = describe(obs.rule_id, obs.numbers, obs.subject, lambda key, default: default)
    assert "fetched" not in obs.note and "fetched" not in line and len(obs.note) <= 200
    assert "were not requested" in obs.note and "passed on" in line


def test_terminal_text_knows_that_the_saved_log_is_capped():
    say = lambda total: describe("terminal-capture-window-and-timeout", {"shown": 49_925, "total": total}, "terminal",
                                 lambda key, default: default)[0]
    assert "Hermes saved the stream to a log file" in say(349_055)
    assert "the log file Hermes saved stops at 5,000,000 chars" in say(6_000_000)


def test_a_failed_summary_call_is_attributed_to_its_own_session_only(fp, host):
    fp.aux_call(aux_task="compression", error="timeout", error_type="TimeoutError", session_id="A")
    fp.compaction(session_id="B", kind="summarize", reason="manual", before_tokens=9, after_tokens=3, messages_removed=2)
    fp.turn_end(session_id="B", turn_id="t1", platform="cli")
    assert "deterministic-fallback-summary" not in {e["rule_id"] for e in fp.ledger.session_events("B")}
    fp.compaction(session_id="A2", old_session_id="A", kind="summarize", reason="manual", before_tokens=9,
                  after_tokens=3, messages_removed=2)
    fp.turn_end(session_id="A2", turn_id="t1", platform="cli")
    assert "deterministic-fallback-summary" in {e["rule_id"] for e in fp.ledger.session_events("A2")}
    fp.aux_call(aux_task="compression", error="timeout", session_id="")           # nobody's: never attributed
    assert "" not in fp._summary_calls


def test_a_path_never_reaches_the_ledger_as_a_label(fp, host):
    from fine_print.ledger import clean_subject
    assert clean_subject("/home/alice/private/model.gguf") == "model.gguf"
    assert clean_subject("C:\\Users\\alice\\models\\m.gguf") == "m.gguf"
    assert clean_subject("file:///home/alice/m.gguf") == "m.gguf"
    assert clean_subject("anthropic/claude-sonnet-5-5") == "anthropic/claude-sonnet-5-5"
    assert clean_subject("tavily -> exa") == "tavily -> exa" and clean_subject("en.wikipedia.org") == "en.wikipedia.org"
    fp.ledger.touch_session("s9", model="/home/alice/private/model.gguf")
    assert fp.ledger.session_info("s9")["model"] == "model.gguf"


def test_side_maps_are_bounded_and_cleaned_on_delete(fp, host):
    from fine_print.core import SIDE_MAP_MAX
    for i in range(SIDE_MAP_MAX + 50):
        fp.subagent_start(parent_session_id="p", child_session_id=f"c{i}")
        fp.api_response(session_id=f"c{i}", turn_id="t", model="m", finish_reason="stop", context_length=1000)
    assert len(fp.parents) == SIDE_MAP_MAX and len(fp.context_length) == SIDE_MAP_MAX
    assert "c0" not in fp.parents and f"c{SIDE_MAP_MAX + 49}" in fp.parents
    fp.session_deleted(session_id=f"c{SIDE_MAP_MAX + 49}")
    assert f"c{SIDE_MAP_MAX + 49}" not in fp.parents and f"c{SIDE_MAP_MAX + 49}" not in fp.context_length
