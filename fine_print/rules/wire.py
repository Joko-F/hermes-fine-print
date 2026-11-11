"""Wire- and api_error-driver rules: what a provider request carries compared with what the conversation
holds, and what a failed request says about retries. They read structure, counts and sizes only.

Facts every rule here rests on (checked against the v0.23.0 core source and by calling the real core
functions, see ``campaign/s5/wire/probe_eviction.py``):

* ``pre_api_request`` is fired after the request is built (agent/turn_api_request.py:193-199, hook body
  :60-108): ``request_messages`` is the final provider-shaped list, ``conversation_history`` is
  ``list(messages)``, the conversation itself. Eviction and the image strip rewrite the per-call clone
  ``api_messages`` only (agent/turn_request_assembly.py:155-165, agent/turn_api_request.py:133-134), so the
  history still holds every image when the hook runs.
* Images have three shapes (``engine.image_sizes`` reads all of them): chat ``image_url`` parts
  (tool results as ``content`` lists: agent/tool_executor.py:1143-1146), Anthropic ``image`` blocks inside
  ``tool_result`` blocks (agent/anthropic_message_convert.py:443-478) and Responses ``input_image``
  parts inside ``function_call_output.output`` (agent/codex_responses_adapter.py:628-645).
* Several mechanisms make a request carry fewer images than the history: send-path eviction
  (agent/context_compressor.py:1512-1550, agent/anthropic_message_convert.py:638-686), the image strip for a
  model that rejected images (agent/message_sanitization.py:364-397, 446-457), a text-only model (images
  become text: agent/vision_message_prep.py:188-202), provider conversion of an unsupported subtype to a
  text placeholder (agent/anthropic_message_convert.py:167-179, agent/codex_responses_adapter.py:337-338)
  and a downgrade of tool results to their text summary (agent/vision_message_prep.py:207-235). Only
  eviction writes a recognisable text into the request, so only that is named, and only when the request
  shows more tool results with that text than the history does. (Live check, Anthropic: 22 held, 14 sent,
  one batch of 8; the evicted results carry the text as a JSON string, see ``_shows_placeholder``.)
"""

from __future__ import annotations

from typing import Any, Iterator

from ..cards import Cfg, text
from ..engine import ApiErrorEvent, Obs, WireEvent, rule
from ..signals import clean_token

# The placeholder core writes in place of an evicted image: agent/context_compressor.py:1451 (chat shape,
# also the Responses shape: its tool-result parts are converted from it) and
# agent/anthropic_message_convert.py:683 (Anthropic shape).
_EVICTED = "[screenshot removed to save context]"
_TEXT_PARTS = frozenset({"text", "input_text"})
_IMAGE_PARTS = frozenset({"image_url", "image", "input_image"})


def _walk(messages: Any, visit: Any, depth: int = 3) -> None:
    """Call ``visit(part)`` for every dict part of every message's ``content`` / ``output`` list, and of
    the lists nested in a part (Anthropic ``tool_result``), the way ``engine.image_sizes`` walks."""
    def go(parts: list, left: int) -> None:
        for part in parts:
            if not isinstance(part, dict):
                continue
            visit(part)
            if left > 0 and part.get("type") not in _IMAGE_PARTS:
                for key in ("content", "output"):
                    inner = part.get(key)
                    if isinstance(inner, list):
                        go(inner, left - 1)

    for message in messages if isinstance(messages, list) else ():
        if isinstance(message, dict):
            for key in ("content", "output"):
                inner = message.get(key)
                if isinstance(inner, list):
                    go(inner, depth)


# In the Anthropic request an evicted tool result is NOT a list of blocks: the chat-shaped pass runs first
# (agent/turn_request_assembly.py:165), the converter then finds no image block in the list and falls back to
# ``json.dumps(content)`` (agent/anthropic_message_convert.py:455-462), so ``tool_result.content`` is the
# JSON text of the part list (probe_anthropic_order.py). Only its two ends are checked (never a scan of the
# string): the placeholder part is first or last when a tool result is "text, image" or "image, text".
_PART = '{"type": "text", "text": "' + _EVICTED + '"}'
_JSON_OPEN = '[' + _PART
_JSON_CLOSE = _PART + ']'


def _shows_placeholder(content: Any) -> bool:
    if isinstance(content, str):
        return content.startswith(_JSON_OPEN) or content.endswith(_JSON_CLOSE)
    return isinstance(content, list) and any(
        isinstance(p, dict) and p.get("type") in _TEXT_PARTS and p.get("text") == _EVICTED for p in content)


def _placeholders(messages: Any) -> int:
    """Tool results that show core's eviction placeholder: a message (chat ``tool`` row, Responses
    ``function_call_output``) or an Anthropic ``tool_result`` block whose content is, or serializes, a part
    list holding exactly that text part. Structure only, never a substring search over content."""
    n = 0
    for message in messages if isinstance(messages, list) else ():
        if not isinstance(message, dict):
            continue
        for key in ("content", "output"):
            inner = message.get(key)
            if _shows_placeholder(inner) and (key == "output" or message.get("role") == "tool"):
                n += 1
            elif isinstance(inner, list):
                n += sum(1 for blk in inner if isinstance(blk, dict) and blk.get("type") == "tool_result"
                         and _shows_placeholder(blk.get("content")))
    return n


