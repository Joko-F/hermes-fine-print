"""Every fixture in tests/fixtures was produced by calling Hermes v0.23.0 core functions on synthetic
input. Each one names the limit it exercises and whether a detector must fire. The same rules must
agree on the raw result (live hook) and on the committed row (history replay)."""

import json

import pytest

from conftest import FIXTURES
from fine_print.engine import REGISTRY, CommitRow, ToolEvent
from fine_print.signals import strip_untrusted
import fine_print.rules  # noqa: F401

CASES = sorted(FIXTURES.glob("*.json"))


def _load(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    args = dict(data.get("args") or {})
    # office fixtures point at files generated next to them; make the path valid on any machine
    if isinstance(args.get("path"), str) and "/tests/fixtures/files/" in args["path"]:
        args["path"] = str(FIXTURES / "files" / args["path"].rsplit("/", 1)[-1])
    return data, args


def _hits(rule_id, tool, args, result, committed):
    rule = REGISTRY.get(rule_id)
    if rule.driver == "tool":
        return [o for o in REGISTRY.run("tool", tool, ToolEvent(tool, args, result)) if o.rule_id == rule_id]
    raw_len = len(result) if isinstance(result, str) else None
    row = CommitRow(tool, args, committed if committed is not None else result, raw_len, "call_1")
    return [o for o in REGISTRY.run("commit", tool, row) if o.rule_id == rule_id]


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_fixture(path):
    data, args = _load(path)
    rule_id = data.get("limit_id")
    if REGISTRY.get(rule_id) is None:
        pytest.skip(f"no detector for {rule_id} in this version (reference only)")
    found = _hits(rule_id, data["tool"], args, data.get("result"), data.get("committed"))
    if data["expect"] == "hit":
        assert found, f"{path.name}: {rule_id} must fire"
    else:
        assert not found, f"{path.name}: {rule_id} must stay silent, got {[o.numbers for o in found]}"


@pytest.mark.parametrize("path", [p for p in CASES if p.name.startswith(("web_extract_", "web_search_"))],
                         ids=lambda p: p.stem)
def test_history_replay_agrees_with_live(path):
    """A committed web row is wrapped as untrusted data; unwrapped, it must give the same numbers."""
    data, args = _load(path)
    rule_id = data.get("limit_id")
    if REGISTRY.get(rule_id) is None or not isinstance(data.get("committed"), str):
        pytest.skip("no detector or no committed shape")
    live = _hits(rule_id, data["tool"], args, data["result"], None)
    replay = [o for o in REGISTRY.run("tool", data["tool"],
                                      ToolEvent(data["tool"], args, strip_untrusted(data["committed"]), replay=True))
              if o.rule_id == rule_id]
    assert [o.numbers for o in live] == [o.numbers for o in replay]


def test_web_cut_numbers_match_ground_truth():
    data, args = _load(FIXTURES / "web_extract_cut_default.json")
    obs = _hits("web_extract.head_tail_truncation", "web_extract", args, data["result"], None)[0]
    facts = data["facts"]
    flat = json.dumps(facts)
    assert str(obs.numbers["total"]) in flat and str(obs.numbers["head"]) in flat and str(obs.numbers["tail"]) in flat


def test_repository_files_as_read_file_results_produce_nothing():
    """P2: this repository is full of the marker strings. Reading it back must raise no signal."""
    from conftest import ROOT
    for path in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.md")) + CASES[:12]:
        text = path.read_text(encoding="utf-8", errors="replace")[:60_000]
        body = "\n".join(f"{i}|{line}" for i, line in enumerate(text.splitlines(), 1))
        result = json.dumps({"content": body, "total_lines": body.count("\n") + 1, "truncated": False})
        assert REGISTRY.run("tool", "read_file", ToolEvent("read_file", {"path": str(path)}, result)) == []
        assert REGISTRY.run("commit", "read_file", CommitRow("read_file", {"path": str(path)}, result)) == []
