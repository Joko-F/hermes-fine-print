"""Static checks for fine_print/catalog.json.

Needs only pytest and the catalog file; no Hermes checkout, no research source.
"""
import json
import re
from pathlib import Path

import pytest

CATALOG = Path(__file__).parent.parent / "fine_print" / "catalog.json"

TOPICS = {"images", "web", "files", "tools", "context", "model", "settings"}
KINDS = {"SAW", "MISSING", "SWAPPED", "SETTINGS", "FORGOT"}
DRIVERS = {"tool", "tool+commit", "commit", "wire", "response", "turn", "session",
           "compaction", "route", "reference"}
DETECTS = {"live", "heuristic", "reference"}
TOLD = {"nobody", "model", "user", "both"}
APPLIES = {"next call", "next session", "gateway restart", ""}
TYPES = {
    "id": str, "title": str, "topic": str, "kind": str, "driver": str,
    "detect": str, "what": str, "threshold": str, "told": str,
    "told_detail": str, "config_key": (str, type(None)), "fix": str,
    "applies": str, "issues": list, "code_refs": list,
}
LIMITS = {"title": 70, "what": 400, "threshold": 120, "told_detail": 160, "fix": 320}
FORBIDDEN = [
    r"ignore\s+(?:all\s+|any\s+|the\s+|previous\s+|prior\s+|above\s+)*instructions",
    r"disregard", r"act\s+as\s+if", r"without\s+restrictions",
    r"without\s+limitations", r"system\s+prompt\s+override", r"developer\s+mode",
    r"do\s+not\s+tell\s+the\s+user", r"don'?t\s+tell\s+the\s+user", r"you\s+are\s+now",
    r"pretend", r"output\s+the\s+system\s+prompt", r"for\s+educational\s+purposes",
    r"new\s+policy", r"updated\s+guidelines", r"revised\s+instructions",
    r"when\s+no\s+one\s+is\s+watching", r"<!--", r"config\.yaml",
]


@pytest.fixture(scope="module")
def doc():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def entries(doc):
    return doc["entries"]


def test_top_level(doc):
    assert doc["version"] == "0.23.0"
    assert isinstance(doc["entries"], list)


def test_entry_count(entries):
    assert len(entries) == 80


def test_unique_ids(entries):
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))


def test_sorted_by_topic_then_id(entries):
    keys = [(e["topic"], e["id"]) for e in entries]
    assert keys == sorted(keys)


def test_required_keys_and_types(entries):
    for e in entries:
        assert set(e) == set(TYPES), e["id"]
        for key, typ in TYPES.items():
            assert isinstance(e[key], typ), f"{e['id']}.{key}"


def test_enums(entries):
    for e in entries:
        assert e["topic"] in TOPICS, e["id"]
        assert e["kind"] in KINDS, e["id"]
        assert e["driver"] in DRIVERS, e["id"]
        assert e["detect"] in DETECTS, e["id"]
        assert e["told"] in TOLD, e["id"]
        assert e["applies"] in APPLIES, e["id"]


def test_length_limits(entries):
    for e in entries:
        for key, limit in LIMITS.items():
            assert len(e[key]) <= limit, f"{e['id']}.{key} is {len(e[key])} > {limit}"
        assert e["title"] and not e["title"].endswith("."), e["id"]
        assert e["what"].strip() and e["fix"].strip(), e["id"]
        assert len(e["code_refs"]) <= 4, e["id"]


def test_issue_refs(entries):
    for e in entries:
        for ref in e["issues"]:
            assert re.fullmatch(r"#\d+", ref), (e["id"], ref)


def test_fix_shape(entries):
    for e in entries:
        fix = e["fix"]
        if fix.startswith("`"):
            assert fix.startswith("`hermes config set "), e["id"]
            assert e["config_key"] is not None, e["id"]
        if e["applies"]:
            assert e["config_key"] is not None, e["id"]
            assert not fix.startswith("Hardcoded"), e["id"]


def test_no_hand_edit_or_safety_off_advice(entries):
    for e in entries:
        text = " ".join(str(v) for v in e.values()).lower()
        assert "redact_secrets false" not in text, e["id"]
        assert "edit config" not in text, e["id"]


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_forbidden_phrases_absent(entries, pattern):
    rx = re.compile(pattern, re.I)
    raw = CATALOG.read_text(encoding="utf-8")
    assert not rx.search(raw), f"{pattern} found in catalog.json"
