"""FILES recovery and severity checks that the single-event fixtures cannot express."""

import json
from pathlib import Path

from fine_print.cards import TEXT, describe
from fine_print.engine import REGISTRY, Call, CommitRow, ToolEvent
import fine_print.rules  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_obs(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    event = ToolEvent(data["tool"], data["args"], data["result"])
    return [obs for obs in REGISTRY.run("tool", event.tool, event) if obs.rule_id == data["limit_id"]]


def test_long_line_recovery_requires_code_not_another_line_offset():
    found = _fixture_obs("read_file_long_line_clamp.json")
    assert len(found) == 1
    obs = found[0]
    assert obs.severity == "digest"              # 2 of 6 lines cut: the size of the loss is unknown, no card
    assert obs.numbers == {"marked": 2, "prefix_max": 2000, "lines": 6}
    assert not any(r.satisfied([Call("read_file", {"path": "long_lines.txt", "offset": 2})])
                   for r in obs.recover)
    for tool in ("execute_code", "terminal"):
        assert any(r.satisfied([Call(tool, {"code": "open('long_lines.txt').read()"})])
                   for r in obs.recover)


def test_terminal_window_recovers_only_when_the_saved_log_is_named_later():
    found = _fixture_obs("terminal_output_window.json")
    assert len(found) == 1
    obs = found[0]
    assert obs.severity == "card"
    assert 0 < obs.numbers["shown"] < obs.numbers["total"]
    data = json.loads((FIXTURES / "terminal_output_window.json").read_text())
    saved = json.loads(data["result"])["full_output_path"]
    assert any(r.satisfied([Call("search_files", {"path": saved, "pattern": "error"})]) for r in obs.recover)
    assert not any(r.satisfied([Call("write_file", {"path": saved, "content": "replacement"})]) for r in obs.recover)
    assert not any(r.satisfied([Call("terminal", {"command": "same command again"})]) for r in obs.recover)


def test_spill_read_has_one_owner_and_offset_retry_cannot_recover_it():
    data = json.loads((FIXTURES / "spill_json_dead_end_read.json").read_text())
    event = ToolEvent("read_file", data["args"], data["result"])
    found = REGISTRY.run("tool", "read_file", event)
    assert [o.rule_id for o in found] == ["spill-format-dead-end-v0.23"]
    obs = found[0]
    assert obs.severity == "card"
    for tool in ("read_file", "search_files"):
        assert not any(r.satisfied([Call(tool, {"path": data["args"]["path"], "offset": 2})]) for r in obs.recover)
    assert any(r.satisfied([Call("execute_code", {"code": repr(data["args"]["path"])})]) for r in obs.recover)


def test_kernel_timeout_is_digest_and_fresh_execution_cannot_restore_lost_state():
    found = _fixture_obs("files_code_timeout.json")
    assert len(found) == 1
    assert found[0].severity == "digest"
    assert found[0].recover == ()
    assert found[0].numbers == {"timeout": 300}


def test_identical_result_stub_is_commit_only_and_informational():
    data = json.loads((FIXTURES / "files_identical_second.json").read_text())
    assert not REGISTRY.run("tool", "terminal", ToolEvent("terminal", data["args"], data["result"]))
    row = CommitRow("terminal", data["args"], data["committed"], len(data["result"]))
    found = [o for o in REGISTRY.run("commit", "terminal", row) if o.rule_id == data["limit_id"]]
    assert len(found) == 1
    assert found[0].severity == "log"            # lossless within the turn: never a card, never counted
    assert not found[0].recover
    assert found[0].numbers["total"] == len(data["result"])


def test_hidden_match_probe_is_digest_and_reports_only_the_probed_subset():
    found = _fixture_obs("files_search_hidden_probe.json")
    assert len(found) == 1
    assert found[0].severity == "digest"
    assert found[0].numbers == {"shown": 0, "probe_matches": 2, "probe_files": 1}


def test_foreground_timeout_is_digest_not_a_capture_loss_card():
    found = _fixture_obs("files_terminal_timeout.json")
    assert len(found) == 1
    assert found[0].severity == "digest"
    assert not found[0].recover
    assert found[0].numbers == {"kind": "timeout", "timeout": 1}


def test_redaction_does_not_turn_a_visible_prefix_length_into_a_guessed_cap():
    found = _fixture_obs("files_long_line_redacted.json")
    assert len(found) == 1
    obs = found[0]
    assert obs.numbers == {"marked": 2, "prefix_max": 2000, "lines": 2}
    assert obs.severity == "card"                # every line the model received was cut
    line, _ = TEXT[obs.rule_id](obs.numbers, obs.subject, lambda key, default: default)
    assert "all 2 lines returned were cut" in line and "prefix" in line and "cut at" not in line


def test_spill_path_shapes_and_single_owner_for_a_clamped_read():
    data = json.loads((FIXTURES / "read_file_long_line_clamp.json").read_text())
    ids = {}
    for path in ("/home/dev/.hermes/cache/spillover/call_1.txt", "~/.hermes/cache/spillover/call_1.txt", "C:\\Users\\dev\\AppData\\Local\\hermes\\cache\\spillover\\call_1.txt",
                 "/home/dev/.hermes/cache/spillover/sub/call_1.txt", "/home/dev/cache/spillover-copy/call_1.txt",
                 "/home/dev/notes/cache/spillover/call_1.md", "/home/dev/long_lines.txt"):
        found = REGISTRY.run("tool", "read_file", ToolEvent("read_file", {"path": path}, data["result"]))
        ids[path] = [o.rule_id for o in found]
    assert ids["/home/dev/.hermes/cache/spillover/call_1.txt"] == ["spill-format-dead-end-v0.23"]
    assert ids["~/.hermes/cache/spillover/call_1.txt"] == ["spill-format-dead-end-v0.23"]
    assert ids["C:\\Users\\dev\\AppData\\Local\\hermes\\cache\\spillover\\call_1.txt"] == ["spill-format-dead-end-v0.23"]
    assert ids["/home/dev/long_lines.txt"] == ["long-line-clamps"]
    for path in list(ids)[3:6]:
        assert ids[path] == ["long-line-clamps"], path


def test_texts_use_the_catalogue_fix_where_none_is_given():
    for rule_id in ("long-line-clamps", "spill-format-dead-end-v0.23", "execute-code-timeout-call-cap"):
        line, fix = describe(rule_id, {"marked": 1, "prefix_max": 2000, "timeout": 300}, "x", lambda k, d: d)
        assert line and fix and not fix.startswith("see /limits")


def test_a_one_line_cached_web_page_has_one_owner():
    """read_file of a cached web copy that is one clamped line: the web rule reports it, the
    general long-line rule stays silent; a many-line cached page with a cut line is the general rule's."""
    data = json.loads((FIXTURES / "webmcp_web_recovery_clamped.json").read_text())
    found = REGISTRY.run("tool", "read_file", ToolEvent("read_file", data["args"], data["result"]))
    assert [o.rule_id for o in found] == ["web_extract.pdf_from_url"]
    many = json.loads(data["result"])
    many["total_lines"] = 40
    found = REGISTRY.run("tool", "read_file", ToolEvent("read_file", data["args"], json.dumps(many)))
    assert [o.rule_id for o in found] == ["long-line-clamps"]


def test_long_line_text_says_how_much_of_the_read_was_cut():
    say = lambda numbers: TEXT["long-line-clamps"](numbers, "a.json", lambda key, default: default)[0]
    assert "the only line returned was cut" in say({"marked": 1, "prefix_max": 2000, "lines": 1})
    assert "2 of 6 lines returned were cut" in say({"marked": 2, "prefix_max": 2000, "lines": 6})
    assert "1 of 40 lines returned was cut" in say({"marked": 1, "prefix_max": 2000, "lines": 40})
    assert "1 line came back cut" in say({"marked": 1, "prefix_max": 2000})        # rows logged before `lines` existed


def test_terminal_window_is_a_card_only_when_under_half_of_the_output_was_kept():
    data = json.loads((FIXTURES / "terminal_output_window.json").read_text())
    found = REGISTRY.run("tool", "terminal", ToolEvent("terminal", data["args"], data["result"]))
    assert [o.severity for o in found] == ["card"] and 2 * found[0].numbers["shown"] < found[0].numbers["total"]
    # the same capture with most of the output kept: 49,841 shown of 60,000
    obj = json.loads(data["result"])
    total, kept = obj["output_total_chars"], 60_000
    for key in ("output", "truncation_note"):
        obj[key] = obj[key].replace(f"{total:,}", f"{kept:,}").replace(f"{total - found[0].numbers['shown']:,}",
                                                                      f"{kept - found[0].numbers['shown']:,}")
    obj["output_total_chars"] = kept
    found = REGISTRY.run("tool", "terminal", ToolEvent("terminal", data["args"], json.dumps(obj)))
    assert [o.severity for o in found] == ["digest"]


def test_a_catalogue_fix_carries_its_timing_and_the_terminal_window_has_its_own_fix():
    cfg = lambda key, default: default
    _line, fix = describe("long-line-clamps", {"marked": 1, "prefix_max": 2000, "lines": 1}, "a.json", cfg)
    assert "tool_output.max_line_length" in fix and fix.endswith("Applies: gateway restart.")
    line, fix = describe("terminal-capture-window-and-timeout", {"shown": 49_925, "total": 349_055}, "terminal", cfg)
    assert "kept 49,925 of 349,055 chars" in line
    assert "tool_output.max_bytes" in fix and "timeout" not in fix and fix.endswith("Applies: gateway restart.")
    line, _fix = describe("long-line-clamps", {"marked": 1, "prefix_max": 2000, "lines": 1}, "a.json", cfg)
    assert line.endswith("the rest of that line was not returned.")
