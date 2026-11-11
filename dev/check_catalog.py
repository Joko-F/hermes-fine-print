#!/usr/bin/env python3
"""Validate fine_print/catalog.json.

Stdlib only. Exits non-zero and prints one line per failure.

    python3 dev/check_catalog.py [--source PATH] [--hermes PATH]

--source  research catalog (limits-catalog.json); id set is compared when present.
--hermes  Hermes checkout; config keys are checked against DEFAULT_CONFIG,
          which is read with ast (Hermes is never imported).
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "fine_print" / "catalog.json"

EXPECTED_COUNT = 75
TOPICS = {"images", "web", "files", "tools", "context", "model", "settings"}
KINDS = {"SAW", "MISSING", "SWAPPED", "SETTINGS", "FORGOT"}
DRIVERS = {"tool", "tool+commit", "commit", "wire", "response", "session",
           "compaction", "route", "reference"}
DETECTS = {"live", "heuristic", "reference"}
TOLD = {"nobody", "model", "user", "both"}
APPLIES = {"next call", "next session", "gateway restart", ""}

ENTRY_TYPES = {
    "id": str, "title": str, "topic": str, "kind": str, "driver": str,
    "detect": str, "what": str, "threshold": str, "told": str,
    "told_detail": str, "config_key": (str, type(None)), "fix": str,
    "applies": str, "issues": list, "code_refs": list,
}
LIMITS = {"title": 70, "what": 400, "threshold": 120, "told_detail": 160, "fix": 320}

FORBIDDEN = [
    re.compile(r"ignore\s+(?:all\s+|any\s+|the\s+|previous\s+|prior\s+|above\s+)*instructions", re.I),
    re.compile(r"disregard", re.I),
    re.compile(r"act\s+as\s+if", re.I),
    re.compile(r"without\s+restrictions", re.I),
    re.compile(r"without\s+limitations", re.I),
    re.compile(r"system\s+prompt\s+override", re.I),
    re.compile(r"developer\s+mode", re.I),
    re.compile(r"do\s+not\s+tell\s+the\s+user", re.I),
    re.compile(r"don'?t\s+tell\s+the\s+user", re.I),
    re.compile(r"you\s+are\s+now", re.I),
    re.compile(r"pretend", re.I),
    re.compile(r"output\s+the\s+system\s+prompt", re.I),
    re.compile(r"for\s+educational\s+purposes", re.I),
    re.compile(r"new\s+policy", re.I),
    re.compile(r"updated\s+guidelines", re.I),
    re.compile(r"revised\s+instructions", re.I),
    re.compile(r"when\s+no\s+one\s+is\s+watching", re.I),
    re.compile(r"<!--", re.I),
    re.compile(r"config\.yaml", re.I),
]

CONFIG_SET = re.compile(r"hermes config set\s+([A-Za-z0-9_.\-<>]+)")
ISSUE_RE = re.compile(r"^#\d+$")
SAFETY_OFF = re.compile(
    r"(redact_secrets\s+(false|off|0))|(approvals?\.\w+\s+(off|false))|(yolo)", re.I)


def fix_shape(fix: str) -> str:
    """Return 'a', 'c' or 'b' for the three allowed fix shapes."""
    if re.match(r"^`hermes config set [^`]+`", fix):
        return "a"
    if fix.startswith("Hardcoded in v0.23.0."):
        return "c"
    return "b"


# --- DEFAULT_CONFIG loading (no import of Hermes) -------------------------

def _find_assign(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name and node.value:
                return node.value
    return None


def _walk_dict(node: ast.AST, prefix: str, out: set) -> None:
    """Collect dotted string keys from every dict literal under node."""
    if not isinstance(node, ast.Dict):
        return
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            path = f"{prefix}.{k.value}" if prefix else k.value
            out.add(path)
            _walk_dict(v, path, out)


def _flatten(d: dict, prefix: str, out: set) -> None:
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        out.add(path)
        if isinstance(v, dict):
            _flatten(v, path, out)


def load_default_config_keys(hermes: Path) -> set[str]:
    candidates = [hermes / "hermes_cli" / "config_defaults.py",
                  hermes / "hermes_cli" / "config.py"]
    for path in candidates:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        value = _find_assign(tree, "DEFAULT_CONFIG")
        if value is None:
            continue
        keys: set[str] = set()
        try:
            _flatten(ast.literal_eval(value), "", keys)
            return keys
        except (ValueError, SyntaxError, TypeError):
            keys = set()
            _walk_dict(value, "", keys)
            if keys:
                return keys
    raise RuntimeError("DEFAULT_CONFIG not found under " + str(hermes))


# --- checks ---------------------------------------------------------------

def check(entries: list, source_ids: set | None, config_keys: set | None) -> list[str]:
    fails: list[str] = []

    def fail(eid, msg):
        fails.append(f"{eid}: {msg}")

    if len(entries) != EXPECTED_COUNT:
        fails.append(f"catalog: expected {EXPECTED_COUNT} entries, found {len(entries)}")
    ids = [e.get("id") if isinstance(e, dict) else None for e in entries]
    seen = set()
    for i in ids:
        if i in seen:
            fails.append(f"{i}: duplicate id")
        seen.add(i)
    if source_ids is not None:
        for i in sorted(source_ids - seen):
            fails.append(f"{i}: missing from catalog (present in source)")
        for i in sorted(x for x in seen - source_ids if x):
            fails.append(f"{i}: not in source catalog")

    # sorted by topic then id
    order = [(e.get("topic"), e.get("id")) for e in entries if isinstance(e, dict)]
    if order != sorted(order):
        fails.append("catalog: entries not sorted by topic then id")

    for e in entries:
        if not isinstance(e, dict):
            fails.append(f"catalog: non-object entry {e!r}")
            continue
        eid = e.get("id", "?")
        extra = set(e) - set(ENTRY_TYPES)
        if extra:
            fail(eid, f"unexpected keys {sorted(extra)}")
        for key, typ in ENTRY_TYPES.items():
            if key not in e:
                fail(eid, f"missing key {key}")
                continue
            if not isinstance(e[key], typ) or (typ is str and isinstance(e[key], bool)):
                fail(eid, f"key {key} has wrong type {type(e[key]).__name__}")
        if any(k not in e for k in ENTRY_TYPES):
            continue

        for key, enum in (("topic", TOPICS), ("kind", KINDS), ("driver", DRIVERS),
                          ("detect", DETECTS), ("told", TOLD), ("applies", APPLIES)):
            if isinstance(e[key], str) and e[key] not in enum:
                fail(eid, f"{key} {e[key]!r} not in {sorted(enum)}")
        for key, lim in LIMITS.items():
            if isinstance(e[key], str) and len(e[key]) > lim:
                fail(eid, f"{key} is {len(e[key])} chars, limit {lim}")
        for key in ("title", "what", "fix"):
            if isinstance(e[key], str) and not e[key].strip():
                fail(eid, f"{key} is empty")
        if isinstance(e["title"], str) and e["title"].endswith("."):
            fail(eid, "title ends with a period")
        if isinstance(e["code_refs"], list):
            if len(e["code_refs"]) > 4:
                fail(eid, "more than 4 code_refs")
            if not all(isinstance(x, str) for x in e["code_refs"]):
                fail(eid, "code_refs must be strings")
        if isinstance(e["issues"], list):
            for x in e["issues"]:
                if not (isinstance(x, str) and ISSUE_RE.match(x)):
                    fail(eid, f"bad issue ref {x!r}")

        # forbidden phrases in every string value
        for key, val in e.items():
            vals = val if isinstance(val, list) else [val]
            for v in vals:
                if isinstance(v, str):
                    for pat in FORBIDDEN:
                        if pat.search(v):
                            fail(eid, f"forbidden phrase /{pat.pattern}/ in {key}")

        # fix shape and config keys
        fix = e["fix"] if isinstance(e["fix"], str) else ""
        shape = fix_shape(fix)
        used = CONFIG_SET.findall(fix)
        if shape == "a" and not used:
            fail(eid, "fix shape (a) without a config key")
        if e["applies"] and e["config_key"] is None:
            fail(eid, "'applies' is set but config_key is null")
        if e["applies"] and shape == "c":
            fail(eid, "'applies' is set but the fix is hardcoded")
        if SAFETY_OFF.search(fix):
            fail(eid, "fix appears to disable a safety feature")
        ck = e["config_key"]
        if ck is not None and isinstance(ck, str) and config_keys is not None and ck not in config_keys:
            fail(eid, f"config_key {ck!r} not in DEFAULT_CONFIG")
        if config_keys is not None:
            for k in used:
                if k not in config_keys:
                    fail(eid, f"fix uses unknown config key {k!r}")
        if shape == "a" and used and ck is not None and used[0] != ck:
            fail(eid, f"fix key {used[0]!r} differs from config_key {ck!r}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", default=str(CATALOG))
    ap.add_argument("--source", default="/home/dev/hermes-fine-print/limits-catalog.json")
    ap.add_argument("--hermes", default="~/.hermes/hermes-agent")
    args = ap.parse_args()

    try:
        doc = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"catalog: cannot load {args.catalog}: {exc}")
        return 1
    fails: list[str] = []
    if not isinstance(doc, dict) or "entries" not in doc or not isinstance(doc["entries"], list):
        print("catalog: top level must be an object with an 'entries' list")
        return 1
    if doc.get("version") != "0.23.0":
        fails.append(f"catalog: version is {doc.get('version')!r}, expected '0.23.0'")

    source_ids = None
    src = Path(args.source).expanduser()
    if src.is_file():
        data = json.loads(src.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("entries", [])
        source_ids = {x["id"] for x in data}
    else:
        print(f"note: source {src} not found, id comparison skipped", file=sys.stderr)

    config_keys = None
    hermes = Path(args.hermes).expanduser()
    if hermes.is_dir():
        try:
            config_keys = load_default_config_keys(hermes)
        except RuntimeError as exc:
            fails.append(f"hermes: {exc}")
    else:
        print(f"note: {hermes} not found, config key check skipped", file=sys.stderr)

    fails += check(doc["entries"], source_ids, config_keys)
    for line in fails:
        print("FAIL " + line)
    if fails:
        return 1
    extra = f", {len(config_keys)} config keys known" if config_keys else ""
    print(f"OK: {len(doc['entries'])} entries{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
