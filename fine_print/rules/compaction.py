"""Compaction-driver rules: what a committed compaction replaced. ``post_compaction`` carries counts
only (kind, reason, token estimates, messages removed), so that is all these rules report."""

from __future__ import annotations

from typing import Iterator

from ..cards import Cfg, text
from ..engine import CompactionEvent, Obs, rule

_KINDS = frozenset({"summarize", "micro_summarize", "prune_tool_results"})


@rule("compaction-stubs-old-tool-results-before-summary", driver="compaction", core_tells="both",
      status="verified")
def compacted(ev: CompactionEvent) -> Iterator[Obs]:
    """Before the summary is written, every tool result outside the protected tail and over 200
    chars becomes a one-line stub. Core shows a status line with no numbers."""
    if ev.kind not in _KINDS:
        return
    numbers: dict = {"kind": ev.kind, "reason": ev.reason[:24]}
    if ev.before_tokens is not None and ev.after_tokens is not None:
        numbers["total"], numbers["shown"] = ev.before_tokens, min(ev.after_tokens, ev.before_tokens)
    if ev.messages_removed is not None:
        numbers["messages_removed"] = ev.messages_removed
    big = ev.kind == "summarize" and "total" in numbers and (ev.messages_removed or 0) > 0
    yield Obs("compaction-stubs-old-tool-results-before-summary", numbers=numbers, subject=ev.kind,
              cause=f"{ev.kind}:{ev.before_tokens}:{ev.after_tokens}:{ev.messages_removed}",
              severity="card" if big else "digest")


@text("compaction-stubs-old-tool-results-before-summary")
def _compacted(n: dict, s: str, cfg: Cfg):
    removed = n.get("messages_removed")
    what = f"{removed} messages were replaced by a summary" if removed else "older context was reduced"
    size = f", about {n['total']:,} to {n['shown']:,} tokens" if n.get("total") else ""
    return (f"context compaction ({n.get('reason') or n.get('kind', 'auto')}): {what}{size}. Tool results "
            "outside the recent tail are one-line stubs now.", None)


@rule("deterministic-fallback-summary", driver="compaction", core_tells="both", status="fixture")
def skeleton_summary(ev: CompactionEvent) -> Iterator[Obs]:
    """The summary model failed right before a committed summarize compaction: the middle of the
    conversation is then most likely the model-free skeleton (at most 8,000 chars)."""
    if ev.kind != "summarize" or not ev.summarizer_failed:
        return
    yield Obs("deterministic-fallback-summary", numbers={"reason": ev.reason[:24]}, subject="summary",
              cause=f"fallback:{ev.before_tokens}:{ev.after_tokens}", severity="card")


@text("deterministic-fallback-summary")
def _skeleton_summary(n: dict, s: str, cfg: Cfg):
    return ("context compaction: the summary model call failed just before this compaction, so the middle "
            "of the conversation was most likely replaced by a skeleton built without a model "
            "(at most 8,000 chars).", None)
