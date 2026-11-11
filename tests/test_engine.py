"""End-to-end over the engine with a fake host: tool results in, card / note / ledger out."""

import json

from fine_print.engine import annotate

WEB_FOOTER = (
    "\n\n──────── [TRUNCATED] ────────\n"
    "Showing 11,250 chars (head) + 3,733 chars (tail) of 24,116 total clean characters.\n"
    "Full text saved to: /home/u/.hermes/cache/web/example.com-7cac558ea3.md\n"
    'To read the omitted middle: read_file path="/home/u/.hermes/cache/web/example.com-7cac558ea3.md" '
    "offset=115 limit=200  (the file is the complete page; raise/lower offset to page through it).\n"
    "─────────────────────────────"
)


def web_result(content="lorem ipsum " * 50 + WEB_FOOTER, url="https://example.com/a"):
    return json.dumps({"results": [{"url": url, "title": "t", "content": content, "error": None}]}, indent=2,
                      ensure_ascii=False)


def run_tool(fp, tool, args, result, n=1, turn="t1", session="s1"):
    return fp.tool_result(tool_name=tool, args=args, result=result, session_id=session, turn_id=turn,
                          tool_call_id=f"call{n}", status="ok")


def test_web_cut_raises_card_when_not_recovered(fp, host):
    assert run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result()) is None  # no note: core tells the model
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert card and "14,983 of 24,116 chars (62%)" in card
    assert "example.com" in card and "web.extract_char_limit 25000" in card
    assert host.posted == [card]
    events = fp.ledger.session_events("s1")
    assert len(events) == 1 and events[0]["surfaced"] and not events[0]["recovered"]


def test_web_cut_recovered_by_reading_saved_copy(fp, host):
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result(), n=1)
    run_tool(fp, "read_file", {"path": "/home/u/.hermes/cache/web/example.com-7cac558ea3.md", "offset": 115},
             json.dumps({"content": "x"}), n=2)
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli") is None
    assert host.posted == []
    assert fp.ledger.session_events("s1")[0]["recovered"]


def test_read_before_the_cut_is_not_recovery(fp, host):
    run_tool(fp, "read_file", {"path": "/x/example.com-7cac558ea3.md"}, json.dumps({"content": "x"}), n=1)
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result(), n=2)
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli") is not None


def test_same_cause_is_not_carded_twice_in_a_session(fp, host):
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result())
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result(), turn="t2")
    assert fp.turn_end(session_id="s1", turn_id="t2", platform="cli") is None
    assert len(host.posted) == 1 and len(fp.ledger.session_events("s1")) == 2


def test_search_page_full_gets_a_note_and_a_card(fp, host):
    result = json.dumps({"total_count": 50, "matches": [{"path": "a", "line": 1, "content": "x"}] * 50})
    out = run_tool(fp, "search_files", {"pattern": "needle"}, result)
    assert out is not None and out.startswith('{"_fine_print": "search_files returned a full page (50 entries')
    assert json.loads(out)["total_count"] == 50
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert "full page of 50" in card and "offset=50" in card and "Fine Print noted it for the model" in card
    assert fp.ledger.session_info("s1")["notes"] == 1


def test_search_continued_with_offset_is_recovered(fp, host):
    result = json.dumps({"total_count": 50, "matches": [1] * 50})
    run_tool(fp, "search_files", {"pattern": "needle"}, result, n=1)
    run_tool(fp, "search_files", {"pattern": "needle", "offset": 50}, json.dumps({"total_count": 70, "matches": [1] * 20}), n=2)
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli") is None


def test_notes_off_changes_no_tool_result(fp, host):
    host.settings["notes"] = False
    result = json.dumps({"total_count": 50, "matches": [1] * 50})
    assert run_tool(fp, "search_files", {"pattern": "needle"}, result) is None
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert card and "Hermes told nobody." in card and "noted it" not in card


def test_cards_auto_is_silent_on_messaging_platforms(fp, host):
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result())
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="telegram") is None
    assert host.posted == [] and len(fp.ledger.session_events("s1")) == 1


