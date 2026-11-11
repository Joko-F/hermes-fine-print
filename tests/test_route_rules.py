"""Route-family detectors: the vision helper (aux driver) and the gateway's image-description wrapper
(route driver). Payload shapes come from the core call sites cited in fine_print/rules/route.py; the
wrapper strings are the output of the real core function (campaign/s5/route/probe_wrappers.py)."""

import pytest

from fine_print.cards import describe
from fine_print.engine import REGISTRY

CFG = lambda key, default: default  # noqa: E731

# Output of GatewayInboundMixin._enrich_message_with_vision (gateway/run_inbound.py:1845-1888) with
# vision_analyze_tool stubbed; the user text was "hi there".
GW_OK = ("[The user sent an image~ Here's what I can see:\nA red square.]\n"
         "[If you need a closer look, use vision_analyze with image_url: /tmp/a.png ~]\n\nhi there")
GW_FAIL = ("[The user sent an image but I couldn't quite see it this time (>_<) You can try looking at it "
           "yourself with vision_analyze using image_url: /tmp/a.png]\n\nhi there")
GW_RAISE = ("[The user sent an image but something went wrong when I tried to look at it~ You can try "
            "examining it yourself with vision_analyze using image_url: /tmp/a.png]\n\nhi there")
GW_TWO = ("[The user sent an image~ Here's what I can see:\nA red square.]\n"
          "[If you need a closer look, use vision_analyze with image_url: /tmp/a.png ~]\n\n"
          "[The user sent an image~ Here's what I can see:\nA red square.]\n"
          "[If you need a closer look, use vision_analyze with image_url: /tmp/b.png ~]")
# hermes_cli/cli_stream_mixin.py:659 and agent/vision_message_prep.py:126 write this one (not the gateway's)
CLI_OK = ("[The user attached an image. Here's what it contains:\nA red square.]\n"
          "[If you need a closer look, use vision_analyze with image_url: /tmp/a.png]\n\nhi")


def line(obs):
    return describe(obs.rule_id, obs.numbers, obs.subject, CFG)[0]


def fix(obs):
    return describe(obs.rule_id, obs.numbers, obs.subject, CFG)[1]


def aux(fp, **over):
    payload = dict(aux_task="vision", model="gemini-2.5-flash", provider="openrouter", session_id="s1",
                   turn_id="t1", message_count=1, api_duration=1.5, error=None, error_type=None)
    payload.update(over)
    fp.aux_call(**payload)
    return [o for o in fp.buffer.get(payload["session_id"], payload["turn_id"]).obs
            if o.rule_id == "aux-vision-substitution"]


def turn(fp, message, **over):
    payload = dict(session_id="s1", turn_id="t1", user_message=message, conversation_history=[],
                   is_first_turn=True, model="deepseek-chat", platform="telegram", parent_session_id="")
    payload.update(over)
    fp.user_turn(**payload)
    return [o for o in fp.buffer.get(payload["session_id"], payload["turn_id"]).obs
            if o.rule_id == "gateway-text-mode-150-word-image"]


# ── aux-vision-substitution ──────────────────────────────────────────────────────────────────────
def test_registered_with_the_catalogue_told():
    for rid, tells, status in (("aux-vision-substitution", "both", "verified"),
                               ("gateway-text-mode-150-word-image", "model", "fixture")):
        rule = REGISTRY.get(rid)
        assert rule.status == status and rule.core_tells == tells
    assert REGISTRY.get("aux-vision-substitution").driver == "aux"
    assert REGISTRY.get("aux-vision-substitution").tools == frozenset({"vision"})


def test_vision_call_is_a_digest_naming_the_helper(fp):
    (obs,) = aux(fp)
    assert obs.severity == "digest" and obs.subject == "gemini-2.5-flash" and obs.numbers == {}
    assert line(obs) == "vision helper: a helper model (gemini-2.5-flash) was asked to look at an image or video this turn."


