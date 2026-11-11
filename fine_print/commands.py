"""The ``/limits`` slash command: this session's hits, the reference for all catalogued limits,
the history report, the self-test and the off switches. Handlers return text; nothing is printed."""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import catalog
from . import constants as C
from .cards import describe
from .sheet import build_sheet

USAGE = ("Usage: /limits [last | topics | <topic> | explain <id> | report [days] | self-test | "
         "mute <id> | unmute <id> | notes on|off | sheet on|off | cards auto|on|off]")


def _state(event: dict) -> str:
    if event["recovered"]:
        return "the model followed up"
    return "shown as a card" if event["surfaced"] else "logged only"


class Commands:
    def __init__(self, fp: Any, set_setting: Callable[[str, Any], None],
                 report: Optional[Callable[[int], str]] = None, hermes_version: Callable[[], str] = lambda: "") -> None:
        self.fp, self.set_setting, self.report, self.hermes_version = fp, set_setting, report, hermes_version

    def handle(self, raw_args: str = "", session_id: str = "", **_: Any) -> str:
        words = (raw_args or "").split()
        cmd = words[0].lower() if words else ""
        rest = words[1:]
        if cmd in ("", "summary", "status"):
            return self.summary(session_id)
        if cmd == "last":
            return self.last(session_id)
        if cmd in ("topics", "list", "all"):
            return self.topics()
        if cmd in catalog.TOPICS:
            return self.topic(cmd)
        if cmd == "explain":
            return self.explain(" ".join(rest))
        if cmd == "report":
            return self._report(rest)
        if cmd in ("self-test", "selftest", "doctor"):
            return self.self_test()
        if cmd in ("mute", "unmute"):
            return self.mute(" ".join(rest), cmd == "mute")
        if cmd in ("notes", "sheet", "cards"):
            return self.switch(cmd, rest[0].lower() if rest else "")
        if cmd in ("help", "-h", "--help"):
            return USAGE
        found = catalog.get(cmd)
        return self.explain(cmd) if found else f"Unknown subcommand '{cmd}'.\n{USAGE}"

    # ── this session ───────────────────────────────────────────────────────────────────────────────
    def _accounting(self, session_id: str) -> str:
        info = self.fp.ledger.session_info(session_id) if session_id else {}
        notes_on, sheet_on = self.fp.notes_on(), self.sheet_on()
        sheet = len(build_sheet(self.fp.host.config)) if sheet_on else 0
        n = info.get("notes", 0)
        return ("Added to the model's context by Fine Print: "
                f"{info.get('note_chars', 0):,} chars of notes in {n} tool result{'s' if n != 1 else ''} this session "
                f"(notes {'on' if notes_on else 'off'}); a sheet of {sheet:,} chars per new session "
                f"(sheet {'on' if sheet_on else 'off'}). No LLM calls, no network. "
                "/limits notes off stops the notes at once; /limits sheet off applies to new sessions.")

    def sheet_on(self) -> bool:
        value = self.fp.host.setting("sheet", True)
        return value if isinstance(value, bool) else str(value).strip().lower() not in ("off", "false", "0", "no")

    def summary(self, session_id: str) -> str:
        events = self.fp.ledger.session_events(session_id) if session_id else []
        lines = []
        if not events:
            lines.append("Fine Print · no silent limits hit in this session so far.")
        else:
            counts = self.fp.ledger.session_counts(session_id)      # exact, the list below is capped
            total, shown, recovered = counts["events"], counts["shown"], counts["recovered"]
            quiet = total - shown - recovered
            parts = [f"{total} limit hit{'s' if total != 1 else ''}"]
            if shown:
                parts.append(f"{shown} shown as a card")
            if recovered:
                parts.append(f"{recovered} followed up by the model")
            if quiet > 0:
                parts.append(f"{quiet} logged only")
            lines.append("Fine Print · this session: " + " · ".join(parts))
            for event in events[:8]:
                line, _fix = describe(event["rule_id"], event["numbers"], event["subject"], self.fp.cfg_for(session_id))
                lines.append(f"• {line} [{_state(event)}]")
            if total > 8:
                lines.append(f"  … and {total - 8} earlier")
            lines.append("/limits last shows the newest one with its fix.")
        lines.append(self._accounting(session_id))
        total = len(catalog.entries())
        lines.append(f"{total} known limits of Hermes v{C.HERMES_VERSION}: /limits topics · "
                     "/limits explain <id> · /limits report · /limits self-test")
        return "\n".join(lines)

    def last(self, session_id: str) -> str:
        events = self.fp.ledger.session_events(session_id, limit=1) if session_id else []
        if not events:
            return "Fine Print · nothing recorded in this session yet."
        event = events[0]
        line, fix = describe(event["rule_id"], event["numbers"], event["subject"], self.fp.cfg_for(session_id))
        return f"{line} [{_state(event)}]\nFix: {fix}\nMore: /limits explain {event['rule_id']}"

    # ── reference ──────────────────────────────────────────────────────────────────────────────────
    def topics(self) -> str:
        lines = [f"Fine Print · {len(catalog.entries())} known limits of Hermes v{C.HERMES_VERSION}"]
        for topic in catalog.TOPICS:
            lines.append(f"• /limits {topic}: {len(catalog.by_topic(topic))}")
        lines.append("Then /limits explain <id>.")
        return "\n".join(lines)

    def topic(self, topic: str) -> str:
        items = catalog.by_topic(topic)
        if not items:
            return f"No catalogued limits under '{topic}'."
        lines = [f"Fine Print · {topic}: {len(items)} limits"]
        for entry in items:
            rule = self.fp.registry.get(entry["id"])
            mark = {"verified": "live", "fixture": "live, unverified"}.get(getattr(rule, "status", ""), "reference")
            lines.append(f"• {entry['id']}: {entry.get('title', '')} [{mark}]")
        lines.append("/limits explain <id> shows what happens and the fix.")
        return "\n".join(lines)

    def explain(self, limit_id: str) -> str:
        if not limit_id:
            return "Usage: /limits explain <id>   (ids: /limits topics)"
        entry = catalog.get(limit_id)
        if not entry:
            return f"No catalogued limit matches '{limit_id}'. Try /limits topics."
        rule = self.fp.registry.get(entry["id"])
        detector = {"verified": "live detector, verified on real history",
                    "fixture": "live detector, checked on fixtures only (logs, no cards)"}.get(
                        getattr(rule, "status", ""), "reference only, no detector in this version")
        told = {"nobody": "nobody", "model": "the model only", "user": "the user only",
                "both": "the model and the user"}.get(entry.get("told", ""), entry.get("told", ""))
        lines = [f"{entry['id']} · {entry.get('title', '')}", entry.get("what", "")]
        if entry.get("threshold"):
            lines.append(f"Threshold: {entry['threshold']}")
        lines.append(f"Hermes tells: {told}" + (f" ({entry['told_detail']})" if entry.get("told_detail") else ""))
        lines.append(f"Fix: {entry.get('fix', '')}" + (f" Applies: {entry['applies']}." if entry.get("applies") else ""))
        if entry.get("issues"):
            lines.append("Upstream: " + ", ".join(entry["issues"]))
        lines.append(f"Fine Print: {detector}.")
        return "\n".join(line for line in lines if line)

    # ── report / self-test ─────────────────────────────────────────────────────────────────────────
    def _report(self, rest: list[str]) -> str:
        if self.report is None:
            return "The history report is not available in this build."
        try:
            days = max(1, min(3650, int(rest[0]))) if rest else 30
        except ValueError:
            return "Usage: /limits report [days]"
        return self.report(days)

    def self_test(self) -> str:
        rules = self.fp.registry.all()
        ids = {e.get("id") for e in catalog.entries()}
        verified = [r for r in rules if r.status == "verified"]
        fixture = [r for r in rules if r.status != "verified"]
        covered = {r.id for r in rules}
        running = self.hermes_version()
        lines = [f"Fine Print self-test · formats pinned to Hermes v{C.HERMES_VERSION}"
                 + (f" · running v{running}" if running else " · running version unknown")]
        if not running:
            lines.append("  the running Hermes version could not be read: formats are unverified for it.")
        elif not running.startswith(C.HERMES_VERSION):
            lines.append(f"  formats unverified for v{running}: a rule whose format does not parse stays silent.")
        lines.append(f"  detectors: {len(verified)} verified (raise cards) · {len(fixture)} fixture-only (log only) · "
                     f"{len(ids - covered)} of {len(ids)} catalogued limits are reference-only")
        for rule in rules:
            failed = self.fp.registry.failed.get(rule.id, 0)
            state = ("OFF after 3 errors (until restart)" if failed >= 3
                     else (f"{failed} errors" if failed else "no errors"))
            lines.append(f"  {rule.status:<8} {rule.driver:<10} {rule.id} · core tells {rule.core_tells} · {state}")
        stats = self.fp.stats
        totals = self.fp.ledger.totals()
        lines.append(f"  this process: {stats['hooks']} hook calls · {stats['errors']} hook errors · "
                     f"{stats['cards']} cards · {stats['notes']} notes")
        lines.append(f"  ledger: {totals['events']} events in {totals['sessions']} sessions "
                     f"({totals['surfaced']} shown, {totals['recovered']} recovered); metadata only")
        return "\n".join(lines)

    # ── switches ───────────────────────────────────────────────────────────────────────────────────
    def mute(self, rule_id: str, on: bool) -> str:
        entry = catalog.get(rule_id) if rule_id else None
        rid = entry["id"] if entry else rule_id.strip()
        if not rid or (self.fp.registry.get(rid) is None and not entry):
            return "Usage: /limits mute <id>   (ids: /limits self-test)"
        self.fp.ledger.mute(rid, on)
        return (f"Muted {rid}: it is still logged, but raises no card." if on else f"Unmuted {rid}.")

    def switch(self, name: str, value: str) -> str:
        if name == "cards":
            if value not in ("auto", "on", "off"):
                return ("cards: " + str(self.fp.host.setting("cards", "auto"))
                        + "   (auto = local surfaces only; a notice on a messaging platform is a real message)")
            self.set_setting("cards", value)
            return f"Cards: {value}."
        if value not in ("on", "off"):
            current = self.fp.notes_on() if name == "notes" else self.sheet_on()
            return f"{name}: {'on' if current else 'off'}   (/limits {name} on|off)"
        self.set_setting(name, value == "on")
        if name == "notes":
            return ("Model notes on: one bracketed line in a tool result, only where Hermes tells nobody."
                    if value == "on" else "Model notes off: Fine Print changes no tool result.")
        return ("Sheet on: new sessions get the limits sheet in their system prompt."
                if value == "on" else "Sheet off: new sessions get no sheet (this session's prompt is already frozen).")
