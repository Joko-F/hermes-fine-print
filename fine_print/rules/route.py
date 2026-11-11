"""Detectors of the route family: where a helper model, not the session's own model, looked at an image
or a video, and where an image reached the turn only as text.

Two sources, each stating only what its payload proves.

``aux`` (``post_auxiliary_call``, agent/auxiliary_hooks.py:92-185): every image or video description
goes through ``vision_analyze_tool`` / ``video_analyze_tool``, whose only call site passes
``task="vision"`` (tools/vision_tools.py:756-771, used at 910 and 1143). The same task name carries
the ``vision_analyze`` tool's legacy path (1013-1027), ``video_analyze``, computer-use screenshots
(tools/computer_use/tool.py:923-942), the history replay for a model without vision
(agent/vision_message_prep.py:112) and the pre-turn description of a user-attached image in the CLI and
the gateway (cli_stream_mixin.py:633-679, gateway/run_inbound.py:1845-1888). The last one runs BEFORE
``run_conversation`` opens the turn (agent/turn_facade.py:120), so its hook carries no turn id and
``FinePrint.aux_call`` drops it: this rule never sees a user-attached image routed before the turn;
the ``route`` rule below does. The hook fires once per physical provider attempt (retries and fallbacks
included), and ``_call_vision_llm`` retries an empty answer (vision_tools.py:820-830), so one image can
give several events: one observation per turn and helper model is kept. Nothing in the payload says
whether the main model also received the pixels of that image elsewhere, so no sentence claims it did not.

``route`` (``pre_llm_call``, agent/turn_context.py:849-874): ``original_user_message`` is
``persist_user_message`` when the caller passed one, else ``user_message`` (turn_context.py:1191). The
classic CLI and the gateway both put the substituted text into the staged message that carries the
wrapper (cli_chat_turn_mixin.py:334, run_turn_prepare.py:608-620 strips only a leading timestamp and the
Discord note), so the wrapper is in the string the hook receives. Anchored on the wrapper's exact first
line at the start of a line; the description itself is never read, stored or quoted.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from ..cards import Cfg, text
from ..engine import AuxEvent, Obs, UserTurnEvent, rule
from ..signals import clean_token

_VISION_TASK = "vision"      # tools/vision_tools.py:770 is the only place that sets it


# ── aux: a helper model was asked to look at an image or video ─────────────────────────────────────
@rule("aux-vision-substitution", driver="aux", tools={_VISION_TASK}, core_tells="both", status="fixture")
def vision_helper(ev: AuxEvent) -> Iterator[Obs]:
    """A finished attempt of the vision helper. A failed attempt (``error_type`` set, auxiliary_hooks.py:154-158)
    is its own observation and its own sentence: the hook is per attempt, so it does not say the image
    stayed undescribed."""
    if ev.aux_task != _VISION_TASK:
        return
    model = clean_token(ev.model)
    if not model:
        return
    err = clean_token(ev.error_type, 60)
    if err:
        yield Obs("aux-vision-substitution", numbers={"error": err}, subject=model,
                  cause=f"err:{model}:{err}", severity="digest")
        return
    main = clean_token(ev.main_model)
    numbers = {"same_as_main": 1} if main and main == model else {}
    yield Obs("aux-vision-substitution", numbers=numbers, subject=model, cause=f"ok:{model}",
              severity="digest")


@text("aux-vision-substitution")
def _vision_helper(n: dict, s: str, cfg: Cfg):
    if n.get("error"):
        return (f"vision helper: an attempt to ask {s} to look at an image or video failed ({n['error']}); "
                "this reports one attempt, not the outcome.",
                "ask the agent to look at the image again; if it keeps failing, check the helper with "
                "`hermes config set auxiliary.vision.model <a vision-capable model>`.")
    if n.get("same_as_main"):
        return (f"vision helper: a separate request to {s}, the same model as this session's, was asked "
                "to look at an image or video this turn.", None)
    return (f"vision helper: a helper model ({s}) was asked to look at an image or video this turn.", None)


# ── route: the exact text core writes into the user message in place of an image ───────────────────
# gateway/run_inbound.py:1866-1883, produced by the real function in campaign/s5/route/probe_wrappers.py.
_GW_DESCRIBED = re.compile(r"^\[The user sent an image~ Here's what I can see:\n", re.M)
_GW_FAILED = re.compile(
    r"^\[The user sent an image but (?:I couldn't quite see it this time \(>_<\)|"
    r"something went wrong when I tried to look at it~) You can try (?:looking at it|examining it) yourself "
    r"with vision_analyze using image_url: ", re.M)
_MAX_PARTS = 64
_MAX_COUNT = 99


def _texts(message: Any) -> list[str]:
    """The text of a user message: the string itself or the text parts of a content list."""
    if isinstance(message, str):
        return [message]
    if not isinstance(message, list):
        return []
    out = []
    for part in message[:_MAX_PARTS]:
        value = part.get("text") if isinstance(part, dict) and part.get("type") in ("text", "input_text") else None
        if isinstance(value, str):
            out.append(value)
    return out


def _count(pattern: re.Pattern, chunks: list[str]) -> int:
    total = 0
    for chunk in chunks:
        for _ in pattern.finditer(chunk):
            total += 1
            if total >= _MAX_COUNT:
                return total
    return total


@rule("gateway-text-mode-150-word-image", driver="route", core_tells="model", status="fixture")
def gateway_description(ev: UserTurnEvent) -> Iterator[Obs]:
    """The gateway's own wrapper (gateway/run_inbound.py:1866-1883) is in the user message: it asked the
    helper model for 2-4 sentences, about 150 English words (1852-1857), per image. A failed image has
    its own wrapper and its own numbers: no description, no pixels."""
    chunks = _texts(ev.message)
    if not chunks:
        return
    described, failed = _count(_GW_DESCRIBED, chunks), _count(_GW_FAILED, chunks)
    if described:
        yield Obs("gateway-text-mode-150-word-image", numbers={"images": described}, subject="gateway",
                  cause="described", severity="card")
    if failed:
        yield Obs("gateway-text-mode-150-word-image", numbers={"failed": failed}, subject="gateway",
                  cause="failed", severity="card")


def _images(n: int) -> str:
    return "1 image" if n == 1 else f"{n} images"


@text("gateway-text-mode-150-word-image")
def _gateway_description(n: dict, s: str, cfg: Cfg):
    if n.get("failed"):
        return (f"gateway image routing: {_images(n['failed'])} could not be described by the helper model; "
                "the model got a note with the file path instead of the image.", None)
    return (f"gateway image routing: {_images(n['images'])} reached the model as a helper-model description, "
            "not as pixels; the gateway asked the helper for 2-4 sentences (about 150 words) per image.", None)
