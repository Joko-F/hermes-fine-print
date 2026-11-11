"""Rule registry, per-turn buffer and the two dispatch points every detector goes through.

    drivers (hooks) -> rules -> observations -> turn buffer -> policy -> card | model note | ledger

The same rule functions and the same card policy run live (hook payloads) and over stored history
(``report.py``). Nothing here imports Hermes: the host (config, posting, storage)
is injected, which is also what lets the test suite run on plain Python.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote, urlsplit

from . import constants as C
from .signals import basename, json_object

logger = logging.getLogger("fine_print")

_UNSET = object()


# ── events ─────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Call:
    """One tool call of a turn, live or replayed from history."""
    tool: str
    args: dict
    tool_call_id: str = ""
    ok: bool = True                  # False when the call returned an error: it recovers nothing
    fetched: Optional[frozenset] = None   # web_extract: the URLs that came back without an error; None = not known

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


@dataclass
class ApiEvent:
    """What a response-driver rule sees: one provider response of the main loop (post_api_request)."""
    model: str = ""
    provider: str = ""
    response_model: str = ""
    finish_reason: str = ""
    api_mode: str = ""
    usage: dict = field(default_factory=dict)
    context_length: int = 0
    api_call_count: int = 0


@dataclass
class TurnEvent:
    """What a turn-driver rule sees: how the turn ended (on_session_end fires for every turn)."""
    exit_reason: str = ""
    completed: bool = True
    failed: bool = False
    interrupted: bool = False


@dataclass
class CompactionEvent:
    """What a compaction-driver rule sees: one committed compaction (post_compaction)."""
    kind: str = ""                   # summarize | micro_summarize | prune_tool_results | provider_native
    reason: str = ""
    before_tokens: Optional[int] = None
    after_tokens: Optional[int] = None
    messages_removed: Optional[int] = None
    summarizer_failed: bool = False  # a compression aux call failed just before this compaction


# ── recovery predicates ────────────────────────────────────────────────────────────────────────────
def _num(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_NAME_EDGE = "[A-Za-z0-9_.~-]"


def _names_file(call: Call, name: str) -> bool:
    """Does this call address the file ``name``? For read_file and search_files the path argument
    must be that file. A shell or code call cannot be checked that far: the name has to stand as a
    whole file name in it (``out.log`` does not match ``out.log.bak`` or ``my-out.log``)."""
    if call.tool in ("read_file", "search_files"):
        return basename(str(call.args.get("path") or "")) == name
    pattern = re.compile(f"(?<!{_NAME_EDGE}){re.escape(name)}(?!{_NAME_EDGE})")
    return any(pattern.search(v) for v in call.args.values() if isinstance(v, str))   # the command or the code


_URL_SAFE = "%/:?#[]@!$&'()*+,;=~-._"


def canon_url(value: Any) -> str:
    """A URL in the form core sends and returns it (tools/url_safety.py:38-75): trimmed, the host
    IDNA-encoded, other non-ASCII text percent-encoded, existing escapes kept. The model's own
    spelling and the ``url`` of a result entry must compare equal."""
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    try:
        host = urlsplit(raw).hostname
        if host and not host.isascii():
            at = raw.lower().find(host)
            if at >= 0:
                raw = raw[:at] + host.encode("idna").decode("ascii") + raw[at + len(host):]
    except (ValueError, UnicodeError):
        pass
    return quote(raw, safe=_URL_SAFE)


def fetched_urls(tool: str, obj: Optional[dict]) -> Optional[frozenset]:
    """The URLs of a web_extract result whose own entry carries no error; None when the result has
    no entries to read (the call's arguments are then all there is to go by)."""
    rows = obj.get("results") if tool == "web_extract" and obj is not None else None
    if not isinstance(rows, list):
        return None
    return frozenset(canon_url(r.get("url")) for r in rows
                     if isinstance(r, dict) and r.get("url") and not r.get("error"))


def _fetched_urls(call: Call) -> frozenset:
    """What a web_extract call really got back: its result's good entries, else the first five
    URLs it was given (later ones are never passed on)."""
    if call.fetched is not None:
        return call.fetched
    urls = call.args.get("urls")
    urls = urls[: C.WEB_EXTRACT_URL_CAP] if isinstance(urls, list) else ()
    return frozenset(canon_url(u) for u in urls) - {""}


# Tools that can actually read a saved file back.
READERS = ("read_file", "search_files", "terminal", "execute_code")


@dataclass(frozen=True)
class Recover:
    """A declarative answer to "did the model follow up on the missing part later in the turn?".

    It is evidence of a follow-up, not proof of a complete recovery: only successful calls count, the
    tool must be one that can fetch the missing part, and the target must match. The user-facing
    text therefore says "followed up", never "read everything"."""
    kind: str
    key: str = ""
    extra: tuple = ()

    @staticmethod
    def names(fragment: str, tools: Iterable[str] = ()) -> "Recover":
        """A later successful call of a reading tool whose arguments name ``fragment`` (the saved
        file's basename). ``tools`` narrows which tools count (only code execution can load a
        clipped spreadsheet); the default is every tool that can read a file."""
        return Recover("names", fragment, tuple(tools) or READERS)

    @staticmethod
    def region(image: str) -> "Recover":
        return Recover("region", str(image))

    @staticmethod
    def search_more(pattern: str, path: str, seen: int, glob: str = "", target: str = "content") -> "Recover":
        return Recover("search_more", str(pattern), (str(path), int(seen), str(glob), str(target)))

    @staticmethod
    def refetch(urls: Iterable[str]) -> "Recover":
        return Recover("refetch", "", tuple(canon_url(u) for u in urls))

    @staticmethod
    def refetch_bigger(url: str, budget: int) -> "Recover":
        """The same URL fetched again with a char_limit clearly above the budget that cut it."""
        return Recover("refetch_bigger", canon_url(url), (int(budget),))

    def satisfied(self, later: list[Call]) -> bool:
        later = [c for c in later if c.ok]
        if self.kind == "names":
            return bool(self.key) and any(c.tool in self.extra and _names_file(c, self.key) for c in later)
        if self.kind == "region":
            return any(c.tool == "vision_analyze" and c.args.get("region")
                       and str(c.args.get("image_url", "")) == self.key for c in later)
        if self.kind == "search_more":
            path, seen, glob, target = self.extra
            for c in later:
                a = c.args
                if c.tool != "search_files" or str(a.get("pattern", "")) != self.key:
                    continue
                if (str(a.get("path", "") or "") != path or str(a.get("file_glob", "") or "") != glob
                        or str(a.get("target") or "content") != target):
                    continue
                offset = _num(a.get("offset"), 0)
                limit = _num(a.get("limit"), C.SEARCH_DEFAULT_LIMIT)
                if offset >= seen or offset + limit > seen:
                    return True
            return False
        if self.kind == "refetch":
            wanted = set(self.extra)
            for c in later:
                if c.tool == "web_extract":
                    wanted -= _fetched_urls(c)
            return not wanted
        if self.kind == "refetch_bigger":
            for c in later:
                if c.tool == "web_extract" and self.key in _fetched_urls(c):
                    if _num(c.args.get("char_limit"), 0) > self.extra[0] + 1000:
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
    dead_end: bool = False           # the call this was seen on fetched nothing new (see fetched_nothing)
    # set by the engine
    call_index: int = -1
    tool_call_id: str = ""
    recovered: bool = False          # a follow-up on the missing part was seen later in the turn
    surfaced: int = 0                # 0 not shown; 1 a card bullet; 2 counted under another bullet
    noted: bool = False              # its note was actually written into the tool result


def fetched_nothing(tool: str, obj: Optional[dict], found: Iterable["Obs"]) -> bool:
    """True when a call cannot count as a follow-up on an earlier loss although it did not fail: a
    rule saw it run into a dead end itself (a saved copy that is one cut line, an offset past its
    end), or it is a read that returned no content at all."""
    if any(o.dead_end for o in found):
        return True
    return tool == "read_file" and obj is not None and obj.get("content") == ""


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
    __slots__ = ("session_id", "turn_id", "calls", "obs", "raw_len", "notes", "note_chars", "t0", "touched",
                 "closed", "merged_into")

    def __init__(self, session_id: str, turn_id: str, now: float) -> None:
        self.session_id, self.turn_id = session_id, turn_id
        self.calls: list[Call] = []
        self.obs: list[Obs] = []
        self.raw_len: dict[str, int] = {}
        self.notes = 0
        self.note_chars = 0
        self.t0 = self.touched = now
        self.closed = False              # set when the turn is popped; a late hook must not append
        self.merged_into: Optional["TurnState"] = None   # set when a rotation joined this half to another state


class TurnBuffer:
    """In-memory, per (session, turn). Lives in the process that runs the turn; everything that must
    survive a turn or a process goes to the ledger."""

    def __init__(self, now: Callable[[], float] = time.time, max_turns: int = 64, idle_s: float = 1800.0):
        self._now, self._max, self._idle = now, max_turns, idle_s
        self._turns: dict[tuple, TurnState] = {}
        self._renamed: dict[str, str] = {}      # rotated session id -> its current id (bounded)
        self._lock = threading.Lock()

    def canonical(self, session_id: str) -> str:
        """The current id of a session whose id was rotated by a compaction."""
        with self._lock:
            return self._renamed.get(session_id, session_id)

    def get(self, session_id: str, turn_id: str) -> TurnState:
        with self._lock:
            session_id = self._renamed.get(session_id, session_id)   # a hook that still carries the old id
            key = (session_id, turn_id)
            st = self._turns.get(key)
            if st is None:
                st = self._turns[key] = TurnState(session_id, turn_id, self._now())
            st.touched = self._now()
            return st

    def pop(self, session_id: str, turn_id: str) -> Optional[TurnState]:
        with self._lock:
            st = self._turns.pop((self._renamed.get(session_id, session_id), turn_id), None)
            if st is not None:
                st.closed = True
            return st

    def rename_session(self, old_id: str, new_id: str) -> None:
        """Compaction can rotate the session id in the middle of a turn: keep the turn in one piece.
        Hooks that still arrive under the old id are led to the new one."""
        with self._lock:
            for earlier, current in list(self._renamed.items()):
                if current == old_id:
                    self._renamed[earlier] = new_id
            if len(self._renamed) >= 256:
                del self._renamed[next(iter(self._renamed))]
            self._renamed[old_id] = new_id
            for key in [k for k in self._turns if k[0] == old_id]:
                st = self._turns.pop(key)
                st.session_id = new_id
                target = self._turns.get((new_id, key[1]))
                if target is None:
                    self._turns[(new_id, key[1])] = st
                else:                      # hooks already arrived under the new id: merge, old part first
                    shift = len(st.calls)
                    for obs in target.obs:
                        if obs.call_index >= 0:
                            obs.call_index += shift
                    target.calls[:0] = st.calls
                    target.obs[:0] = st.obs
                    target.raw_len.update(st.raw_len)
                    target.notes += st.notes
                    target.note_chars += st.note_chars
                    st.merged_into = target    # a hook still holding the old half continues on the joined turn

    def pop_stale(self) -> list[TurnState]:
        """Turns nobody closed (background forks fire tool hooks but no end-of-turn hook)."""
        with self._lock:
            if len(self._turns) <= self._max:
                cutoff = self._now() - self._idle
                keys = [k for k, st in self._turns.items() if st.touched < cutoff]
            else:
                keys = sorted(self._turns, key=lambda k: self._turns[k].touched)[: len(self._turns) - self._max]
            stale = [self._turns.pop(k) for k in keys]
            for st in stale:
                st.closed = True
            return stale

    def lock(self) -> threading.Lock:
        return self._lock


# ── model notes ────────────────────────────────────────────────────────────────────────────────────
def annotate(text: str, note: Any, is_object: Optional[bool] = None) -> str:
    """Put one bracketed line where it survives the 1,500-char spill preview: as the first key of a
    JSON-object result (spliced in, the rest of the bytes untouched), else as a first line.
    ``note`` is one sentence or a list of them: each is cut to the note budget and they share ONE
    key (two keys of the same name would leave a JSON reader with only the last).
    ``is_object`` passes on what the caller already parsed, so a large result is not parsed twice."""
    notes = [note] if isinstance(note, str) else list(note)
    note = " ".join(" ".join(str(n).split())[: C.NOTE_MAX_CHARS] for n in notes if n)
    if is_object is None:
        is_object = json_object(text) is not None
    if is_object and text.startswith("{"):
        body = text[1:]
        if body.strip() == "}":
            return '{"_fine_print": %s}' % json.dumps(note, ensure_ascii=False)
        return '{"_fine_print": %s,%s%s' % (json.dumps(note, ensure_ascii=False),
                                             "" if body[:1].isspace() else " ", body)
    return f"[fine-print: {note}]\n{text}"
