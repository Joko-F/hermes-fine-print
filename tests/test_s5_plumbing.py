"""S5 plumbing: the request, failed-request, user-turn and helper-call hooks become small events of
structure and sizes; observations that belong to no tool call join the turn once; the session
rules run once per session with the facts of its first request."""

import sqlite3

import pytest

from fine_print.core import FinePrint
from fine_print.engine import Obs, Registry, image_sizes, rule
from fine_print.ledger import Ledger

PNG = "data:image/png;base64," + "A" * 400


def chat_image(url=PNG):
    return {"type": "image_url", "image_url": {"url": url}}


def anthropic_image(data="B" * 300):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def make(host, seen):
    """A FinePrint with a private registry whose rules record the events they get."""
    reg = Registry()

    @rule("t-wire", driver="wire", core_tells="model", status="verified", registry=reg)
    def _wire(ev):
        seen.append(ev)
        if len(ev.history_images) > len(ev.request_images):
            yield Obs("t-wire", {"held": len(ev.history_images), "sent": len(ev.request_images)}, subject="images",
                      cause=f"{len(ev.history_images)}:{len(ev.request_images)}")

    @rule("t-error", driver="api_error", core_tells="user", registry=reg)
    def _error(ev):
        seen.append(ev)
        yield Obs("t-error", {"status": ev.status_code or 0}, cause="e", severity="digest")

    @rule("t-route", driver="route", core_tells="nobody", registry=reg)
    def _route(ev):
        seen.append(ev)
        return ()

    @rule("t-aux", driver="aux", tools={"vision"}, core_tells="both", registry=reg)
    def _aux(ev):
        seen.append(ev)
        yield Obs("t-aux", {}, subject=ev.model, cause=ev.model, severity="digest")

    @rule("t-session", driver="session", core_tells="nobody", registry=reg)
    def _session(ev):
        seen.append(ev)
        if ev.cfg("model.context_length", 0) and ev.cfg("model.context_length", 0) != ev.context_length:
            yield Obs("t-session", {"pinned": ev.cfg("model.context_length", 0), "used": ev.context_length},
                      cause="pin", severity="digest")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return FinePrint(host, Ledger(lambda: conn), registry=reg)


def request(history, sent, **extra):
    """The keyword arguments of ``pre_api_request`` (agent/turn_api_request.py:80-104)."""
    payload = dict(task_id="t", turn_id="T1", api_request_id="r1", session_id="S", user_message="hi",
                   conversation_history=history, platform="cli", model="main-model", provider="anthropic",
                   base_url="", api_mode="anthropic_messages", api_call_count=1, retry_count=0,
                   request_messages=sent, system_prompt="x" * 1200, message_count=len(sent), tool_count=31,
                   approx_input_tokens=900, request_char_count=3600, max_tokens=8192, started_at=0.0,
                   middleware_trace=[], request={"method": "POST", "body": {}})
    payload.update(extra)
    return payload


def response(**extra):
    payload = dict(session_id="S", turn_id="T1", model="main-model", provider="anthropic", response_model="main-model",
                   finish_reason="stop", api_mode="anthropic_messages", usage={}, context_length=200_000,
                   api_call_count=1)
    payload.update(extra)
    return payload


# ── image parts are counted in the three request shapes, by size, without touching content ──────
def test_image_sizes_reads_chat_anthropic_and_responses_shapes():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "look"}, chat_image()]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                      "content": [{"type": "text", "text": "shot"}, anthropic_image()]}]},
        {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/jpeg;base64," + "C" * 100}]},
        {"type": "function_call_output", "output": [{"type": "input_image", "image_url": "https://h.example/a.png"}]},
        {"role": "assistant", "content": "an image_url in plain text is not an image part"},
    ]
    assert image_sizes(messages) == [len(PNG), 300, 123, 0]         # a remote URL carries no payload


@pytest.mark.parametrize("junk", [None, "text", 7, [None, 3, "x"], [{"content": None}], [{"content": [None, 5, {}]}],
                                  [{"content": [{"type": "image_url"}]}], [{"content": [{"type": "image", "source": 4}]}]])
def test_image_sizes_never_raises_on_other_shapes(junk):
    assert all(isinstance(n, int) for n in image_sizes(junk))


