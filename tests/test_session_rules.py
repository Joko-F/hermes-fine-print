"""Session-driver rules: one hit, the nearest misses and garbage payloads per rule, through the public
entry points (``fp.user_turn``, ``fp.api_request``, ``fp.api_response``). Payload shapes follow the core
call sites: ``pre_llm_call`` (agent/turn_context.py:861-873), ``pre_api_request``
(agent/turn_api_request.py:60-108; the request body lists tools in the chat shape,
``body["tools"][i]["function"]["name"]``) and ``post_api_request`` (agent/turn_response_intake.py:76-106,
``context_length`` = the compressor's window)."""

from fine_print import cards
from fine_print.engine import REGISTRY, SessionEvent

SESSION_RULES = (
    "context-length-pin-ignored", "compaction-trigger-floor-and-codex-autoraise", "codex-oauth-context-cap",
)
PIN = "context-length-pin-ignored"


def run(fp, host, *, cfg=None, window=200_000, model="claude-sonnet-5-5", provider="anthropic",
        tool_count=0, first_turn=True, sid="s1"):
    """One session's first request and response; returns {rule_id: Obs} of the session rules."""
    host.cfg.clear()
    host.cfg.update(cfg or {})
    if first_turn is not None:
        fp.user_turn(session_id=sid, turn_id="t1", user_message="hi", conversation_history=[],
                     is_first_turn=first_turn, model=model, platform="cli")
    fp.api_request(session_id=sid, turn_id="t1", model=model, provider=provider, api_mode="chat_completions",
                   tool_count=tool_count, platform="cli")
    fp.api_response(session_id=sid, turn_id="t1", model=model, provider=provider, finish_reason="stop",
                    context_length=window)
    return {o.rule_id: o for o in fp.buffer.get(sid, "t1").obs if o.rule_id in SESSION_RULES}


def line(obs, host):
    return cards.describe(obs.rule_id, obs.numbers, obs.subject, host.config)[0]


def test_all_rules_registered_on_the_session_driver():
    for rid in SESSION_RULES:
        rule = next(r for r in REGISTRY.matching("session", "") if r.id == rid)
        assert rule.status == ("verified" if rid == "context-length-pin-ignored" else "fixture")
    told = {r.id: r.core_tells for r in REGISTRY.matching("session", "")}
    assert told["context-length-pin-ignored"] == "nobody"
    assert told["compaction-trigger-floor-and-codex-autoraise"] == "user"


# ── context-length-pin-ignored ─────────────────────────────────────────────────────────────────────
def test_a_window_above_the_pin_proves_the_pin_is_not_in_force(fp, host):
    got = run(fp, host, cfg={"model.context_length": 32_000}, window=200_000)
    obs = got[PIN]
    assert obs.numbers == {"pinned": 32_000, "window": 200_000} and obs.severity == "card"
    assert line(obs, host) == (
        "model.context_length pins 32,000 tokens in your config, but this session (claude-sonnet-5-5) runs with a "
        "200,000-token window: the pin is not in force. Hermes applies it only while the running model, base URL "
        "and provider all match the configured default.")


def test_a_window_below_the_pin_names_both_causes_and_is_no_card(fp, host):
    """Core lowers a window at run time without looking at the pin (the long-context tier cap, a provider
    limit), so a smaller window does not prove that the pin was dropped."""
    obs = run(fp, host, cfg={"model.context_length": 1_000_000}, window=200_000)[PIN]
    assert obs.numbers == {"pinned": 1_000_000, "window": 200_000} and obs.severity == "digest"
    text = line(obs, host)
    assert text == (
        "model.context_length pins 1,000,000 tokens in your config; this session (claude-sonnet-5-5) runs with a "
        "smaller 200,000-token window. Either the pin is not in force (Hermes applies it only while model, base URL "
        "and provider match the configured default) or Hermes lowered the window after a provider limit; the hook "
        "does not show which.")
    assert "not in force." not in text


