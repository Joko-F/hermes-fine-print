"""Rule registry, per-turn buffer and the two dispatch points every detector goes through.

    drivers (hooks) -> rules -> observations -> turn buffer -> policy -> card | model note | ledger

The same rule functions run live (hook payloads) and over stored history (``report.py``), so a card and
``/limits report`` cannot disagree. Nothing here imports Hermes: the host (config, posting, storage)
is injected, which is also what lets the test suite run on plain Python.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from . import constants as C
from .signals import json_object

logger = logging.getLogger("fine_print")

_UNSET = object()


# ── events ─────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Call:
    """One tool call of a turn, live or replayed from history."""
    tool: str
    args: dict
    tool_call_id: str = ""

    def flat_args(self) -> str:
        try:
            return json.dumps(self.args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(self.args)


@dataclass
class ToolEvent:
    """What a tool-driver rule sees: the tool's own output, before spill, wrapping and budgets."""
    tool: str
    args: dict
    result: Any                      # str, or the multimodal envelope dict
    session_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""
    status: str = "ok"
    replay: bool = False             # True when built from a stored row by the history driver
    _json: Any = field(default=_UNSET, repr=False, compare=False)

    @property
    def text(self) -> Optional[str]:
        return self.result if isinstance(self.result, str) else None

    @property
    def json(self) -> Optional[dict]:
        if self._json is _UNSET:
            self._json = json_object(self.text)
        return self._json

    @property
    def is_error(self) -> bool:
        if self.status not in ("ok", ""):
            return True
        obj = self.json
        return bool(obj is not None and obj.get("error") and len(obj) <= 2)


@dataclass
class CommitRow:
    """What a commit-driver rule sees: one tool row as it was committed to the conversation."""
    tool: str
    args: dict
    content: Any                     # committed content (str, or list parts)
    raw_len: Optional[int] = None    # length of the raw str result seen by the tool driver, if any
    tool_call_id: str = ""


# ── recovery predicates ────────────────────────────────────────────────────────────────────────────
def _num(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Recover:
    """A declarative answer to "did the model get the missing part later in the turn?"."""
    kind: str
    key: str = ""
    extra: tuple = ()

    @staticmethod
    def names(fragment: str, tools: Iterable[str] = ()) -> "Recover":
        """A later call whose arguments name ``fragment`` (a saved file's basename): read_file,
        search_files, terminal or execute_code on that file all count. ``tools`` narrows which
        tools count (e.g. only code execution can recover a clipped spreadsheet)."""
        return Recover("names", fragment, tuple(tools))

    @staticmethod
    def region(image: str) -> "Recover":
        return Recover("region", str(image))

    @staticmethod
    def search_more(pattern: str, path: str, seen: int) -> "Recover":
        return Recover("search_more", str(pattern), (str(path), int(seen)))

    @staticmethod
    def refetch(urls: Iterable[str]) -> "Recover":
        return Recover("refetch", "", tuple(str(u) for u in urls))

    @staticmethod
    def refetch_bigger(url: str, char_limit: int) -> "Recover":
        return Recover("refetch_bigger", str(url), (int(char_limit),))

    def satisfied(self, later: list[Call]) -> bool:
        if self.kind == "names":
            return bool(self.key) and any(self.key in c.flat_args() for c in later
                                          if not self.extra or c.tool in self.extra)
        if self.kind == "region":
            return any(c.tool == "vision_analyze" and c.args.get("region")
                       and str(c.args.get("image_url", "")) == self.key for c in later)
        if self.kind == "search_more":
            path, seen = self.extra
            for c in later:
                if c.tool != "search_files" or str(c.args.get("pattern", "")) != self.key:
                    continue
                if str(c.args.get("path", "") or "") != path:
                    continue
                offset = _num(c.args.get("offset"), 0)
                limit = _num(c.args.get("limit"), C.SEARCH_DEFAULT_LIMIT)
                if offset >= seen or offset + limit > seen:
                    return True
            return False
        if self.kind == "refetch":
            wanted = set(self.extra)
            for c in later:
                if c.tool == "web_extract":
                    wanted -= {str(u) for u in (c.args.get("urls") or ())}
            return not wanted
        if self.kind == "refetch_bigger":
            for c in later:
                if c.tool == "web_extract" and self.key in {str(u) for u in (c.args.get("urls") or ())}:
                    if _num(c.args.get("char_limit"), 0) > self.extra[0]:
                        return True
            return False
        return False


# ── observations ───────────────────────────────────────────────────────────────────────────────────
@dataclass
class Obs:
    """One measured case of a limit biting. ``numbers`` are plain ints/floats/short strings only."""
    rule_id: str
    numbers: dict
    subject: str = ""                # short label safe to show and store: a host, a basename, a tool
    cause: str = ""                  # dedup key within a session (stored hashed)
    recover: tuple = ()              # alternatives; any satisfied => recovered
    note: Optional[str] = None       # model-facing line; used only when the rule's core_tells is "nobody"
    severity: str = "card"           # card | digest | log
    # set by the engine
    call_index: int = -1
    tool_call_id: str = ""
    recovered: bool = False
    surfaced: bool = False


# ── rules ──────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Rule:
    id: str                          # the catalog id of the limit
    driver: str                      # tool | commit | wire | response | session | compaction | route
    tools: frozenset                 # exact tool names; empty = every tool
    prefixes: tuple                  # tool-name prefixes, e.g. ("mcp_",)
    core_tells: str                  # nobody | model | user | both: who core informs when the limit bites
    status: str                      # verified | fixture | reference
    fn: Callable


class Registry:
    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}
        self._by_tool: dict[tuple, list[Rule]] = {}
        self._wild: dict[str, list[Rule]] = {}
        self.failed: dict[str, int] = {}

    def add(self, rule: Rule) -> None:
        if rule.id in self._rules:
            raise ValueError(f"duplicate rule id {rule.id!r}")
        self._rules[rule.id] = rule
        if rule.tools:
            for tool in rule.tools:
                self._by_tool.setdefault((rule.driver, tool), []).append(rule)
        if rule.prefixes or not rule.tools:
            self._wild.setdefault(rule.driver, []).append(rule)

    def get(self, rule_id: str) -> Optional[Rule]:
        return self._rules.get(rule_id)

    def all(self) -> list[Rule]:
        return sorted(self._rules.values(), key=lambda r: (r.driver, r.id))

    def matching(self, driver: str, tool: str) -> list[Rule]:
        out = list(self._by_tool.get((driver, tool), ()))
        for rule in self._wild.get(driver, ()):
            if not rule.tools and not rule.prefixes:
                out.append(rule)
            elif rule.prefixes and tool.startswith(rule.prefixes) and tool not in rule.tools:
                out.append(rule)
        return out

    def run(self, driver: str, tool: str, event: Any) -> list[Obs]:
        """Run every matching rule. A rule that raises is switched off for this process and counted
        (P6); an unknown format simply yields nothing."""
        found: list[Obs] = []
        for rule in self.matching(driver, tool):
            if self.failed.get(rule.id, 0) >= 3:
                continue
            try:
                for obs in rule.fn(event) or ():
                    if rule.core_tells != "nobody":
                        obs.note = None
                    found.append(obs)
            except Exception:  # noqa: BLE001 - a detector must never break a tool call
                self.failed[rule.id] = self.failed.get(rule.id, 0) + 1
                logger.warning("fine-print rule %s failed (%d)", rule.id, self.failed[rule.id], exc_info=True)
        return found