def test_a_part_without_a_payload_counts_as_an_image_of_size_zero():
    assert image_sizes([{"content": [{"type": "image_url"}, {"type": "image", "source": {}}]}]) == [0, 0]


# ── wire: the rule sees both lists by reference; a repeated fact is one observation per turn ────
def test_a_wire_rule_gets_the_lists_core_passed_and_its_observation_joins_the_turn(host):
    seen = []
    fp = make(host, seen)
    history = [{"role": "user", "content": [chat_image(), chat_image()]}]
    sent = [{"role": "user", "content": [anthropic_image()]}]
    fp.api_request(**request(history, sent))
    ev = seen[0]
    assert ev.history is history and ev.request is sent
    assert (ev.history_images, ev.request_images) == ([len(PNG), len(PNG)], [300])
    assert (ev.model, ev.api_call_count, ev.tool_count, ev.system_prompt_chars, ev.max_tokens) == \
        ("main-model", 1, 31, 1200, 8192)
    fp.api_request(**request(history, sent, api_call_count=2))      # the same loss on the turn's next request
    assert [o.rule_id for o in fp.buffer.get("S", "T1").obs] == ["t-wire"]
    card = fp.turn_end(session_id="S", turn_id="T1")
    assert card and "t-wire" in card and len(host.posted) == 1


def test_a_growing_loss_is_a_new_observation(host):
    fp = make(host, [])
    fp.api_request(**request([{"content": [chat_image(), chat_image()]}], [{"content": [chat_image()]}]))
    fp.api_request(**request([{"content": [chat_image(), chat_image(), chat_image()]}], [{"content": [chat_image()]}]))
    assert [o.cause for o in fp.buffer.get("S", "T1").obs] == ["2:1", "3:1"]


def test_requests_without_a_turn_are_not_attributed(host):
    seen = []
    fp = make(host, seen)
    fp.api_request(**request([{"content": [chat_image()]}], [], turn_id=""))
    fp.api_error(session_id="S", turn_id="", status_code=500)
    fp.aux_call(aux_task="vision", session_id="S", turn_id="", model="helper")
    assert seen == []


@pytest.mark.parametrize("field", ["conversation_history", "request_messages", "system_prompt",
                                   "api_call_count", "max_tokens", "tool_count"])
@pytest.mark.parametrize("junk", [None, "x", 3.5, {"a": 1}, True])
def test_a_request_hook_with_unexpected_types_never_raises(host, field, junk):
    seen = []
    fp = make(host, seen)
    fp.api_request(**request([], [], **{field: junk}))
    assert isinstance(seen[0].history, list) and isinstance(seen[0].request, list)
    assert isinstance(seen[0].api_call_count, int) and not isinstance(seen[0].api_call_count, bool)


# ── failed requests, the user turn, helper calls ────────────────────────────────────────────────
def test_a_failed_request_carries_status_and_retry_counters_only(host):
    seen = []
    fp = make(host, seen)
    fp.api_error(session_id="S", turn_id="T1", model="main-model", provider="anthropic", api_mode="anthropic_messages",
                 status_code=529, retry_count=2, max_retries=5, retryable=True, reason="overloaded",
                 error={"type": "OverloadedError", "message": "secret text of the provider"},
                 request={"method": "POST", "body": {"messages": ["private"]}}, api_call_count=3)
    ev = seen[0]
    assert (ev.status_code, ev.retry_count, ev.max_retries, ev.retryable, ev.reason, ev.error_type) == \
        (529, 2, 5, True, "overloaded", "OverloadedError")
    assert "secret" not in repr(ev) and "private" not in repr(ev)
    assert [o.rule_id for o in fp.buffer.get("S", "T1").obs] == ["t-error"]


def test_the_user_turn_hook_returns_nothing_so_nothing_is_injected(host):
    seen = []
    fp = make(host, seen)
    out = fp.user_turn(session_id="S", task_id="t", turn_id="T1", user_message=[{"type": "text", "text": "hi"}],
                       conversation_history=[], is_first_turn=True, model="main-model", platform="telegram",
                       parent_session_id="", sender_id="u")
    assert out is None
    ev = seen[0]
    assert (ev.platform, ev.is_first_turn, ev.is_subagent, ev.model) == ("telegram", True, False, "main-model")