def test_pin_given_as_digit_string_counts(fp, host):
    assert "context-length-pin-ignored" in run(fp, host, cfg={"model.context_length": "32000"})


def test_pin_in_force_or_absent_is_silent(fp, host):
    assert "context-length-pin-ignored" not in run(fp, host, cfg={"model.context_length": 200_000}, window=200_000)
    assert "context-length-pin-ignored" not in run(fp, host, window=200_000, sid="s2")           # no pin
    assert "context-length-pin-ignored" not in run(fp, host, cfg={"model.context_length": 32_000}, window=0, sid="s3")


def test_pin_junk_values_and_subagents_are_silent(fp, host):
    for i, junk in enumerate((None, 0, -5, True, "256K", [1], {"a": 1}, 1.5)):
        assert "context-length-pin-ignored" not in run(fp, host, cfg={"model.context_length": junk}, sid=f"j{i}")
    fp.subagent_start(parent_session_id="p", child_session_id="kid")
    assert "context-length-pin-ignored" not in run(fp, host, cfg={"model.context_length": 32_000}, sid="kid")


# ── compaction-trigger-floor-and-codex-autoraise ───────────────────────────────────────────────────
FLOOR = "compaction-trigger-floor-and-codex-autoraise"


def test_the_shipped_default_threshold_yields_nothing(fp, host):
    """0.50 is what every user who set nothing has: no row, no "in your config" for a value nobody chose."""
    assert FLOOR not in run(fp, host, cfg={"compression.threshold": 0.5}, window=200_000, sid="a")
    assert FLOOR not in run(fp, host, window=128_000, sid="b")           # key absent: core default 0.50


def test_a_threshold_the_user_chose_states_the_floor_and_not_when_compaction_starts(fp, host):
    obs = run(fp, host, cfg={"compression.threshold": 0.3}, window=200_000)[FLOOR]
    assert obs.numbers == {"configured": 0.3, "window": 200_000} and obs.severity == "digest"
    text = line(obs, host)
    assert text == (
        "compression.threshold is 0.3 in your config, but this session's window is 200,000 tokens (under "
        "512,000), and Hermes v0.23.0 raises the threshold ratio for such windows to at least 0.75. Other limits "
        "(compression.threshold_tokens, the summary model's own window) can still lower the trigger.")
    assert "later than configured" not in text


def test_threshold_nearest_misses_are_silent(fp, host):
    assert FLOOR not in run(fp, host, cfg={"compression.threshold": 0.75}, window=200_000, sid="a")     # at the floor
    assert FLOOR not in run(fp, host, cfg={"compression.threshold": 0.9}, window=200_000, sid="b")
    assert FLOOR not in run(fp, host, cfg={"compression.threshold": 0.3}, window=512_000, sid="c")      # not under
    assert FLOOR not in run(fp, host, cfg={"compression.threshold": 0.3}, window=1_000_000, sid="d")
    assert FLOOR not in run(fp, host, cfg={"compression.threshold": 0.3}, window=0, sid="e")            # unknown


def test_threshold_not_claimed_when_something_else_sets_the_trigger(fp, host):
    base = {"compression.threshold": 0.3}
    for i, extra in enumerate(({"compression.threshold_tokens": 60_000}, {"compression.enabled": False},
                               {"context.engine": "lcm"}, {"compression.model_thresholds": {"sonnet": 0.9}})):
        assert FLOOR not in run(fp, host, cfg={**base, **extra}, sid=f"x{i}")
    assert FLOOR in run(fp, host, cfg={**base, "compression.model_thresholds": {}, "compression.threshold_tokens": None,
                                       "context.engine": "compressor"}, sid="empty")


def test_threshold_garbage_values_are_silent(fp, host):
    for i, junk in enumerate((None, True, "abc", [0.5], {}, -1, 0)):
        assert FLOOR not in run(fp, host, cfg={"compression.threshold": junk}, sid=f"g{i}")