def test_muted_rule_logs_but_raises_no_card(fp, host):
    fp.ledger.mute("web_extract.head_tail_truncation")
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result())
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli") is None
    assert len(fp.ledger.session_events("s1")) == 1


def test_error_results_and_unknown_tools_are_ignored(fp, host):
    assert run_tool(fp, "web_extract", {"urls": ["u"]}, json.dumps({"error": "boom"})) is None
    assert run_tool(fp, "some_tool", {}, "plain text") is None
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli") is None
    assert fp.ledger.session_events("s1") == []


def test_never_annotates_tools_another_plugin_owns(fp, host):
    for tool in ("write_file", "patch", "skill_manage"):
        assert run_tool(fp, tool, {"path": "x"}, json.dumps({"total_count": 50, "matches": [1] * 50})) is None


def test_spill_detected_from_the_committed_row(fp, host):
    raw = "x" * 150_000
    run_tool(fp, "terminal", {"command": "big"}, raw)
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call1"}]},
        {"role": "tool", "tool_call_id": "call1", "content": (
            "<persisted-output>\nThis tool result was too large (150,000 characters, 146.5 KB).\n"
            "Full output saved to: /home/u/.hermes/cache/spillover/abc123.txt\nUse the read_file tool.\n\n"
            "Preview (first 1500 chars):\n" + "x" * 1500 + "\n</persisted-output>")},
        {"role": "assistant", "content": "done"},
    ]
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli", conversation_history=history)
    assert card and "150,000-char result was replaced by a 1,500-char preview" in card


def test_a_file_that_mentions_the_markers_is_not_a_signal(fp, host):
    text = json.dumps({"content": "1|<persisted-output> rescued_from [TRUNCATED] Showing 1 chars (head) + 1 chars (tail) "
                                  "of 9 total clean characters.\n2|Note: Image downscaled from 10x10 to 5x5 for vision; "
                                  "multiply any coordinates you report by 2.00", "total_lines": 2})
    assert run_tool(fp, "read_file", {"path": "/repo/research.md"}, text) is None
    history = [{"role": "user", "content": "q"}, {"role": "tool", "tool_call_id": "call1", "content": text}]
    assert fp.turn_end(session_id="s1", turn_id="t1", platform="cli", conversation_history=history) is None
    assert fp.ledger.session_events("s1") == []


def test_subagent_hits_fold_into_the_parent_card(fp, host):
    fp.subagent_start(parent_session_id="s1", child_session_id="c1")
    run_tool(fp, "web_extract", {"urls": ["https://example.com/a"]}, web_result(), session="c1", turn="ct")
    assert fp.turn_end(session_id="c1", turn_id="ct", platform="cli") is None and host.posted == []
    run_tool(fp, "web_extract", {"urls": ["https://example.org/b"]}, web_result(url="https://example.org/b"))
    card = fp.turn_end(session_id="s1", turn_id="t1", platform="cli")
    assert "1 in subagents" in card
    assert len(fp.ledger.session_events("s1")) == 2      # parent view includes the child's row


def test_a_failing_rule_is_switched_off_not_fatal(fp, host):
    from fine_print.engine import Registry, rule

    reg = Registry()

    @rule("boom", driver="tool", tools={"x"}, core_tells="nobody", registry=reg)
    def boom(ev):
        raise RuntimeError("bug")

    fp.registry = reg
    for i in range(5):
        assert run_tool(fp, "x", {}, "{}", n=i) is None
    assert reg.failed["boom"] == 3


def test_annotate_keeps_json_valid_and_bytes_intact():
    raw = '{\n  "results": [1, 2],\n  "s": "é"\n}'
    out = annotate(raw, "a note")
    assert json.loads(out) == {"_fine_print": "a note", "results": [1, 2], "s": "é"}
    assert out.endswith(raw[1:])
    assert annotate("{}", "n") == '{"_fine_print": "n"}'
    assert annotate("plain", "n") == "[fine-print: n]\nplain"
    assert len(json.loads(annotate('{"a": 1}', "x" * 900))["_fine_print"]) == 200