def test_helper_calls_are_gated_by_task_and_know_the_session_model(host):
    seen = []
    fp = make(host, seen)
    fp.api_request(**request([], []))
    seen.clear()
    fp.aux_call(aux_task="title", session_id="S", turn_id="T1", model="helper", provider="p")
    assert seen == []
    fp.aux_call(aux_task="vision", session_id="S", turn_id="T1", model="helper", provider="p", message_count=2,
                api_duration=1.5, error=None, error_type=None)
    ev = seen[0]
    assert (ev.aux_task, ev.model, ev.main_model, ev.error_type, ev.message_count) == \
        ("vision", "helper", "main-model", "", 2)
    fp.aux_call(aux_task="vision", session_id="S", turn_id="T1", model="helper", error="Timeout: x", error_type="Timeout")
    assert seen[1].error_type == "Timeout"
    assert len(fp.buffer.get("S", "T1").obs) == 1               # same helper, same turn: one observation


def test_a_helper_call_before_any_request_does_not_name_a_session_model(host):
    seen = []
    fp = make(host, seen)
    fp.aux_call(aux_task="vision", session_id="S", turn_id="T1", model="helper")
    assert seen[0].main_model == ""


# ── the session check: once, after the first response, with the first request's facts ───────────
def test_session_rules_run_once_with_the_facts_of_the_first_request(host):
    seen = []
    fp = make(host, seen)
    host.cfg["model.context_length"] = 1_000_000
    fp.user_turn(session_id="S", turn_id="T1", user_message="hi", conversation_history=[{"role": "user"}],
                 is_first_turn=False, model="main-model", platform="cli")
    fp.api_request(**request([], []))
    assert not [e for e in seen if type(e).__name__ == "SessionEvent"]      # not before a response
    fp.api_response(**response())
    (ev,) = [e for e in seen if type(e).__name__ == "SessionEvent"]
    assert (ev.model, ev.provider, ev.platform, ev.context_length, ev.max_tokens, ev.tool_count,
            ev.system_prompt_chars, ev.is_first_turn, ev.is_subagent) == \
        ("main-model", "anthropic", "cli", 200_000, 8192, 31, 1200, False, False)
    assert [o.rule_id for o in fp.buffer.get("S", "T1").obs if o.rule_id == "t-session"] == ["t-session"]
    fp.user_turn(session_id="S", turn_id="T2", user_message="more", conversation_history=[{}], is_first_turn=False)
    fp.api_request(**request([], [], turn_id="T2", tool_count=5))
    fp.api_response(**response(turn_id="T2", context_length=64_000))
    assert len([e for e in seen if type(e).__name__ == "SessionEvent"]) == 1
    assert "S" not in fp.session_facts                                      # nothing is kept after the check


def test_the_session_check_survives_a_response_without_a_request(host):
    seen = []
    fp = make(host, seen)
    fp.api_response(**response())
    (ev,) = [e for e in seen if type(e).__name__ == "SessionEvent"]
    assert (ev.model, ev.tool_count, ev.is_first_turn) == ("main-model", 0, None)


def test_a_subagent_session_is_marked(host):
    seen = []
    fp = make(host, seen)
    fp.subagent_start(parent_session_id="P", child_session_id="S")
    fp.api_response(**response())
    assert [e for e in seen if type(e).__name__ == "SessionEvent"][0].is_subagent is True


def test_deleting_a_session_drops_its_side_state(host):
    fp = make(host, [])
    fp.api_request(**request([], []))
    fp.api_response(**response())
    fp.api_request(**request([], [], session_id="S2"))
    fp.session_deleted(session_id="S", deleted_ids=["S2"])
    assert not ({"S", "S2"} & (set(fp.session_facts) | set(fp._session_checked) | set(fp.models)))


# ── audit 3: a background fork is not the session; a hook without a session is nobody's ──────────
def announce(fp, turn="T1", session="S", message="hi"):
    fp.user_turn(session_id=session, turn_id=turn, user_message=message, conversation_history=[],
                 is_first_turn=True, model="main-model", platform="cli")


