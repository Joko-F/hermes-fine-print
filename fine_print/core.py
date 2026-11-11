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
from .engine import (REGISTRY, ApiErrorEvent, ApiEvent, AuxEvent, Call, CommitRow, CompactionEvent, Obs, Registry,
                     SessionEvent, ToolEvent, TurnBuffer, TurnEvent, TurnState, UserTurnEvent, WireEvent, annotate,
                     fetched_nothing, fetched_urls)
from .ledger import Ledger

logger = logging.getLogger("fine_print")


class Host(Protocol):
    def setting(self, key: str, default: Any) -> Any: ...          # plugins.entries.fine-print.settings.*
    def config(self, dotted: str, default: Any) -> Any: ...        # Hermes config, read-only
    def post(self, text: str) -> bool: ...                         # a user-facing notice in this chat
    def surface(self) -> str: ...                                  # cli | tui | desktop | telegram | ...


SIDE_MAP_MAX = 512        # sessions remembered per in-memory side map of a long-lived gateway


def _put(mapping: dict, key: str, value: Any) -> None:
    """Set a key in a side map that must not grow without bound: the oldest entry makes room."""
    if key not in mapping and len(mapping) >= SIDE_MAP_MAX:
        del mapping[next(iter(mapping))]
    mapping[key] = value


