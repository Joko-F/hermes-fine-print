"""Response- and turn-driver rules: what a provider response or the way a turn ended says about a
limit. They read metadata only (finish reason, token counts, the exit reason), never the reply.

Each sentence states what the payload carries and nothing about what Hermes did next. There is no rule
for ``finish_reason="length"`` on purpose: on the main loop core handles it in ``check_api_response``
(agent/turn_response_check.py:173-191) and every ``recover_from_truncation`` path ends in a return,
break or continue (agent/turn_truncation.py:454-548) before ``normalize_model_response`` fires
``post_api_request`` (agent/turn_response_intake.py:188; call order agent/conversation_loop.py:1776),
so that hook is never reached for ``length``. The limit stays in the catalogue as reference."""

from __future__ import annotations

import re
from typing import Iterator

from ..cards import Cfg, text
from ..engine import ApiEvent, Obs, TurnEvent, rule


def _tokens(ev: ApiEvent) -> int:
    for key in ("completion_tokens", "output_tokens"):
        value = ev.usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


@rule("codex-incomplete-ladder", driver="response", core_tells="both", status="fixture")
def codex_incomplete(ev: ApiEvent) -> Iterator[Obs]:
    """post_api_request fires (agent/turn_response_intake.py:188) before the Codex incomplete branch
    (turn_response_intake.py:226), so a Responses-API reply normalized as ``incomplete`` reaches the
    hook with that finish reason (agent/codex_responses_adapter.py:1352-1356). The payload says nothing
    about the cause (output cap, unfinished items or reasoning only) or about what happens next."""
    if ev.finish_reason != "incomplete" or ev.api_mode != "codex_responses":
        return
    yield Obs("codex-incomplete-ladder", numbers={"output_tokens": _tokens(ev)},
              subject=(ev.response_model or ev.model)[:60], cause="incomplete", severity="digest")


@text("codex-incomplete-ladder")
def _codex_incomplete(n: dict, s: str, cfg: Cfg):
    at = f" ({n['output_tokens']:,} output tokens)" if n.get("output_tokens") else ""
    return (f"{s or 'the model'}: a Codex Responses reply ended with finish reason 'incomplete'{at}.", None)


def _reason(ev: TurnEvent) -> str:
    return ev.exit_reason.split("(", 1)[0].strip()


_LIMIT_RE = re.compile(r"^max_iterations_reached\((\d+)/(\d+)\)$")


@rule("turn-and-subagent-caps", driver="turn", core_tells="both", status="fixture")
def turn_budget(ev: TurnEvent) -> Iterator[Obs]:
    """on_session_end carries ``turn_exit_reason`` (agent/turn_finalizer.py:894; post_llm_call does not).
    An exhausted iteration budget arrives as ``max_iterations_reached(calls/limit)``: the finalizer
    rewrites ``budget_exhausted`` to that unless the turn was failed or interrupted
    (turn_finalizer.py:131-138). A bare ``budget_exhausted`` is the surviving other case."""
    numbers: dict = {}
    match = _LIMIT_RE.match(ev.exit_reason.strip())
    if match:
        numbers = {"exit": "max_iterations_reached", "calls": int(match.group(1)), "limit": int(match.group(2))}
    elif _reason(ev) == "budget_exhausted":
        numbers = {"exit": "budget_exhausted"}
    else:
        return
    yield Obs("turn-and-subagent-caps", numbers=numbers, subject="turn", cause="budget", severity="digest")


@text("turn-and-subagent-caps")
def _turn_budget(n: dict, s: str, cfg: Cfg):
    if n.get("limit"):
        return (f"the turn ended with exit reason max_iterations_reached ({n['calls']} of {n['limit']} "
                "iterations).", None)
    return ("the turn ended with exit reason budget_exhausted (the iteration budget ran out).", None)


_EMPTY = {
    "empty_response_exhausted": "the model's replies stayed empty after Hermes's empty-reply retries",
    "all_retries_exhausted_no_response": "no provider response arrived after all retries",
}


@rule("empty-response-retries", driver="turn", core_tells="user", status="fixture")
def empty_reply(ev: TurnEvent) -> Iterator[Obs]:
    """The exit reason is set where the empty-reply ladder gives up (agent/turn_empty_response.py:313)
    or where every retry ended without a response (agent/turn_iteration_prep.py:557); the finalizer
    only rewrites ``budget_exhausted``, so both reach on_session_end unchanged."""
    reason = _reason(ev)
    if reason not in _EMPTY:
        return
    yield Obs("empty-response-retries", numbers={"exit": reason}, subject="turn", cause=reason, severity="digest")


@text("empty-response-retries")
def _empty_reply(n: dict, s: str, cfg: Cfg):
    return (f"the turn ended with exit reason {n['exit']}: {_EMPTY.get(n['exit'], 'retries were used up')}.", None)
