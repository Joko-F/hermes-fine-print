"""Checks on everything a user, a model or the install scanner reads: phrases, budgets, and that
advice names real config keys (the catalog's keys are validated against Hermes by dev/check_catalog.py)."""

import json
import re

from conftest import FIXTURES, ROOT
from fine_print import constants as C
from fine_print.engine import REGISTRY, ToolEvent
from fine_print.sheet import build_sheet
import fine_print.rules  # noqa: F401

# Phrases the install scanner treats as prompt injection (tools/skills_guard.py, tools/threat_patterns.py).
INJECTION = [
    r"ignore\s+(?:\w+\s+){0,3}instructions", r"disregard\s+(?:your|all|any)", r"act\s+as\s+(?:if|though)\s+you",
    r"(?:respond|answer|reply)\s+without\s+(?:\w+\s+){0,2}(?:restrictions|limitations|filters|safety)",
    r"system\s+prompt\s+override", r"developer\s+mode", r"do\s+not\s+(?:\w+\s+){0,2}tell\s+(?:\w+\s+){0,2}the\s+user",
    r"you\s+are\s+(?:\w+\s+){0,3}now\b", r"pretend\s+(?:you\s+are|to\s+be)", r"output\s+(?:\w+\s+){0,2}(?:system|initial)\s+prompt",
    r"for\s+educational\s+purposes\s+only", r"when\s+no\s+one\s+is\s+watching", r"<!--",
    r"you\s+have\s+been\s+(?:\w+\s+){0,2}(?:updated|patched)",
]
SHIPPED = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".yaml", ".json", ".txt"}
           and ".git" not in p.parts and "__pycache__" not in p.parts
           and p.name not in {"test_texts.py", "test_catalog.py", "check_catalog.py"}
           and "fixtures" not in p.parts]


def test_no_injection_phrases_in_shipped_text():
    bad = []
    for path in SHIPPED:
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in INJECTION:
            for m in re.finditer(pattern, text, re.I):
                bad.append(f"{path.relative_to(ROOT)}: {m.group(0)!r}")
    assert not bad, bad


def test_only_documented_hermes_imports():
    """P1: the plugin package touches Hermes through three documented modules, all imported lazily."""
    allowed = {"hermes_cli.config", "plugins.plugin_storage", "hermes_constants"}
    stdlib_or_self = re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.M)
    seen = set()
    for path in (ROOT / "fine_print").rglob("*.py"):
        for name in stdlib_or_self.findall(path.read_text(encoding="utf-8")):
            top = name.split(".")[0]
            if top in {"hermes_cli", "plugins", "hermes_constants", "tools", "agent", "gateway", "tui_gateway",
                       "hermes_state", "run_agent", "model_tools"}:
                seen.add(name)
    assert seen <= allowed, seen - allowed


def test_no_network_subprocess_or_llm_surface():
    banned = re.compile(r"\b(?:import\s+(?:subprocess|socket|requests|httpx|urllib\.request)|ctx\.llm|side_question|"
                        r"os\.system|register_middleware|register_tool)\b")
    for path in list((ROOT / "fine_print").rglob("*.py")) + [ROOT / "__init__.py"]:
        assert not banned.search(path.read_text(encoding="utf-8")), path


def test_sheet_budget_and_content():
    sheet = build_sheet(lambda key, default: default)
    assert len(sheet) <= C.SHEET_MAX_CHARS
    for number in ("1568", "262,144", "15,000", "5,000", "50"):
        assert number in sheet


def test_every_note_fits_its_budget():
    longest = 0
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        args = dict(data.get("args") or {})
        if isinstance(args.get("path"), str) and "/tests/fixtures/files/" in args["path"]:
            args["path"] = str(FIXTURES / "files" / args["path"].rsplit("/", 1)[-1])
        for obs in REGISTRY.run("tool", data["tool"], ToolEvent(data["tool"], args, data.get("result"))):
            if obs.note:
                longest = max(longest, len(obs.note))
                assert len(obs.note) <= C.NOTE_MAX_CHARS, (path.name, len(obs.note), obs.note)
    assert longest > 0


def test_card_advice_names_only_catalogued_config_keys():
    catalog = json.loads((ROOT / "fine_print" / "catalog.json").read_text(encoding="utf-8"))
    known = {e["config_key"] for e in catalog["entries"] if e["config_key"]}
    for e in catalog["entries"]:
        known.update(re.findall(r"hermes config set ([a-z_.]+)", e["fix"]))
    used = set(re.findall(r"hermes config set ([a-z_.]+)", (ROOT / "fine_print" / "cards.py").read_text()))
    assert used and used <= known, used - known


def test_readme_lists_every_detector_and_the_table_is_fresh():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for rule in REGISTRY.all():
        assert f"`{rule.id}`" in readme, rule.id
    import subprocess
    import sys
    assert subprocess.run([sys.executable, str(ROOT / "dev" / "readme_table.py"), "--check"]).returncode == 0


def test_manifest_declares_exactly_the_registered_hooks():
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    declared = set(re.findall(r"^  - ([a-z_]+)$", manifest.split("provides_hooks:")[1].split("config_schema:")[0], re.M))
    registered = set(re.findall(r"register_hook\(\"([a-z_]+)\"", (ROOT / "fine_print" / "plugin.py").read_text()))
    assert declared == registered