def _tool_names(request: Any) -> tuple:
    """Names of the tools listed in a request payload as the hook passes it (``request["body"]["tools"]``,
    chat or Responses shape); () when the payload does not list them."""
    body = request.get("body") if isinstance(request, dict) else None
    tools = body.get("tools") if isinstance(body, dict) else None
    names = []
    for tool in tools if isinstance(tools, list) else ():
        if isinstance(tool, dict):
            fn = tool.get("function")
            name = fn.get("name") if isinstance(fn, dict) else tool.get("name")
            if isinstance(name, str) and name:
                names.append(name[:80])
    return tuple(names[:200])


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
        self.models: dict[str, str] = {}           # session -> its own model, from responses
        self._summary_calls: dict[str, tuple] = {} # session -> (time, ok) of its last compression aux call
        self.session_facts: dict[str, dict] = {}   # session -> facts of its first request, until the session check ran
        self._session_checked: dict[str, bool] = {}  # sessions whose once-per-session rules have run
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
        failed = ev.is_error             # core's status, e.g. a non-zero exit code or a top-level error
        st = self.buffer.get(ev.session_id, ev.turn_id)
        call = Call(tool, args, ev.tool_call_id, ok=not failed,
                    fetched=fetched_urls(tool, ev.json) if tool == "web_extract" else None)
        raw_len = len(ev.text) if ev.text is not None and ev.tool_call_id else None
        with self.buffer.lock():
            st.calls.append(call)
            if raw_len is not None:
                st.raw_len[ev.tool_call_id] = raw_len
        # A failed call is no follow-up and is never annotated, but it is still read: a failed build
        # whose output was cut, a timeout and a refusal are results with an error status.
        found = self.registry.run("tool", tool, ev)
        nothing = not failed and fetched_nothing(tool, ev.json, found)     # a read that got nothing is no follow-up
        with self.buffer.lock():
            # The rules ran without the lock. A compaction may have rotated the session meanwhile and
            # joined this turn's halves: follow the turn, and find the call by identity, not by position.
            while st.merged_into is not None:
                st = st.merged_into
            index = next((i for i in range(len(st.calls) - 1, -1, -1) if st.calls[i] is call), -1)
            if index < 0:                    # it was appended to the half that had already been joined
                st.calls.append(call)
                index = len(st.calls) - 1
                if raw_len is not None:
                    st.raw_len[ev.tool_call_id] = raw_len
            if nothing:
                st.calls[index] = dataclasses.replace(call, ok=False)
            for obs in found:
                obs.call_index, obs.tool_call_id = index, ev.tool_call_id
            late = st.closed             # the turn was closed while this hook ran (a slow or abandoned call)
            if found and not late:
                st.obs.extend(found)
        if not found:
            return None
        if late:
            self.ledger.record(st.session_id, st.turn_id, found)
            return None
        return None if failed else self._note(ev, found, st)

    def _note(self, ev: ToolEvent, found: list[Obs], st: TurnState) -> Optional[str]:
        if ev.text is None or ev.tool in C.NEVER_ANNOTATE or not self.notes_on():
            return None
        obj = ev.json
        if obj is not None and "_fine_print" in obj:       # never write over a key that is already there
            return None
        noted = [o for o in found if o.note]
        with self.buffer.lock():
            room = C.NOTES_PER_TURN - st.notes
            noted = noted[: max(0, room)]
            if not noted:
                return None
            st.notes += len(noted)
        out = annotate(ev.text, [o.note for o in noted], obj is not None)      # one key for all of them
        for obs in noted:
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
        session_id, turn_id = self.buffer.canonical(str(session_id or "")), str(turn_id or "")
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

    # ── observers without a tool call (wire, api_error, route, aux, session) ───────────────────────
    def _attach(self, session_id: str, turn_id: str, found: list[Obs]) -> None:
        """Add observations that belong to no tool call to the turn. A request-level fact repeats on
        every request of a turn, so one (rule, cause) is kept once per turn."""
        if not found or not turn_id:
            return
        st = self.buffer.get(session_id, turn_id)
        with self.buffer.lock():
            while st.merged_into is not None:
                st = st.merged_into
            have = {(o.rule_id, o.cause) for o in st.obs}
            for obs in found:
                if (obs.rule_id, obs.cause) not in have:
                    have.add((obs.rule_id, obs.cause))
                    st.obs.append(obs)

    def api_request(self, session_id: str = "", turn_id: str = "", conversation_history: Any = None,
                    request_messages: Any = None, model: str = "", provider: str = "", api_mode: str = "",
                    api_call_count: Any = 0, retry_count: Any = 0, tool_count: Any = 0,
                    approx_input_tokens: Any = 0, request_char_count: Any = 0, max_tokens: Any = None,
                    system_prompt: Any = None, platform: str = "", request: Any = None, **_: Any) -> None:
        """One request about to be sent. Rules see the two message lists by reference and read
        structure and sizes only; nothing of a request is kept beyond the turn's counters."""
        self.stats["hooks"] += 1
        if not turn_id:
            return                       # background forks send requests without a user turn

        def _count(value: Any) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        ev = WireEvent(str(model or ""), str(provider or ""), str(api_mode or ""),
                       conversation_history if isinstance(conversation_history, list) else [],
                       request_messages if isinstance(request_messages, list) else [],
                       _count(api_call_count), _count(retry_count), _count(tool_count), _count(approx_input_tokens),
                       _count(request_char_count), max_tokens if isinstance(max_tokens, int) else None,
                       len(system_prompt) if isinstance(system_prompt, str) else 0)
        session_id = self.buffer.canonical(str(session_id or ""))
        with self._side_lock:
            if session_id and session_id not in self._session_checked:
                facts = self.session_facts.get(session_id)
                if facts is None:
                    facts = {}
                    _put(self.session_facts, session_id, facts)
                if "model" not in facts:     # the first request only; pre_llm_call may have left is_first_turn
                    facts.update(model=ev.model, provider=ev.provider, api_mode=ev.api_mode,
                                 platform=str(platform or ""), max_tokens=ev.max_tokens, tool_count=ev.tool_count,
                                 tool_names=_tool_names(request), system_prompt_chars=ev.system_prompt_chars)
        self._attach(session_id, str(turn_id), self.registry.run("wire", "", ev))

    def api_error(self, session_id: str = "", turn_id: str = "", model: str = "", provider: str = "",
                  api_mode: str = "", status_code: Any = None, retry_count: Any = None, max_retries: Any = None,
                  retryable: Any = None, reason: Any = None, error: Any = None, api_call_count: Any = 0,
                  **_: Any) -> None:
        """One failed provider request: status, retry counters and core's classification, no body."""
        self.stats["hooks"] += 1
        if not turn_id:
            return
        ev = ApiErrorEvent(str(model or ""), str(provider or ""), str(api_mode or ""),
                           status_code if isinstance(status_code, int) and not isinstance(status_code, bool) else None,
                           retry_count if isinstance(retry_count, int) and not isinstance(retry_count, bool) else 0,
                           max_retries if isinstance(max_retries, int) and not isinstance(max_retries, bool) else None,
                           retryable if isinstance(retryable, bool) else None, str(reason or ""),
                           str(error.get("type") or "") if isinstance(error, dict) else "",
                           api_call_count if isinstance(api_call_count, int) else 0)
        self._attach(self.buffer.canonical(str(session_id or "")), str(turn_id), self.registry.run("api_error", "", ev))

    def user_turn(self, session_id: str = "", turn_id: str = "", user_message: Any = None,
                  conversation_history: Any = None, is_first_turn: Any = None, model: str = "",
                  platform: str = "", parent_session_id: str = "", **_: Any) -> None:
        """The user message as a turn starts. Returns nothing: this hook may inject context, Fine Print never does."""
        self.stats["hooks"] += 1
        session_id = self.buffer.canonical(str(session_id or ""))
        if session_id and isinstance(is_first_turn, bool):
            with self._side_lock:
                facts = self.session_facts.get(session_id)
                if facts is not None:
                    facts.setdefault("is_first_turn", is_first_turn)
                elif session_id not in self._session_checked:
                    _put(self.session_facts, session_id, {"is_first_turn": is_first_turn})
        if not turn_id:
            return
        ev = UserTurnEvent(user_message, str(model or ""), str(platform or ""), bool(is_first_turn),
                           bool(parent_session_id), conversation_history if isinstance(conversation_history, list) else [])
        self._attach(session_id, str(turn_id), self.registry.run("route", "", ev))

    def _session_check(self, session_id: str, ev: ApiEvent) -> list[Obs]:
        """Run the once-per-session rules after the session's first provider response."""
        with self._side_lock:
            if not session_id or session_id in self._session_checked:
                return []
            _put(self._session_checked, session_id, True)
            facts = self.session_facts.pop(session_id, {})
        sev = SessionEvent(ev.model or facts.get("model", ""), ev.provider or facts.get("provider", ""),
                           ev.api_mode or facts.get("api_mode", ""), facts.get("platform", ""), ev.context_length,
                           facts.get("max_tokens"), facts.get("tool_count", 0), facts.get("tool_names", ()),
                           facts.get("system_prompt_chars", 0), facts.get("is_first_turn"),
                           session_id in self.parents, self.host.config)
        return self.registry.run("session", "", sev)

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
        session_id = self.buffer.canonical(str(session_id or ""))
        if session_id and ev.model:
            _put(self.models, session_id, ev.model)
        if ev.context_length > 0 and session_id:
            _put(self.context_length, session_id, ev.context_length)
        found = self.registry.run("response", "", ev)
        found += self._session_check(session_id, ev)
        if found:
            st = self.buffer.get(session_id, str(turn_id))
            with self.buffer.lock():
                st.obs.extend(found)

    # ── compaction driver (post_auxiliary_call, post_compaction) ───────────────────────────────────
    def aux_call(self, aux_task: str = "", error: Any = None, error_type: Any = None, session_id: str = "",
                 turn_id: str = "", model: str = "", provider: str = "", message_count: Any = 0,
                 api_duration: Any = 0.0, **_: Any) -> None:
        """One finished helper-model call. For compression: remember, per session, whether it failed
        (a call that carries no session, cron or idle work, is attributed to nobody). Every call
        with a turn goes to the aux rules, gated by its task."""
        self.stats["hooks"] += 1
        if aux_task == "compression" and session_id:
            with self._side_lock:
                _put(self._summary_calls, str(session_id), (time.time(), not (error or error_type)))
        if not session_id or not turn_id:
            return
        session_id = self.buffer.canonical(str(session_id))
        with self._side_lock:
            main_model = (self.session_facts.get(session_id) or {}).get("model", "") or self.models.get(session_id, "")
        ev = AuxEvent(str(aux_task or ""), str(model or ""), str(provider or ""), main_model,
                      str(error_type or ("error" if error else "")),
                      message_count if isinstance(message_count, int) else 0,
                      float(api_duration) if isinstance(api_duration, (int, float)) else 0.0)
        self._attach(session_id, str(turn_id), self.registry.run("aux", ev.aux_task, ev))

    def compaction(self, session_id: str = "", old_session_id: str = "", kind: str = "", reason: str = "",
                   before_tokens: Any = None, after_tokens: Any = None, messages_removed: Any = None,
                   **_: Any) -> None:
        """A committed compaction. The session id may rotate: the ledger and the open turn follow it.
        The observation waits for the end of the session's current or next turn, where the card goes."""
        self.stats["hooks"] += 1
        session_id, old_session_id = str(session_id or ""), str(old_session_id or "")
        rotated = bool(old_session_id and session_id and old_session_id != session_id)
        if rotated:
            self.buffer.rename_session(old_session_id, session_id)
            self.ledger.rename_session(old_session_id, session_id)
            with self._side_lock:
                if old_session_id in self.pending:
                    self.pending.setdefault(session_id, []).extend(self.pending.pop(old_session_id))
                if old_session_id in self.parents:                     # the rotated session is a subagent
                    self.parents[session_id] = self.parents.pop(old_session_id)
                for child, parent in list(self.parents.items()):       # ... or it has subagents
                    if parent == old_session_id:
                        self.parents[child] = session_id
                if old_session_id in self.child_hits:
                    self.child_hits[session_id] = self.child_hits.get(session_id, 0) + self.child_hits.pop(old_session_id)
                for mapping in (self.context_length, self.models, self.session_facts, self._session_checked):
                    if old_session_id in mapping:
                        mapping[session_id] = mapping.pop(old_session_id)
        with self._side_lock:
            # the helper call of this compaction ran under this session, before or after the rotation
            calls = [self._summary_calls.pop(sid) for sid in {old_session_id, session_id} if sid in self._summary_calls]
            stamp, ok = max(calls) if calls else (0.0, True)
            failed = (not ok) and (time.time() - stamp) < 300.0

        def _int(value: Any) -> Optional[int]:
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        ev = CompactionEvent(str(kind or ""), str(reason or ""), _int(before_tokens), _int(after_tokens),
                             _int(messages_removed), failed)
        found = self.registry.run("compaction", "", ev)
        if found and session_id:
            with self._side_lock:
                waiting = self.pending.pop(session_id, [])
                _put(self.pending, session_id, waiting + found)

    def _close(self, st: TurnState, history: Any, platform: str, model: str, post: bool) -> Optional[str]:
        st.session_id = self.buffer.canonical(st.session_id)    # the id may have rotated after the turn was popped
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
                    with self._side_lock:
                        _put(self.child_hits, parent, self.child_hits.get(parent, 0) + open_hits)
            elif post:
                card, groups, child_hits = self._card(st.session_id, observations, platform)
                if card and self.host.post(card):        # only a delivered card counts as shown
                    mark_surfaced(groups)
                    self.stats["cards"] += 1
                elif card:
                    if child_hits:
                        with self._side_lock:
                            _put(self.child_hits, st.session_id, self.child_hits.get(st.session_id, 0) + child_hits)
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
        with self._side_lock:
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
            with self._side_lock:
                _put(self.parents, str(child_session_id), self.buffer.canonical(str(parent_session_id)))

    def session_deleted(self, session_id: str = "", deleted_ids: Any = None, **_: Any) -> None:
        ids = [str(session_id)] if session_id else []
        if isinstance(deleted_ids, (list, tuple)):
            ids.extend(str(i) for i in deleted_ids)
        self.ledger.purge(ids)
        with self._side_lock:
            for sid in ids:
                for mapping in (self.parents, self.child_hits, self.pending, self.context_length, self._summary_calls,
                                self.models, self.session_facts, self._session_checked):
                    mapping.pop(sid, None)


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
