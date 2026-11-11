"""The runtime: hook payloads in, cards / notes / ledger rows out.

``FinePrint`` owns the turn buffer and talks to Hermes only through ``Host`` (config, settings,
posting, storage), which ``plugin.py`` implements with public plugin APIs and tests replace with a
fake. No LLM calls, no network, no subprocesses anywhere in this package.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Any, Callable, Optional, Protocol

from . import constants as C
from . import rules  # noqa: F401  (registers the detectors)
from .cards import decide, mark_surfaced, render
from .engine import (REGISTRY, ApiEvent, Call, CommitRow, CompactionEvent, Obs, Registry, ToolEvent, TurnBuffer,
                     TurnEvent, TurnState, annotate, fetched_nothing)
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
        self.pending: dict[str, list[Obs]] = {}    # session -> observations waiting for a turn to close
        self.context_length: dict[str, int] = {}   # session -> context window in tokens, from responses
        self._last_summary = (0.0, True)           # (time, ok) of the last compression aux call
        self._side_lock = threading.Lock()
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
        failed = ev.is_error
        st = self.buffer.get(ev.session_id, ev.turn_id)
        with self.buffer.lock():
            index = len(st.calls)
            st.calls.append(Call(tool, args, ev.tool_call_id, ok=not failed))
            if ev.text is not None and ev.tool_call_id:
                st.raw_len[ev.tool_call_id] = len(ev.text)
        if failed:
            return None
        found = self.registry.run("tool", tool, ev)
        if fetched_nothing(tool, ev.json, found):          # a read that got nothing is not a follow-up
            with self.buffer.lock():
                st.calls[index] = dataclasses.replace(st.calls[index], ok=False)
        if not found:
            return None
        for obs in found:
            obs.call_index, obs.tool_call_id = index, ev.tool_call_id
        with self.buffer.lock():
            late = st.closed             # the turn was closed while this hook ran (a slow or abandoned call)
            if not late:
                st.obs.extend(found)
        if late:
            self.ledger.record(ev.session_id, ev.turn_id, found)
            return None
        return self._note(ev, found, st)

    def _note(self, ev: ToolEvent, found: list[Obs], st: TurnState) -> Optional[str]:
        if ev.text is None or ev.tool in C.NEVER_ANNOTATE or not self.notes_on():
            return None
        noted = [o for o in found if o.note]
        with self.buffer.lock():
            room = C.NOTES_PER_TURN - st.notes
            noted = noted[: max(0, room)]
            if not noted:
                return None
            st.notes += len(noted)
        out = ev.text
        is_object = ev.json is not None
        for obs in reversed(noted):
            out = annotate(out, obs.note, is_object)
            obs.noted = True
        notes = noted
        with self.buffer.lock():
            st.note_chars += len(out) - len(ev.text)
        self.stats["notes"] += len(notes)
        return out

    # ── commit driver (post_llm_call, on_session_end) ──────────────────────────────────────────────
    def turn_end(self, session_id: str = "", turn_id: str = "", conversation_history: Any = None,
                 platform: str = "", model: str = "", turn_exit_reason: str = "", completed: Any = True,
                 failed: Any = False, interrupted: Any = False, **_: Any) -> Optional[str]:
        """Close the turn: diff raw results against committed rows, decide what the model recovered,
        write the ledger once, and post at most one card. Returns the card text (also for tests).
        ``post_llm_call`` closes a normal turn; ``on_session_end`` fires for every turn and closes the
        ones that failed or were interrupted (it finds nothing to do after a normal close)."""
        self.stats["hooks"] += 1
        session_id, turn_id = str(session_id or ""), str(turn_id or "")
        st = self.buffer.pop(session_id, turn_id)
        self._flush_stale()
        extra: list[Obs] = []
        if turn_exit_reason:
            extra = self.registry.run("turn", "", TurnEvent(str(turn_exit_reason), bool(completed), bool(failed),
                                                            bool(interrupted)))
        with self._side_lock:
            waiting = self.pending.pop(session_id, [])
        if st is None:
            if not extra and not waiting:
                return None
            st = TurnState(session_id, turn_id, time.time())
        st.obs.extend(extra)
        st.obs.extend(waiting)
        return self._close(st, conversation_history, str(platform or ""), str(model or ""), post=True)

    # ── response driver (post_api_request) ─────────────────────────────────────────────────────────
    def api_response(self, session_id: str = "", turn_id: str = "", model: str = "", provider: str = "",
                     response_model: Any = None, finish_reason: Any = None, api_mode: str = "", usage: Any = None,
                     context_length: Any = None, api_call_count: Any = 0, **_: Any) -> None:
        """One provider response of the main loop. Rules see metadata only (finish reason, served
        model, token counts); the response body is never read."""
        self.stats["hooks"] += 1
        if not turn_id:
            return                       # background forks fire this hook without a user turn
        ev = ApiEvent(str(model or ""), str(provider or ""), str(response_model or ""), str(finish_reason or ""),
                      str(api_mode or ""), usage if isinstance(usage, dict) else {},
                      context_length if isinstance(context_length, int) else 0,
                      api_call_count if isinstance(api_call_count, int) else 0)
        if ev.context_length > 0 and session_id:
            if len(self.context_length) > 256:
                self.context_length.clear()
            self.context_length[str(session_id)] = ev.context_length
        found = self.registry.run("response", "", ev)
        if found:
            st = self.buffer.get(str(session_id or ""), str(turn_id))
            with self.buffer.lock():
                st.obs.extend(found)

    # ── compaction driver (post_auxiliary_call, post_compaction) ───────────────────────────────────
    def aux_call(self, aux_task: str = "", error: Any = None, error_type: Any = None, **_: Any) -> None:
        if aux_task == "compression":
            with self._side_lock:
                self._last_summary = (time.time(), not (error or error_type))

    def compaction(self, session_id: str = "", old_session_id: str = "", kind: str = "", reason: str = "",
                   before_tokens: Any = None, after_tokens: Any = None, messages_removed: Any = None,
                   **_: Any) -> None:
        """A committed compaction. The session id may rotate: the ledger and the open turn follow it.
        The observation waits for the end of the session's current or next turn, where the card goes."""
        self.stats["hooks"] += 1
        session_id, old_session_id = str(session_id or ""), str(old_session_id or "")
        if old_session_id and session_id and old_session_id != session_id:
            self.buffer.rename_session(old_session_id, session_id)
            self.ledger.rename_session(old_session_id, session_id)
            with self._side_lock:
                if old_session_id in self.pending:
                    self.pending.setdefault(session_id, []).extend(self.pending.pop(old_session_id))
                if old_session_id in self.parents:
                    self.parents[session_id] = self.parents.pop(old_session_id)
        with self._side_lock:
            stamp, ok = self._last_summary
            failed = (not ok) and (time.time() - stamp) < 300.0

        def _int(value: Any) -> Optional[int]:
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        ev = CompactionEvent(str(kind or ""), str(reason or ""), _int(before_tokens), _int(after_tokens),
                             _int(messages_removed), failed)
        found = self.registry.run("compaction", "", ev)
        if found and session_id:
            with self._side_lock:
                self.pending.setdefault(session_id, []).extend(found)

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
                card, groups, child_hits = self._card(st.session_id, observations, platform)
                if card and self.host.post(card):        # only a delivered card counts as shown
                    mark_surfaced(groups)
                    self.stats["cards"] += 1
                elif card:
                    if child_hits:
                        self.child_hits[st.session_id] = self.child_hits.get(st.session_id, 0) + child_hits
                    card = None
            self.ledger.record(st.session_id, st.turn_id, observations)
        if st.note_chars:
            self.ledger.add_note_chars(st.session_id, st.note_chars, st.notes)
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

    def _card(self, session_id: str, observations: list[Obs], platform: str) -> tuple[Optional[str], list, int]:
        if not self.cards_on(platform):
            return None, [], 0
        groups, more = decide(
            observations,
            status_of=lambda rid: getattr(self.registry.get(rid), "status", ""),
            mutes=self.ledger.mutes(),
            surfaced_causes=self.ledger.surfaced_causes(session_id),
            surfaced_counts=self.ledger.surfaced_counts(session_id),
        )
        child_hits = self.child_hits.pop(session_id, 0) if groups else 0
        text = render(groups, more, core_tells_of=lambda rid: getattr(self.registry.get(rid), "core_tells", ""),
                      cfg=self.cfg_for(session_id), child_hits=child_hits)
        return text, groups, child_hits

    def cfg_for(self, session_id: str) -> Callable[[str, Any], Any]:
        """The Hermes config reader, plus the one runtime fact advice needs: this session's window."""
        window = self.context_length.get(session_id, 0)

        def cfg(key: str, default: Any) -> Any:
            return (window or default) if key == "@context_length" else self.host.config(key, default)
        return cfg

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