# ── codex-oauth-context-cap ────────────────────────────────────────────────────────────────────────
CODEX = "codex-oauth-context-cap"


def test_codex_272k_on_a_900k_capable_model_fires(fp, host):
    got = run(fp, host, window=272_000, model="gpt-6.1-sol", provider="openai-codex")
    obs = got[CODEX]
    assert obs.numbers == {"window": 272_000, "variant": 900_000} and obs.subject == "gpt-6.1-sol"
    assert line(obs, host) == (
        "gpt-6.1-sol on the Codex OAuth route runs with a 272,000-token window (the Codex catalog's or Hermes's "
        "own fallback value); Hermes v0.23.0 lists an opt-in gpt-6.1-sol-900k variant of this model with up to "
        "900,000 tokens.")


def test_codex_dated_snapshot_and_vendor_prefix_count(fp, host):
    assert CODEX in run(fp, host, window=272_000, model="openai/gpt-6-sol-2026-09-22", provider="openai-codex")


def test_codex_misses(fp, host):
    assert CODEX not in run(fp, host, window=272_000, model="gpt-5.5", provider="openai-codex", sid="a")      # real limit
    assert CODEX not in run(fp, host, window=272_000, model="gpt-5.4-mini", provider="openai-codex", sid="b")
    assert CODEX not in run(fp, host, window=900_000, model="gpt-6.1-sol", provider="openai-codex", sid="c")
    assert CODEX not in run(fp, host, window=272_000, model="gpt-6.1-sol-900k", provider="openai-codex", sid="d")
    assert CODEX not in run(fp, host, window=272_000, model="gpt-6.1-sol", provider="openrouter", sid="e")
    assert CODEX not in run(fp, host, window=272_000, model="gpt-6-sol-2026-9-22", provider="openai-codex", sid="f")
    assert CODEX not in run(fp, host, window=0, model="gpt-6.1-sol", provider="openai-codex", sid="g")
    assert CODEX not in run(fp, host, cfg={"model.context_length": 272_000}, window=272_000,      # the user's own pin
                            model="gpt-6.1-sol", provider="openai-codex", sid="h")


def test_each_session_is_judged_once(fp, host):
    assert PIN in run(fp, host, cfg={"model.context_length": 32_000})
    fp.user_turn(session_id="s1", turn_id="t2", user_message="again", conversation_history=[{}], is_first_turn=False)
    fp.api_request(session_id="s1", turn_id="t2", model="m", provider="p")
    fp.api_response(session_id="s1", turn_id="t2", model="m", provider="p", finish_reason="stop", context_length=1)
    assert [o.rule_id for o in fp.buffer.get("s1", "t2").obs if o.rule_id in SESSION_RULES] == []


def test_no_rule_is_left_for_a_resumed_session(fp, host):
    """A continued session is a standing fact, not a limit that bit: it yields nothing."""
    assert run(fp, host, first_turn=False) == {}


# ── garbage ────────────────────────────────────────────────────────────────────────────────────────
def test_no_rule_raises_on_garbage_events():
    junk = (None, True, "x", 0, -1, 1.5, [], {}, ["a"], {"a": 1}, object(), ())
    rules = [r for r in REGISTRY.matching("session", "") if r.id in SESSION_RULES]
    assert len(rules) == len(SESSION_RULES)
    for model in junk:
        for window in junk:
            for cfg_value in junk:
                ev = SessionEvent(model=model, provider=model, context_length=window, tool_count=window,
                                  is_first_turn=cfg_value, max_tokens=window,
                                  cfg=lambda key, default, v=cfg_value: v)
                for r in rules:
                    for obs in r.fn(ev) or ():
                        cards.describe(obs.rule_id, obs.numbers, obs.subject, lambda k, d: d)


def test_default_event_yields_nothing():
    for r in REGISTRY.matching("session", ""):
        if r.id in SESSION_RULES:
            assert list(r.fn(SessionEvent()) or ()) == [], r.id
