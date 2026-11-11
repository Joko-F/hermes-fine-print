"""Compaction-driver rules. ``post_compaction`` carries counts only (agent/compaction_events.py:239-259):
``kind``, ``reason`` (the trigger), estimated token counts before and after, and the net change of the
message count. Every sentence here states those and nothing about which messages or tool results were
touched. Kinds (compaction_events.py:36): ``summarize`` replaces a middle window with a summary,
``micro_summarize`` absorbs an exchange into a rolling summary, ``prune_tool_results`` is a
deterministic prune with the message count unchanged (compaction_events.py:224-232)."""

from __future__ import annotations

from typing import Iterator

from ..cards import Cfg, text
from ..engine import CompactionEvent, Obs, rule

_KINDS = frozenset({"summarize", "micro_summarize", "prune_tool_results"})


@rule("compaction-stubs-old-tool-results-before-summary", driver="compaction", core_tells="both",
      status="verified")
def compacted(ev: CompactionEvent) -> Iterator[Obs]:
    """A committed compaction. A card only for ``summarize`` with a net message loss, the one kind
    that replaces part of the conversation with a summary; the others are logged."""
    if ev.kind not in _KINDS:
        return
    numbers: dict = {"kind": ev.kind, "reason": ev.reason[:24]}
    if ev.before_tokens is not None and ev.after_tokens is not None:
        numbers["total"], numbers["shown"] = ev.before_tokens, min(ev.after_tokens, ev.before_tokens)
        numbers["after"] = ev.after_tokens
    if ev.messages_removed is not None:
        numbers["messages_removed"] = ev.messages_removed
    big = ev.kind == "summarize" and (ev.messages_removed or 0) > 0
    yield Obs("compaction-stubs-old-tool-results-before-summary", numbers=numbers, subject=ev.kind,
              cause=f"{ev.kind}:{ev.before_tokens}:{ev.after_tokens}:{ev.messages_removed}",
              severity="card" if big else "digest")


_LABEL = {"summarize": "context compaction", "micro_summarize": "micro-compaction",
          "prune_tool_results": "tool-result prune"}


@text("compaction-stubs-old-tool-results-before-summary")
def _compacted(n: dict, s: str, cfg: Cfg):
    kind = n.get("kind") or "summarize"
    head = f"{_LABEL.get(kind, 'compaction')} ({kind}, trigger {n.get('reason') or 'unknown'})"
    removed = n.get("messages_removed")
    if removed is None:
        count = ""
    elif removed > 0:
        count = f"net message count fell by {removed}"
    elif removed == 0:
        count = "the message count did not change"
    else:
        count = f"net message count grew by {-removed}"
    size = ""
    if n.get("total") is not None and n.get("after") is not None:
        size = f"estimated tokens went from {n['total']:,} to {n['after']:,}"
    parts = [p for p in (count, size) if p]
    return (f"{head}: {'; '.join(parts)}." if parts else f"{head}.", None)


@rule("deterministic-fallback-summary", driver="compaction", core_tells="both", status="fixture")
def skeleton_summary(ev: CompactionEvent) -> Iterator[Obs]:
    """A compression summary call (``post_auxiliary_call`` with aux_task=compression) reported an
    error shortly before a committed ``summarize`` compaction. The hook does not say which summary
    was committed, so this is an observation about the call, not about the result."""
    if ev.kind != "summarize" or not ev.summarizer_failed:
        return
    yield Obs("deterministic-fallback-summary", numbers={"reason": ev.reason[:24]}, subject="summary",
              cause=f"fallback:{ev.before_tokens}:{ev.after_tokens}", severity="card")


@text("deterministic-fallback-summary")
def _skeleton_summary(n: dict, s: str, cfg: Cfg):
    return ("context compaction: a compaction summary call failed shortly before this compaction "
            f"(trigger {n.get('reason') or 'unknown'}).", None)