REGISTRY = Registry()


def rule(rule_id: str, *, driver: str, tools: Iterable[str] = (), prefixes: Iterable[str] = (),
         core_tells: str, status: str = "fixture", registry: Optional[Registry] = None):
    """Register a detector. ``status`` is honest (P8): only ``verified`` rules may raise cards."""
    def deco(fn: Callable) -> Callable:
        (registry or REGISTRY).add(Rule(rule_id, driver, frozenset(tools), tuple(prefixes),
                                        core_tells, status, fn))
        return fn
    return deco


# ── per-turn buffer ────────────────────────────────────────────────────────────────────────────────
class TurnState:
    __slots__ = ("session_id", "turn_id", "calls", "obs", "raw_len", "notes", "note_chars", "t0", "touched")

    def __init__(self, session_id: str, turn_id: str, now: float) -> None:
        self.session_id, self.turn_id = session_id, turn_id
        self.calls: list[Call] = []
        self.obs: list[Obs] = []
        self.raw_len: dict[str, int] = {}
        self.notes = 0
        self.note_chars = 0
        self.t0 = self.touched = now


class TurnBuffer:
    """In-memory, per (session, turn). Lives in the process that runs the turn; everything that must
    survive a turn or a process goes to the ledger."""

    def __init__(self, now: Callable[[], float] = time.time, max_turns: int = 64, idle_s: float = 1800.0):
        self._now, self._max, self._idle = now, max_turns, idle_s
        self._turns: dict[tuple, TurnState] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str, turn_id: str) -> TurnState:
        key = (session_id, turn_id)
        with self._lock:
            st = self._turns.get(key)
            if st is None:
                st = self._turns[key] = TurnState(session_id, turn_id, self._now())
            st.touched = self._now()
            return st

    def pop(self, session_id: str, turn_id: str) -> Optional[TurnState]:
        with self._lock:
            return self._turns.pop((session_id, turn_id), None)

    def pop_stale(self) -> list[TurnState]:
        """Turns nobody closed (background forks fire tool hooks but no end-of-turn hook)."""
        with self._lock:
            if len(self._turns) <= self._max:
                cutoff = self._now() - self._idle
                keys = [k for k, st in self._turns.items() if st.touched < cutoff]
            else:
                keys = sorted(self._turns, key=lambda k: self._turns[k].touched)[: len(self._turns) - self._max]
            return [self._turns.pop(k) for k in keys]

    def lock(self) -> threading.Lock:
        return self._lock


# ── model notes ────────────────────────────────────────────────────────────────────────────────────
def annotate(text: str, note: str) -> str:
    """Put one bracketed line where it survives the 1,500-char spill preview: as the first key of a
    JSON-object result (spliced in, the rest of the bytes untouched), else as a first line."""
    note = " ".join(note.split())[: C.NOTE_MAX_CHARS]
    if text.startswith("{") and json_object(text) is not None:
        body = text[1:]
        if body.strip() == "}":
            return '{"_fine_print": %s}' % json.dumps(note, ensure_ascii=False)
        return '{"_fine_print": %s,%s%s' % (json.dumps(note, ensure_ascii=False),
                                             "" if body[:1].isspace() else " ", body)
    return f"[fine-print: {note}]\n{text}"
