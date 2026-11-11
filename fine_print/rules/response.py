"""Response- and turn-driver rules: what a provider response or the way a turn ended says about a
limit. They read metadata only (finish reason, token counts, the exit reason), never the reply."""

from __future__ import annotations

from typing import Iterator

from ..cards import Cfg, text
from ..engine import ApiEvent, Obs, TurnEvent, rule


def _tokens(ev: ApiEvent) -> int:
    for key in ("completion_tokens", "output_tokens"):
        value = ev.usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


@rule("output-cap-and-length-continuation", driver="response", core_tells="both", status="fixture")
def output_cap(ev: ApiEvent) -> Iterator[Obs]:
    """finish_reason=length: the reply hit the output cap; core asks the model to continue (up to 4
    times) and joins the pieces without a marker. The cap itself is not configurable in v0.23.0."""
    if ev.finish_reason != "length":
        return
    yield Obs("output-cap-and-length-continuation", numbers={"output_tokens": _tokens(ev)},
              subject=(ev.response_model or ev.model)[:60], cause="length", severity="digest")


@text("output-cap-and-length-continuation")
def _output_cap(n: dict, s: str, cfg: Cfg):
    at = f" at about {n['output_tokens']:,} output tokens" if n.get("output_tokens") else ""
    return (f"{s or 'the model'}: a reply hit the output cap{at}; Hermes asked for a continuation and joined "
            "the pieces without a marker.", None)


@rule("codex-incomplete-ladder", driver="response", core_tells="both", status="fixture")
def codex_incomplete(ev: ApiEvent) -> Iterator[Obs]:
    """On the Codex Responses path a truncated text reply arrives as status incomplete; core allows
    up to 3 continuations per turn."""
    if ev.finish_reason != "incomplete":
        return
    yield Obs("codex-incomplete-ladder", numbers={"output_tokens": _tokens(ev)},
              subject=(ev.response_model or ev.model)[:60], cause="incomplete", severity="digest")


@text("codex-incomplete-ladder")
def _codex_incomplete(n: dict, s: str, cfg: Cfg):
    return (f"{s or 'the model'}: a reply came back incomplete; Hermes continued it automatically "
            "(at most 3 continuations per turn).", None)


def _reason(ev: TurnEvent) -> str:
    return ev.exit_reason.split("(", 1)[0].strip()


@rule("turn-and-subagent-caps", driver="turn", core_tells="both", status="fixture")
def turn_budget(ev: TurnEvent) -> Iterator[Obs]:
    """The turn ended because the iteration budget ran out: the last answer is a forced summary."""
    if _reason(ev) != "budget_exhausted":
        return
    yield Obs("turn-and-subagent-caps", numbers={"exit": "budget_exhausted"}, subject="turn", cause="budget",
              severity="digest")


@text("turn-and-subagent-caps")
def _turn_budget(n: dict, s: str, cfg: Cfg):
    return ("the turn ended on its iteration budget: the model was given a final request without tools and "
            "had to summarise.", None)


_EMPTY = frozenset({"empty_response_exhausted", "all_retries_exhausted_no_response"})


@rule("empty-response-retries", driver="turn", core_tells="user", status="fixture")
def empty_reply(ev: TurnEvent) -> Iterator[Obs]:
    """The turn ended after the empty-reply recovery ladder was used up (each retry re-sends the
    whole input)."""
    reason = _reason(ev)
    if reason not in _EMPTY:
        return
    yield Obs("empty-response-retries", numbers={"exit": reason}, subject="turn", cause=reason, severity="digest")


@text("empty-response-retries")
def _empty_reply(n: dict, s: str, cfg: Cfg):
    return ("the turn ended with no answer after Hermes used up its retries for an empty or thinking-only "
            "reply; each retry sent the whole input again.", None)