def test_main_model_is_named_only_through_the_same_model_case(fp):
    fp.models["s1"] = "gemini-2.5-flash"
    (obs,) = aux(fp)
    assert obs.numbers == {"same_as_main": 1}
    assert line(obs) == ("vision helper: a separate request to gemini-2.5-flash, the same model as this session's, "
                         "was asked to look at an image or video this turn.")
    fp.models["s2"] = "deepseek-chat"
    (other,) = aux(fp, session_id="s2")
    assert "deepseek" not in line(other) and other.numbers == {}


def test_retries_and_several_images_are_one_observation_per_turn_and_model(fp):
    for _ in range(3):
        aux(fp)
    assert len(aux(fp)) == 1
    assert len(aux(fp, model="other-vlm")) == 2


def test_failed_attempt_has_its_own_wording_and_claims_no_outcome(fp):
    (obs,) = aux(fp, error="APIConnectionError: boom secret text", error_type="APIConnectionError")
    assert obs.numbers == {"error": "APIConnectionError"} and obs.severity == "digest"
    assert line(obs) == ("vision helper: an attempt to ask gemini-2.5-flash to look at an image or video failed "
                         "(APIConnectionError); this reports one attempt, not the outcome.")
    assert "secret" not in str(obs.numbers) and "boom" not in line(obs)
    assert "hermes config set auxiliary.vision.model" in fix(obs)
    assert len(aux(fp)) == 2          # the success of a later attempt is a separate observation


def test_error_without_a_type_still_counts_as_failed(fp):
    (obs,) = aux(fp, error="x")       # core.aux_call turns error into error_type "error"
    assert obs.numbers == {"error": "error"}


@pytest.mark.parametrize("task", ["compression", "title_generation", "approval", "", "vision_x", "Vision"])
def test_other_tasks_do_not_fire(fp, task):
    assert aux(fp, aux_task=task) == []


@pytest.mark.parametrize("over", [dict(model=""), dict(model=None), dict(model="///")])
def test_a_vision_call_without_a_usable_model_name_yields_nothing(fp, over):
    assert aux(fp, **over) == []


@pytest.mark.parametrize("over", [dict(session_id=""), dict(turn_id="")])
def test_a_call_outside_a_turn_is_dropped_before_the_rules(fp, over):
    # The pre-turn description of a user-attached image (cli_stream_mixin.py:633, run_inbound.py:1845) has no
    # turn id; core.aux_call drops it, so the aux rule never sees it.
    fp.aux_call(aux_task="vision", model="gemini-2.5-flash", **{"session_id": "s1", "turn_id": "t1", **over})
    assert not fp.buffer.get("s1", "t1").obs


# ── gateway-text-mode-150-word-image ─────────────────────────────────────────────────────────────
def test_one_described_image_as_the_gateway_writes_it(fp):
    (obs,) = turn(fp, GW_OK)
    assert obs.numbers == {"images": 1} and obs.severity == "card" and obs.subject == "gateway"
    assert line(obs) == ("gateway image routing: 1 image reached the model as a helper-model description, not as "
                         "pixels; the gateway asked the helper for 2-4 sentences (about 150 words) per image.")
    assert "red square" not in line(obs) and "red square" not in str(obs.numbers)


def test_two_images_are_counted_and_nothing_is_quoted(fp):
    (obs,) = turn(fp, GW_TWO)
    assert obs.numbers == {"images": 2}
    assert line(obs).startswith("gateway image routing: 2 images reached the model")


@pytest.mark.parametrize("wrapper", [GW_FAIL, GW_RAISE])
def test_failed_description_is_its_own_fact(fp, wrapper):
    (obs,) = turn(fp, wrapper)
    assert obs.numbers == {"failed": 1}
    assert line(obs) == ("gateway image routing: 1 image could not be described by the helper model; the model "
                         "got a note with the file path instead of the image.")
    assert "150" not in line(obs)


def test_described_and_failed_in_one_message_are_two_observations(fp):
    obs = turn(fp, GW_OK + "\n\n" + GW_FAIL)
    assert sorted(tuple(o.numbers) for o in obs) == [("failed",), ("images",)]