def test_hooks_of_a_turn_the_session_did_not_announce_are_ignored(host):
    """A review fork runs under the parent's session id with its own turn id and no pre_llm_call
    (agent/background_review.py:1016-1019, agent/turn_context.py:858)."""
    seen = []
    fp = make(host, seen)
    host.cfg["model.context_length"] = 32_000
    announce(fp)
    seen.clear()
    images = [{"content": [chat_image(), chat_image()]}]
    fp.api_request(**request(images, [], turn_id="FORK", model="fork-model"))
    fp.api_error(session_id="S", turn_id="FORK", status_code=500, retry_count=1, retryable=True)
    fp.aux_call(aux_task="vision", session_id="S", turn_id="FORK", model="helper")
    fp.api_response(**response(turn_id="FORK", model="fork-model", context_length=128_000))
    assert seen == []
    assert fp.buffer.get("S", "FORK").obs == [] and "S" not in fp.models and "S" not in fp.context_length
    fp.api_request(**request([], []))                                   # the session's own turn
    fp.api_response(**response())
    (ev,) = [e for e in seen if type(e).__name__ == "SessionEvent"]     # the check was not used up by the fork
    assert (ev.model, ev.context_length) == ("main-model", 200_000)
    assert fp.models["S"] == "main-model" and fp.context_length["S"] == 200_000


def test_a_later_turn_of_the_session_replaces_the_announced_one(host):
    seen = []
    fp = make(host, seen)
    announce(fp, "T1")
    announce(fp, "T2")
    seen.clear()
    fp.api_request(**request([{"content": [chat_image()]}], [], turn_id="T1"))      # a straggler of the old turn
    assert seen == []
    fp.api_request(**request([{"content": [chat_image()]}], [], turn_id="T2"))
    assert len(seen) == 1


def test_the_announced_turn_follows_a_session_rotation_and_goes_with_a_deleted_session(host):
    fp = make(host, [])
    announce(fp, "T1", session="OLD")
    fp.compaction(session_id="NEW", old_session_id="OLD", kind="summarize", reason="r", before_tokens=10,
                  after_tokens=5, messages_removed=1)
    assert fp._foreign_turn("NEW", "FORK") and not fp._foreign_turn("NEW", "T1")
    fp.session_deleted(session_id="NEW")
    assert not fp._foreign_turn("NEW", "FORK")


@pytest.mark.parametrize("call", ["api_request", "api_error", "user_turn", "aux_call"])
def test_a_hook_without_a_session_id_is_attributed_to_nobody(host, call):
    seen = []
    fp = make(host, seen)
    {"api_request": lambda: fp.api_request(**request([{"content": [chat_image()]}], [], session_id="")),
     "api_error": lambda: fp.api_error(session_id="", turn_id="T1", status_code=500, retry_count=1, retryable=True),
     "user_turn": lambda: fp.user_turn(session_id="", turn_id="T1", user_message="hi", is_first_turn=True),
     "aux_call": lambda: fp.aux_call(aux_task="vision", session_id="", turn_id="T1", model="helper")}[call]()
    assert seen == [] and fp.buffer.get("", "T1").obs == []


def test_a_response_observation_follows_the_turn_across_a_rotation(host):
    """api_response used to append to the half of a turn that a rotation had already merged away."""
    fp = make(host, [])
    host.cfg["model.context_length"] = 32_000
    stale = fp.buffer.get("S", "T1")
    merged = fp.buffer.get("S2", "T1")
    stale.merged_into = merged
    fp.api_response(**response())
    assert [o.rule_id for o in merged.obs] == ["t-session"] and stale.obs == []


def test_a_vision_call_without_a_turn_is_dropped(host):
    """The CLI describes an attached image in the client process before the turn exists (seen live: two
    pids), so such a call is nobody's and leaves no state behind."""
    seen = []
    fp = make(host, seen)
    fp.aux_call(aux_task="vision", session_id="", turn_id="", model="helper")
    fp.aux_call(aux_task="vision", session_id="S", turn_id="", model="helper")
    assert seen == [] and fp.buffer.get("S", "").obs == []


def test_a_response_without_a_session_id_is_nobodys(host):
    seen = []
    fp = make(host, seen)
    host.cfg["model.context_length"] = 32_000
    fp.api_response(**response(session_id=""))
    assert seen == [] and fp.buffer.get("", "T1").obs == [] and "" not in fp.models
