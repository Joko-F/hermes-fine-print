"""Detectors of the session family: once per session, after its first provider response, compare what
the user's configuration asks for with what Hermes runs with (``SessionEvent``, built in
``FinePrint._session_check``). ``context_length`` is the window the session's compressor resolved,
reported with the response (agent/turn_response_intake.py:101); ``cfg`` reads the user's effective
config. Every sentence states those values and the v0.23.0 rule of core; none claims which of several
possible causes applied unless the payload shows it. A rule fires at most once per session by
construction. A standing fact of a default setup is bookkeeping (severity ``log``); a value the user
set, or a pin that is not in force, is a ``digest`` line.

The numbers copied from core (v0.23.0) are at the top, with file:line.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

from ..cards import Cfg, text
from ..engine import Obs, SessionEvent, rule
from ..signals import clean_token

# agent/context_compressor.py:1151-1152, applied by _effective_threshold_percent (:2752-2756): below this
# window the compaction threshold ratio is raised to at least the floor (raise-only).
SMALL_CTX_WINDOW_LIMIT = 512_000
SMALL_CTX_THRESHOLD_FLOOR = 0.75
# agent/model_metadata.py:1684: the only Codex OAuth window the -900k variants replace; the variants
# resolve to 900,000 (model_metadata.py:1673-1683).
CODEX_OAUTH_WINDOW = 272_000
CODEX_900K_WINDOW = 900_000
# agent/model_metadata.py:1688-1689: -900k exists only for these bases (and dated snapshots of the first
# five); a model not listed (gpt-5.5, gpt-5.4-mini ...) genuinely stops at 272K.
_CODEX_SNAPSHOT_BASES = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-sol", "gpt-6-luna")
_CODEX_900K_BASES = frozenset({*_CODEX_SNAPSHOT_BASES, "gpt-5.4", "gpt-daybreak-blue-latest", "gpt-6-astra",
                               "gpt-6.1-sol"})
# hermes_cli/config_defaults.py:573-576: the shipped default of compression.threshold.
DEFAULT_THRESHOLD = 0.50


def _int(value: Any) -> Optional[int]:
    """A positive int as core reads one (an int, or a string of digits); anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None


def _ratio(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _off(value: Any) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() in ("false", "off", "no", "0"))


# ── model.context_length pin ───────────────────────────────────────────────────────────────────────
@rule("context-length-pin-ignored", driver="session", core_tells="nobody", status="fixture")
def pin_not_in_effect(ev: SessionEvent) -> Iterator[Obs]:
    """``model.context_length`` is a durable pin: when it is applied, the compressor's window IS the pin
    (agent/model_metadata.py:2221-2223), and that window is what the response hook reports
    (agent/turn_response_intake.py:101). Core applies the pin only while model, base URL and provider
    match the configured default, else drops it with a debug log only (agent/agent_init.py:1606-1650; the
    live paths re-scope it the same way, :1668-1696). So a positive pin that differs from the reported
    window is a pin that is not in force. The payload has no base URL and no normalized model, so it
    cannot say which of the three differs and the text does not. Subagents are left out: their window
    is not the user's session window."""
    if ev.is_subagent or not isinstance(ev.context_length, int) or ev.context_length <= 0:
        return
    pin = _int(ev.cfg("model.context_length", None))
    if pin is None or pin == ev.context_length:
        return
    yield Obs("context-length-pin-ignored", numbers={"pinned": pin, "window": ev.context_length},
              subject=clean_token(ev.model), cause=f"{pin}:{ev.context_length}", severity="digest")


@text("context-length-pin-ignored")
def _pin_not_in_effect(n: dict, s: str, cfg: Cfg):
    who = f" {s}" if s else ""
    return (f"model.context_length pins {n['pinned']:,} tokens in your config; this session{who} runs with a "
            f"{n['window']:,}-token window (Hermes applies the pin only while the running model, base URL "
            "and provider all match the configured default; the hook does not show which one differs).",
            "Run the configured default model, provider and base URL (model.default, model.provider, "
            "model.base_url) so the pin applies, or accept the detected window shown in /usage.")


# ── compression.threshold floor ────────────────────────────────────────────────────────────────────
@rule("compaction-trigger-floor-and-codex-autoraise", driver="session", core_tells="user", status="fixture")
def threshold_floor(ev: SessionEvent) -> Iterator[Obs]:
    """The built-in compressor raises the threshold ratio to at least 0.75 for any window under 512,000
    (agent/context_compressor.py:2184-2185, 2752-2756), after the per-model override and the Codex
    autoraise have chosen the base (agent/agent_init.py:1365-1393), so for such a window the trigger is
    at least 0.75 whatever the configured ratio. Not claimed when: the window is unknown or not under the
    limit; ``compression.threshold_tokens`` is set (an absolute cap lowers the trigger below the ratio,
    context_compressor.py:2740-2749); compression is off; ``context.engine`` names another engine (the
    floor is the built-in compressor's, agent_init.py:1803-1822); ``compression.model_thresholds`` is
    non-empty (the base is then not the configured ratio). The Codex autoraise to 0.85 stays out of the
    sentence: it only moves the trigger further up and has its own one-time notice. With the shipped
    default (0.50) this is true of nearly every session, so it is bookkeeping; a value the user chose is
    a digest line."""
    window = ev.context_length
    if ev.is_subagent or not isinstance(window, int) or not 0 < window < SMALL_CTX_WINDOW_LIMIT:
        return
    configured = _ratio(ev.cfg("compression.threshold", DEFAULT_THRESHOLD))
    if configured is None or not 0 < configured < SMALL_CTX_THRESHOLD_FLOOR:
        return
    if ev.cfg("context.engine", "compressor") not in ("compressor", "", None):
        return
    if _off(ev.cfg("compression.enabled", True)):
        return
    if ev.cfg("compression.threshold_tokens", None) not in (None, "", 0, False):
        return
    if ev.cfg("compression.model_thresholds", None):
        return
    yield Obs("compaction-trigger-floor-and-codex-autoraise",
              numbers={"configured": round(configured, 4), "window": window},
              subject="compression.threshold", cause=f"{configured}:{window}",
              severity="log" if configured == DEFAULT_THRESHOLD else "digest")


