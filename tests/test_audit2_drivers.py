"""Audit round 2, drivers: every sentence from the compaction and response modules states what the hook
payload carries, and no rule claims a signal the main loop never delivers."""

import re

import pytest

from fine_print.cards import describe
from fine_print.engine import REGISTRY, ApiEvent, CompactionEvent, TurnEvent
from fine_print.rules import response


def compaction(kind, reason="turn_start_threshold", before=182_000, after=41_000, removed=34, failed=False):
    return REGISTRY.run("compaction", "", CompactionEvent(kind, reason, before, after, removed, failed))


def api(finish, mode="codex_responses", **usage):
    return REGISTRY.run("response", "", ApiEvent("gpt", "openai-codex", "gpt", finish, mode, usage, 0, 1))


def line(obs):
    return describe(obs.rule_id, obs.numbers, obs.subject, lambda key, default: default)[0]


# ── compaction: only hook facts, per kind ─────────────────────────────────────────────────────────
def test_summarize_with_a_net_loss_is_the_only_card_and_states_counts():
    (obs,) = [o for o in compaction("summarize") if o.rule_id.startswith("compaction-stubs")]
    assert obs.severity == "card"
    assert line(obs) == ("context compaction (summarize, trigger turn_start_threshold): "
                         "net message count fell by 34; estimated tokens went from 182,000 to 41,000.")


@pytest.mark.parametrize("kind,removed", [("micro_summarize", 2), ("prune_tool_results", 0), ("summarize", 0)])
def test_other_kinds_and_no_net_loss_are_digests(kind, removed):
    (obs,) = compaction(kind, removed=removed)
    assert obs.severity == "digest"


def test_prune_says_the_message_count_did_not_change_and_nothing_about_stubs():
    (obs,) = compaction("prune_tool_results", reason="proactive_prune", before=90_000, after=70_000, removed=0)
    assert line(obs) == ("tool-result prune (prune_tool_results, trigger proactive_prune): the message count did "
                         "not change; estimated tokens went from 90,000 to 70,000.")


def test_compaction_text_never_claims_per_result_state():
    for kind in ("summarize", "micro_summarize", "prune_tool_results"):
        for obs in compaction(kind, removed=5):
            text = line(obs).lower()
            assert not re.search(r"stub|one-line|tail|replaced|no longer|skeleton", text), text


def test_missing_numbers_are_left_out_not_invented():
    ev = CompactionEvent("summarize", "manual", None, None, None, False)
    (obs,) = REGISTRY.run("compaction", "", ev)
    assert line(obs) == "context compaction (summarize, trigger manual)."
    assert obs.severity == "digest"


def test_fallback_sentence_reports_the_failed_call_not_the_committed_summary():
    obs = [o for o in compaction("summarize", failed=True) if o.rule_id == "deterministic-fallback-summary"]
    assert len(obs) == 1
    sentence = line(obs[0])
    assert sentence == ("context compaction: a compaction summary call failed shortly before this compaction "
                        "(trigger turn_start_threshold).")
    assert "skeleton" not in sentence and "replaced" not in sentence
    assert not [o for o in compaction("prune_tool_results", failed=True) if o.rule_id == "deterministic-fallback-summary"]


# ── response: the length rule is gone, the rest says what the payload shows ───────────────────────
def test_there_is_no_length_rule_and_a_length_response_yields_nothing():
    assert REGISTRY.get("output-cap-and-length-continuation") is None
    assert not hasattr(response, "output_cap")
    assert api("length", mode="chat_completions", completion_tokens=8192) == []
    assert api("length") == []


def test_codex_incomplete_sentence_has_no_claim_about_what_happened_next():
    (obs,) = api("incomplete", output_tokens=4096)
    assert line(obs) == "gpt: a Codex Responses reply ended with finish reason 'incomplete' (4,096 output tokens)."
    assert not re.search(r"continu|automatic|at most|truncat|cap", line(obs))


def test_incomplete_outside_the_codex_mode_is_not_reported():
    assert api("incomplete", mode="chat_completions") == []


def run_turn(reason):
    return REGISTRY.run("turn", "", TurnEvent(reason, False, False, False))


def test_budget_exit_as_the_finalizer_delivers_it():
    (obs,) = run_turn("max_iterations_reached(90/90)")
    assert obs.numbers == {"exit": "max_iterations_reached", "calls": 90, "limit": 90}
    assert line(obs) == "the turn ended with exit reason max_iterations_reached (90 of 90 iterations)."
    (bare,) = run_turn("budget_exhausted")
    assert line(bare) == "the turn ended with exit reason budget_exhausted (the iteration budget ran out)."
    assert run_turn("error_near_max_iterations(3/90)") == []
    assert "summar" not in line(obs) + line(bare)


@pytest.mark.parametrize("reason,tail", [
    ("empty_response_exhausted", "the model's replies stayed empty after Hermes's empty-reply retries."),
    ("all_retries_exhausted_no_response", "no provider response arrived after all retries."),
])
def test_empty_reply_sentences(reason, tail):
    (obs,) = run_turn(reason)
    assert line(obs) == f"the turn ended with exit reason {reason}: {tail}"
    assert "whole input" not in line(obs)
