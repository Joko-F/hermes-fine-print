"""Commit-driver rules: what happened to a tool result between the tool and the conversation.

They look at one committed row (live: ``post_llm_call.conversation_history``; history: a stored
row). The block they parse sits at the very START of the row (or, for the lost-tail fallback, at
its very END), so a file that merely mentions the marker never matches."""

from __future__ import annotations

from typing import Iterator

from .. import constants as C
from ..engine import CommitRow, Obs, Recover, rule
from ..signals import basename, parse_lost_tail, parse_persisted, strip_untrusted


@rule("tool_result.spill_trap", driver="commit", core_tells="model", status="verified")
def spilled(row: CommitRow) -> Iterator[Obs]:
    """A result over the per-result threshold is replaced by a stub with a 1,500-char preview and a
    saved file. Core tells the model where the file is; the user sees nothing. When the file cannot
    be written the row keeps a head and the rest is gone."""
    content = strip_untrusted(row.content)      # web and MCP rows are wrapped after the spill
    block = parse_persisted(content)
    if block:
        shown = block["preview"] or C.SPILL_PREVIEW_CHARS
        total = max(block["total"], row.raw_len or 0)
        saved = basename(block["path"])
        yield Obs("tool_result.spill_trap", numbers={"shown": min(shown, total), "total": total},
                  subject=row.tool, cause=row.tool_call_id or saved,
                  recover=(Recover.names(saved),) if saved else ())
        return
    lost = parse_lost_tail(content)
    if lost and (row.raw_len is None or row.raw_len >= lost["total"]):
        yield Obs("tool_result.spill_trap", numbers={"shown": lost["shown"], "total": lost["total"], "lost": 1},
                  subject=row.tool, cause=row.tool_call_id or f"{row.tool}:{lost['total']}")
