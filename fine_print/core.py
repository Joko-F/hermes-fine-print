"""The runtime: hook payloads in, cards / notes / ledger rows out.

``FinePrint`` owns the turn buffer and talks to Hermes only through ``Host`` (config, settings,
posting, storage), which ``plugin.py`` implements with public plugin APIs and tests replace with a
fake. No LLM calls, no network, no subprocesses anywhere in this package.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, Protocol

from . import constants as C
from . import rules  # noqa: F401  (registers the detectors)
from .cards import decide, render
from .engine import REGISTRY, Call, CommitRow, Obs, Registry, ToolEvent, TurnBuffer, TurnState, annotate
from .ledger import Ledger

logger = logging.getLogger("fine_print")


class Host(Protocol):
    def setting(self, key: str, default: Any) -> Any: ...          # plugins.entries.fine-print.settings.*
    def config(self, dotted: str, default: Any) -> Any: ...        # Hermes config, read-only
    def post(self, text: str) -> bool: ...                         # a user-facing notice in this chat
    def surface(self) -> str: ...                                  # cli | tui | desktop | telegram | ...


def _truthy(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("off", "false", "0", "no", "")
    return default if value is None else bool(value)


class FinePrint:
    def __init__(self, host: Host, ledger: Ledger, registry: Registry = REGISTRY,
                 now: Callable[[], float] = time.time) -> None:
        self.host, self.ledger, self.registry = host, ledger, registry
        self.buffer = TurnBuffer(now)
        self.parents: dict[str, str] = {}          # child session -> parent session
        self.child_hits: dict[str, int] = {}       # parent session -> unrecovered hits in its subagents
        self.stats = {"hooks": 0, "errors": 0, "cards": 0, "notes": 0}

    # ── settings ───────────────────────────────────────────────────────────────────────────────────
    def notes_on(self) -> bool:
        return _truthy(self.host.setting("notes", True))

    def cards_on(self, platform: str = "") -> bool:
        mode = self.host.setting("cards", "auto")
        if isinstance(mode, bool):
            return mode
        mode = str(mode or "auto").strip().lower()
        if mode in ("on", "true", "always"):
            return True
        if mode in ("off", "false", "never"):
            return False
        surface = (platform or "").lower() or self.host.surface().lower()
        return surface in C.LOCAL_SURFACES      # a notice on a messaging platform is a real message

    # ── tool driver (transform_tool_result) ────────────────────────────────────────────────────────
    def tool_result(self, tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "",
                    turn_id: str = "", tool_call_id: str = "", status: str = "ok", **_: Any) -> Optional[str]:
        """Record the call, run the tool rules on the raw result, and return an annotated copy only
        when a rule has something the model is told by nobody else. None means "leave it as it is"."""
        self.stats["hooks"] += 1
        tool = str(tool_name or "")
        args = args if isinstance(args, dict) else {}
        ev = ToolEvent(tool, args, result, str(session_id or ""), str(turn_id or ""),
                       str(tool_call_id or ""), str(status or "ok"))
        st = self.buffer.get(ev.session_id, ev.turn_id)
        with self.buffer.lock():
            index = len(st.calls)
            st.calls.append(Call(tool, args, ev.tool_call_id))
            if ev.text is not None and ev.tool_call_id:
                st.raw_len[ev.tool_call_id] = len(ev.text)
        if ev.is_error:
            return None
        found = self.registry.run("tool", tool, ev)
        if not found:
            return None
        for obs in found:
            obs.call_index, obs.tool_call_id = index, ev.tool_call_id
        with self.buffer.lock():
            st.obs.extend(found)
        return self._note(ev, found, st)

    def _note(self, ev: ToolEvent, found: list[Obs], st: TurnState) -> Optional[str]:
        if ev.text is None or ev.tool in C.NEVER_ANNOTATE or not self.notes_on():
            return None
        notes = [o.note for o in found if o.note]
        with self.buffer.lock():
            room = C.NOTES_PER_TURN - st.notes
            notes = notes[: max(0, room)]
            if not notes:
                return None
            st.notes += len(notes)
        out = ev.text
        for note in reversed(notes):
            out = annotate(out, note)
        with self.buffer.lock():
            st.note_chars += len(out) - len(ev.text)
        self.stats["notes"] += len(notes)
        return out

    # ── commit driver (post_llm_call, on_session_end) ──────────────────────────────────────────────
    def turn_end(self, session_id: str = "", turn_id: str = "", conversation_history: Any = None,
                 platform: str = "", model: str = "", **_: Any) -> Optional[str]:
        """Close the turn: diff raw results against committed rows, decide what the model recovered,
        write the ledger once, and post at most one card. Returns the card text (also for tests)."""
        self.stats["hooks"] += 1
        session_id, turn_id = str(session_id or ""), str(turn_id or "")
        st = self.buffer.pop(session_id, turn_id)
        self._flush_stale()
        if st is None:
            return None
        return self._close(st, conversation_history, str(platform or ""), str(model or ""), post=True)

    def _close(self, st: TurnState, history: Any, platform: str, model: str, post: bool) -> Optional[str]:
        observations = list(st.obs)
        observations.extend(self._commit_rules(st, history))
        for obs in observations:
            later = st.calls[obs.call_index + 1:] if obs.call_index >= 0 else []
            obs.recovered = any(r.satisfied(later) for r in obs.recover)
        parent = self.parents.get(st.session_id, "")
        self.ledger.touch_session(st.session_id, parent_id=parent, model=model, platform=platform)
        card = None
        if observations:
            if parent:        # a subagent's notice would land in the parent chat: fold it into the parent's card
                open_hits = sum(1 for o in observations if not o.recovered)
                if open_hits:
                    self.child_hits[parent] = self.child_hits.get(parent, 0) + open_hits
            elif post:
                card = self._card(st.session_id, observations, platform)
            self.ledger.record(st.session_id, st.turn_id, observations)
        if st.note_chars:
            self.ledger.add_note_chars(st.session_id, st.note_chars, st.notes)
        if card and self.host.post(card):
            self.stats["cards"] += 1
        return card

    def _commit_rules(self, st: TurnState, history: Any) -> list[Obs]:
        if not isinstance(history, list) or not st.calls:
            return []
        index = {c.tool_call_id: i for i, c in enumerate(st.calls) if c.tool_call_id}
        found: list[Obs] = []
        for row in reversed(history):
            if not isinstance(row, dict):
                continue
            role = row.get("role")
            if role == "user":
                break                                   # rows of earlier turns are not ours
            if role != "tool":
                continue
            tcid = str(row.get("tool_call_id") or "")
            i = index.get(tcid)
            if i is None:
                continue
            call = st.calls[i]
            crow = CommitRow(call.tool, call.args, row.get("content"), st.raw_len.get(tcid), tcid)
            for obs in self.registry.run("commit", call.tool, crow):
                obs.call_index, obs.tool_call_id = i, tcid
                found.append(obs)
        found.reverse()
        return found

    def _card(self, session_id: str, observations: list[Obs], platform: str) -> Optional[str]:
        if not self.cards_on(platform):
            return None
        groups, more = decide(
            observations,
            status_of=lambda rid: getattr(self.registry.get(rid), "status", ""),
            mutes=self.ledger.mutes(),
            surfaced_causes=self.ledger.surfaced_causes(session_id),
            surfaced_counts=self.ledger.surfaced_counts(session_id),
        )
        return render(groups, more,
                      core_tells_of=lambda rid: getattr(self.registry.get(rid), "core_tells", ""),
                      notes_on=self.notes_on(), cfg=self.host.config,
                      child_hits=self.child_hits.pop(session_id, 0) if groups else 0)

    def _flush_stale(self) -> None:
        for st in self.buffer.pop_stale():
            try:
                self._close(st, None, "", "", post=False)
            except Exception:  # noqa: BLE001
                logger.debug("fine-print stale flush failed", exc_info=True)

    # ── session bookkeeping ────────────────────────────────────────────────────────────────────────
    def subagent_start(self, parent_session_id: str = "", child_session_id: str = "", **_: Any) -> None:
        if parent_session_id and child_session_id:
            self.parents[str(child_session_id)] = str(parent_session_id)

    def compaction(self, session_id: str = "", old_session_id: str = "", **_: Any) -> None:
        if old_session_id and session_id and old_session_id != session_id:
            self.ledger.rename_session(str(old_session_id), str(session_id))

    def session_deleted(self, session_id: str = "", deleted_ids: Any = None, **_: Any) -> None:
        ids = [str(session_id)] if session_id else []
        if isinstance(deleted_ids, (list, tuple)):
            ids.extend(str(i) for i in deleted_ids)
        self.ledger.purge(ids)


def guarded(fn: Callable, stats: dict) -> Callable:
    """Hook wrapper: a detector bug must never break a turn. The callback keeps ``**kwargs``."""
    def hook(**kwargs: Any) -> Any:
        try:
            return fn(**kwargs)
        except Exception:  # noqa: BLE001
            stats["errors"] = stats.get("errors", 0) + 1
            logger.warning("fine-print hook %s failed", getattr(fn, "__name__", "?"), exc_info=True)
            return None
    hook.__name__ = getattr(fn, "__name__", "hook")
    return hook
