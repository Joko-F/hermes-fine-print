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