@rule("outbound-image-eviction", driver="wire", core_tells="model", status="verified")
def image_gap(ev: WireEvent) -> Iterator[Obs]:
    """The conversation holds M image parts, this request carries K < M. Returns at once when the history
    holds none (text-only sessions pay one pass over the history, cached on the event). When the request
    shows more tool results with core's eviction placeholder than the history does (``marked``), those are
    evictions by core's own text; the other mechanisms above leave no marker, so then only the numbers are
    stated. ``marked`` counts tool results, not images (one result can carry several). ``cause`` is the
    shape of the gap (``evict`` or ``absent`` plus the missing count): the same state on the next request
    is one observation, a larger gap is a new one. Severity: a gap that core's own placeholder marks as
    eviction is a card (pixels the agent loaded are gone from what the model sees, and the user is not
    told); a gap with no marker has several possible causes and stays a digest line."""
    held = ev.history_images
    if not held:
        return
    sent = ev.request_images
    missing = len(held) - len(sent)
    if missing <= 0:
        return
    marked = max(0, _placeholders(ev.request) - _placeholders(ev.history))
    kind = "evict" if marked else "absent"
    yield Obs("outbound-image-eviction",
              numbers={"held": len(held), "sent": len(sent), "missing": missing, "marked": marked,
                       "sent_kb": sum(sent) // 1024, "total": len(held), "shown": len(sent)},
              subject=clean_token(ev.model), cause=f"{kind}:{missing}", severity="card" if marked else "digest")


@text("outbound-image-eviction")
def _image_gap(n: dict, s: str, cfg: Cfg):
    held, sent, missing, marked = n["held"], n["sent"], n["missing"], n.get("marked", 0)
    size = f" ({n['sent_kb']:,} KB encoded)" if sent and n.get("sent_kb") else ""
    head = f"{s}: " if s else ""
    if not marked:
        return (f"{head}{missing} of the {held} images in the conversation were not in this request; "
                f"it carries {sent}{size}.", None)
    results = f"{marked} tool result" + ("" if marked == 1 else "s")
    return (f"{head}{missing} of the {held} images in the conversation were not in this request; it carries {sent}{size}. "
            f"{results} in the request show Hermes's {_EVICTED!r} placeholder.", None)


@rule("openai-detail-never-set", driver="wire", core_tells="nobody", status="fixture")
def image_detail(ev: WireEvent) -> Iterator[Obs]:
    """The Responses request the hook receives is final: ``preflight_kwargs`` and the request middleware
    run before the hook (agent/turn_api_request.py:143-149, 156-164, 193), and the part is built without a
    ``detail`` unless the stored part carried one (agent/codex_responses_adapter.py:322-342). Counts the
    ``input_image`` parts of this request that have no ``detail`` field. Fires once per session (the cause
    is constant): the number is a fact about the request shape, not a loss."""
    if ev.api_mode != "codex_responses" or not ev.history_images or not ev.request_images:
        return
    plain = total = 0

    def visit(part: dict) -> None:
        nonlocal plain, total
        if part.get("type") == "input_image":
            total += 1
            plain += not (isinstance(part.get("detail"), str) and part["detail"].strip())

    _walk(ev.request, visit)
    if plain:
        yield Obs("openai-detail-never-set", numbers={"plain": plain, "images": total},
                  subject=clean_token(ev.model), cause="detail", severity="log")


@text("openai-detail-never-set")
def _image_detail(n: dict, s: str, cfg: Cfg):
    head = f"{s}: " if s else ""
    return (f"{head}{n['plain']} of the {n['images']} images in this Responses request have no 'detail' "
            "field, so the provider's default applies.", None)


@rule("transport-retries-hidden", driver="api_error", core_tells="user", status="fixture")
def retries(ev: ApiErrorEvent) -> Iterator[Obs]:
    """``api_request_error`` fires for every failed attempt, before recovery and before ``retry_count`` is
    incremented (agent/turn_api_error.py:137-152), so the count it carries is the number of retries that
    came before this failure. A failure with ``retry_count >= 1`` and a retryable classification is
    therefore evidence of at least that many earlier retries of the same call (the counter is per API
    call: agent/conversation_loop.py:1761); the first failure (0) is not. The turn keeps the first such
    observation (``FinePrint._attach`` keeps one per rule and cause), so a turn reports the first retried
    failure, not the last."""
    if ev.retryable is not True or ev.retry_count < 1:
        return
    yield Obs("transport-retries-hidden",
              numbers={"retries": ev.retry_count, "max": ev.max_retries or 0, "status": ev.status_code or 0,
                       "kind": clean_token(ev.error_type, 30)},
              subject=clean_token(ev.model), cause="retries", severity="digest")


@text("transport-retries-hidden")
def _retries(n: dict, s: str, cfg: Cfg):
    head = f"{s}: " if s else ""
    what = f"HTTP {n['status']}" if n.get("status") else (n.get("kind") or "an error")
    limit = f" (retry limit {n['max']})" if n.get("max") else ""
    k = n["retries"]
    return (f"{head}a provider request failed ({what}) after {k} earlier {'retry' if k == 1 else 'retries'}"
            f"{limit}.", None)