def test_wrapper_found_after_a_prepended_note_and_in_text_parts_of_a_list(fp):
    (obs,) = turn(fp, "[note from the gateway]\n\n" + GW_OK)
    assert obs.numbers == {"images": 1}
    parts = [{"type": "text", "text": GW_OK}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    (again,) = turn(fp, parts, session_id="s2")
    assert again.numbers == {"images": 1}


@pytest.mark.parametrize("message", [
    CLI_OK,                                                        # the CLI wrapper: a different entry's fact
    "hi there",
    "please explain the line [The user sent an image~ Here's what I can see: in this log",   # mid-line
    "[The user sent an image~ Here's what I can see:",             # no newline after the colon
    "[The user sent an image but it couldn't be analyzed.",
    "",
    "[Image attached at: /tmp/a.png]",
])
def test_text_that_is_not_the_wrapper_does_not_fire(fp, message):
    assert turn(fp, message) == []


@pytest.mark.parametrize("message", [None, 5, 1.5, {}, [], [None], [{"type": "image_url"}], [{"type": "text"}],
                                     [{"type": "text", "text": None}], [[GW_OK]], (GW_OK,), b"x"])
def test_degenerate_messages_yield_nothing(fp, message):
    assert turn(fp, message) == []


def test_a_text_part_of_another_type_is_ignored(fp):
    assert turn(fp, [{"type": "image_url", "text": GW_OK}]) == []


def test_count_is_capped(fp):
    (obs,) = turn(fp, (GW_OK + "\n\n") * 150)
    assert obs.numbers == {"images": 99}


def test_rules_never_raise_on_garbage(fp):
    garbage = [None, 0, "x", [], {}, object(), [object()], [{"type": object(), "text": object()}], "\x00" * 10]
    from fine_print.engine import AuxEvent, UserTurnEvent
    for g in garbage:
        assert isinstance(REGISTRY.run("route", "", UserTurnEvent(message=g)), list)
        if isinstance(g, str):
            assert isinstance(REGISTRY.run("aux", "vision", AuxEvent(model=g, error_type=g, main_model=g)), list)
    fp.user_turn(session_id="s", turn_id="t", user_message=object(), conversation_history=5, is_first_turn="x")
    fp.aux_call(aux_task="vision", model=object(), error_type=object(), session_id="s", turn_id="t",
                message_count="x", api_duration="y")
    assert REGISTRY.failed.get("aux-vision-substitution", 0) == 0
    assert REGISTRY.failed.get("gateway-text-mode-150-word-image", 0) == 0


# ── cli-text-mode-image-description ──────────────────────────────────────────────────────────────
# The three notes hermes_cli/cli_stream_mixin.py:657-677 writes, joined as :683-686 joins them. The first
# is what a live CLI turn with --image and an explicit auxiliary.vision.model delivered to pre_llm_call.
CLI_FAIL = ("[The user attached an image but it couldn't be analyzed. You can try examining it with "
            "vision_analyze using image_url: /tmp/a.png]\n\nhi")
CLI_RAISE = ("[The user attached an image but analysis failed (TimeoutError: slow). You can try examining it "
             "with vision_analyze using image_url: /tmp/a.png]\n\nhi")
CLI_TWO = (CLI_OK[:-len("\n\nhi")] + "\n\n" + CLI_OK)


def cli_turn(fp, message, **over):
    payload = dict(session_id="s1", turn_id="t1", user_message=message, conversation_history=[],
                   is_first_turn=True, model="claude-haiku-5-5", platform="cli", parent_session_id="")
    payload.update(over)
    fp.user_turn(**payload)
    return [o for o in fp.buffer.get(payload["session_id"], payload["turn_id"]).obs
            if o.rule_id == "cli-text-mode-image-description"]


def test_cli_rule_is_registered_with_the_catalogue_told():
    rule = REGISTRY.get("cli-text-mode-image-description")
    assert (rule.driver, rule.core_tells) == ("route", "both")


def test_one_described_image_as_the_cli_writes_it(fp):
    (obs,) = cli_turn(fp, CLI_OK)
    assert obs.numbers == {"images": 1} and obs.severity == "card" and obs.subject == "cli" and obs.cause == "described"
    assert line(obs) == ("CLI image routing: 1 image you attached reached the model as a helper-model description, "
                         "not as pixels.")
    assert "agent.image_input_mode native" in fix(obs)                    # the catalogue's fix
    assert "red square" not in str(obs.numbers) + obs.subject + obs.cause + line(obs)


def test_two_cli_images_are_counted(fp):
    (obs,) = cli_turn(fp, CLI_TWO)
    assert obs.numbers == {"images": 2}
    assert line(obs).startswith("CLI image routing: 2 images you attached reached the model")


@pytest.mark.parametrize("message", [CLI_FAIL, CLI_RAISE])
def test_a_cli_image_that_could_not_be_described_is_its_own_fact(fp, message):
    (obs,) = cli_turn(fp, message)
    assert obs.numbers == {"failed": 1} and obs.cause == "failed" and obs.severity == "card"
    assert line(obs) == ("CLI image routing: 1 image you attached could not be described by the helper model; the "
                         "model got a note with the file path instead of the image.")
    assert fix(obs).startswith("ask the agent to call vision_analyze on the path")
    assert "TimeoutError" not in line(obs) and "/tmp" not in line(obs) + str(obs.numbers)


def test_described_and_failed_cli_images_are_two_observations(fp):
    got = cli_turn(fp, CLI_OK[:-len("\n\nhi")] + "\n\n" + CLI_FAIL)
    assert sorted(o.cause for o in got) == ["described", "failed"]


@pytest.mark.parametrize("message", [
    "hi\n" + CLI_OK,                                          # the note is not where the CLI puts it
    "Why does Hermes write [The user attached an image. Here's what it contains: ...] into my message?",
    "[The user attached an image: shot.png]\n[Examine it with the vision_analyze tool using image_url: /tmp/s.png]",
    "[The user attached an image. Here's what it contains: all on one line]",
    GW_OK, "", None, 7, [], [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
])
def test_text_that_is_not_the_cli_note_does_not_fire(fp, message):
    assert cli_turn(fp, message) == []


def test_the_cli_note_in_the_first_text_part_of_a_list_counts(fp):
    (obs,) = cli_turn(fp, [{"type": "text", "text": CLI_OK}])
    assert obs.numbers == {"images": 1}


def test_the_gateway_rule_and_the_cli_rule_do_not_fire_on_each_other(fp):
    assert turn(fp, CLI_OK) == []
    assert cli_turn(fp, GW_OK, turn_id="t2") == []


# ── audit 3: a note counts only in its complete layout ───────────────────────────────────────────
@pytest.mark.parametrize("message", [
    "[The user attached an image. Here's what it contains:\nA red square.]\n\nwhat is this?",      # no hint line
    "[The user attached an image. Here's what it contains:\nwhy does Hermes write this into my message?",
    "[The user attached an image but it couldn't be analyzed.]\n\nhi",                              # not the sentence
    "[The user attached an image but analysis failed (x)]\n\nhi",
])
def test_the_first_line_of_a_cli_note_alone_raises_nothing(fp, message):
    assert cli_turn(fp, message) == []


def test_two_openings_and_one_hint_count_as_one_image(fp):
    message = "[The user attached an image. Here's what it contains:\n" + CLI_OK
    (obs,) = cli_turn(fp, message)
    assert obs.numbers == {"images": 1}


def test_a_complete_copy_of_the_note_cannot_be_told_from_a_routed_image(fp):
    """Documented in the README: the note is text inside the message and v0.23.0 gives no out-of-band
    flag; the helper call that would prove it runs in another process."""
    (obs,) = cli_turn(fp, CLI_OK)
    assert obs.numbers == {"images": 1}