@text("compaction-trigger-floor-and-codex-autoraise")
def _threshold_floor(n: dict, s: str, cfg: Cfg):
    return (f"compression.threshold is {n['configured']:g} in your config, but this session's window is "
            f"{n['window']:,} tokens (under {SMALL_CTX_WINDOW_LIMIT:,}), and Hermes v0.23.0 raises the "
            f"compaction trigger for such windows to at least {SMALL_CTX_THRESHOLD_FLOOR:g} of the usable "
            "window, so compaction starts later than configured.", None)


# ── Codex OAuth window ─────────────────────────────────────────────────────────────────────────────
def _codex_slug(model: Any) -> str:
    return str(model or "").strip().lower().rsplit("/", 1)[-1]      # agent/model_metadata.py:1693-1699


def _has_900k_variant(model: Any) -> bool:
    """Copy of ``is_codex_900k_base`` (agent/model_metadata.py:1702-1710)."""
    slug = _codex_slug(model)
    if not slug or slug.endswith("-900k"):
        return False
    if slug in _CODEX_900K_BASES:
        return True
    for base in _CODEX_SNAPSHOT_BASES:
        rest = slug[len(base) + 1:]
        if slug.startswith(base + "-") and len(rest) == 10 and rest[4] == rest[7] == "-" \
                and (rest[:4] + rest[5:7] + rest[8:]).isdigit():
            return True
    return False


@rule("codex-oauth-context-cap", driver="session", core_tells="nobody", status="fixture")
def codex_window(ev: SessionEvent) -> Iterator[Obs]:
    """On ``openai-codex`` the window is the Codex catalog's (live probe) or Hermes's static table's
    (agent/model_metadata.py:1656-1664); 272,000 is the one value the opt-in ``-900k`` variants replace
    (:1684), and only for the bases in ``_CODEX_900K_BASES``. Fires only for such a base at exactly
    272,000 with no ``model.context_length`` pin. It cannot tell the catalog from the fallback table, and
    for models without a variant (gpt-5.5, gpt-5.4-mini) 272K is the real limit, so those stay silent."""
    if ev.is_subagent or ev.provider != "openai-codex" or ev.context_length != CODEX_OAUTH_WINDOW:
        return
    if not _has_900k_variant(ev.model) or _int(ev.cfg("model.context_length", None)) is not None:
        return
    slug = clean_token(_codex_slug(ev.model))
    yield Obs("codex-oauth-context-cap", numbers={"window": ev.context_length, "variant": CODEX_900K_WINDOW},
              subject=slug, cause=slug, severity="digest")


@text("codex-oauth-context-cap")
def _codex_window(n: dict, s: str, cfg: Cfg):
    return (f"{s} on the Codex OAuth route runs with a {n['window']:,}-token window (the Codex catalog's or "
            f"Hermes's own fallback value); Hermes v0.23.0 lists an opt-in {s}-900k variant of this model "
            f"with up to {n['variant']:,} tokens.", None)


# ── resumed session ────────────────────────────────────────────────────────────────────────────────
@rule("stale-system-prompt-and-pinned-tools-on-resume", driver="session", core_tells="nobody", status="fixture")
def resumed_session(ev: SessionEvent) -> Iterator[Obs]:
    """``is_first_turn`` is ``not bool(conversation_history)`` at ``pre_llm_call``
    (agent/turn_context.py:869): False means the session already had history when this process first saw
    it. With history and a stored prompt that matches the runtime, core replays the stored prompt
    (agent/conversation_loop.py:811-868) and re-pins the stored tool list (:749-768); a prompt-refresh
    request or a prompt that no longer matches the runtime rebuilds instead, and the payload cannot show
    which happened. It cannot show that anything was edited either, so the sentence says what a resumed
    session normally keeps, not that something is out of date. ``None`` (not seen) is unknown.
    Bookkeeping only: every continued session is one."""
    if ev.is_subagent or ev.is_first_turn is not False:
        return
    yield Obs("stale-system-prompt-and-pinned-tools-on-resume", numbers={}, subject="resumed",
              cause="resumed", severity="log")


@text("stale-system-prompt-and-pinned-tools-on-resume")
def _resumed_session(n: dict, s: str, cfg: Cfg):
    return ("this session continues earlier turns; a continuing session normally replays its stored system "
            "prompt and tool list, so later edits to SOUL.md, project context files or skills do not reach "
            "it until /new, a compaction or `hermes sessions refresh-prompts`.", None)
