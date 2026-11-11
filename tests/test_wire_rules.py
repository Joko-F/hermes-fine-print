"""Wire-family rules: images in the conversation versus images in the request, and retried failures.

Payload shapes are the ones core builds; the eviction placeholder and the three image shapes were produced
by calling the real core functions in ``campaign/s5/wire/probe_eviction.py`` (``evict_stale_outbound_tool_images``
for the chat list, ``convert_messages_to_anthropic`` for the Anthropic list: 25 tool images, one over 2000 px
-> 8 replaced by ``[screenshot removed to save context]``, 17 kept; 21 images under 2000 px -> none). The real order
of a request (``probe_anthropic_order.py``: chat-shaped eviction, then the Anthropic conversion) leaves an evicted tool
result as a JSON STRING of the part list in ``tool_result.content``; a live session showed the same (no text block)."""

import json

import pytest

from fine_print.cards import describe
from fine_print.engine import REGISTRY

PLACEHOLDER = "[screenshot removed to save context]"      # agent/context_compressor.py:1451, anthropic_message_convert.py:683
URL = "data:image/png;base64," + "A" * 2000                # 2,022 chars each


def chat_img(url=URL):
    return {"type": "image_url", "image_url": {"url": url}}


def chat_tool(i, evicted=False):
    """A tool result as core holds it in history, or as the chat request carries it (agent/tool_executor.py:1143)."""
    part = {"type": "text", "text": PLACEHOLDER} if evicted else chat_img()
    return {"role": "tool", "tool_call_id": f"call_{i}", "content": [{"type": "text", "text": f"shot {i}"}, part]}


def anthropic_user(n_images, n_evicted, as_json=True):
    """One user message of tool_result blocks (agent/anthropic_message_convert.py:465-478). An evicted result is the
    JSON text of its part list (:455-462, as the probe printed it); ``as_json=False`` is the block list that
    ``_evict_old_screenshots`` alone writes (:683-686)."""
    blocks = []
    for i in range(n_images + n_evicted):
        gone = i < n_evicted
        inner = {"type": "text", "text": PLACEHOLDER} if gone else {
            "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 2000}}
        content = [{"type": "text", "text": f"shot {i}"}, inner]
        blocks.append({"type": "tool_result", "tool_use_id": f"call_{i}",
                       "content": json.dumps(content) if gone and as_json else content})
    return {"role": "user", "content": blocks}


def responses_request(n_images, n_evicted, detail=None):
    """function_call_output items with an ``output`` list (agent/codex_responses_adapter.py:628-645)."""
    items = []
    for i in range(n_images + n_evicted):
        gone = i < n_evicted
        part = {"type": "input_text", "text": PLACEHOLDER} if gone else {"type": "input_image", "image_url": URL}
        if detail and not gone:
            part["detail"] = detail
        items.append({"type": "function_call_output", "call_id": f"call_{i}", "output": [part]})
    return items


def history(n):
    return [{"role": "user", "content": "go"}] + [chat_tool(i) for i in range(n)]


def send(fp, hist, req, mode="chat_completions", model="claude-sonnet-5", turn="t1", **kw):
    fp.api_request(session_id="s1", turn_id=turn, conversation_history=hist, request_messages=req, model=model,
                   provider="anthropic", api_mode=mode, api_call_count=1, **kw)
    return [o for o in fp.buffer.get("s1", turn).obs if o.rule_id == "outbound-image-eviction"]


def line(obs):
    return describe(obs.rule_id, obs.numbers, obs.subject, lambda key, default: default)[0]


# ── outbound-image-eviction ───────────────────────────────────────────────────────────────────────
def test_registered_as_a_wire_rule_with_the_catalogue_tell():
    rule = REGISTRY.get("outbound-image-eviction")
    assert (rule.driver, rule.core_tells, rule.status) == ("wire", "model", "fixture")


def test_chat_request_with_core_placeholders_names_eviction(fp):
    req = [{"role": "user", "content": "go"}] + [chat_tool(i, evicted=i < 8) for i in range(30)]
    (obs,) = send(fp, history(30), req)
    assert obs.numbers == {"held": 30, "sent": 22, "missing": 8, "marked": 8, "sent_kb": 43, "total": 30, "shown": 22}
    assert obs.severity == "digest" and obs.cause == "evict:8" and obs.subject == "claude-sonnet-5"
    assert line(obs) == ("claude-sonnet-5: 8 of the 30 images in the conversation were not in this request; it carries "
                         f"22 (43 KB encoded). 8 tool results in the request show Hermes's '{PLACEHOLDER}' placeholder.")


def test_anthropic_shape_nested_in_tool_result_blocks(fp):
    req = [anthropic_user(17, 8)]
    (obs,) = send(fp, history(25), req, mode="anthropic_messages")
    assert (obs.numbers["held"], obs.numbers["sent"], obs.numbers["missing"], obs.numbers["marked"]) == (25, 17, 8, 8)
    assert obs.cause == "evict:8"
    (obs2,) = send(fp, history(25), [anthropic_user(17, 8, as_json=False)], mode="anthropic_messages", turn="t2")
    assert (obs2.numbers["sent"], obs2.numbers["marked"]) == (17, 8)


def test_responses_shape_function_call_output(fp):
    (obs,) = send(fp, history(12), responses_request(9, 3), mode="codex_responses")
    assert (obs.numbers["held"], obs.numbers["sent"], obs.numbers["marked"]) == (12, 9, 3)


def test_without_a_placeholder_only_the_numbers_are_stated(fp):
    """A text-only model, the image strip after a rejection, a provider conversion: all leave no marker, so the
    sentence must not name eviction."""
    req = [{"role": "user", "content": "go"}] + [{"role": "tool", "tool_call_id": f"c{i}", "content": "shot"} for i in range(5)]
    (obs,) = send(fp, history(5), req)
    assert obs.cause == "absent:5" and obs.numbers["marked"] == 0
    assert line(obs) == "claude-sonnet-5: 5 of the 5 images in the conversation were not in this request; it carries 0."
    assert "placeholder" not in line(obs) and "evict" not in line(obs).lower()


def test_marked_results_are_counted_apart_from_the_missing_images(fp):
    req = [{"role": "user", "content": "go"}] + [chat_tool(i, evicted=i < 2) for i in range(6)]
    req.pop()                                            # one image left out without a marker
    (obs,) = send(fp, history(6), req)
    assert (obs.numbers["missing"], obs.numbers["marked"]) == (3, 2)        # results with the marker, not images
    assert line(obs) == ("claude-sonnet-5: 3 of the 6 images in the conversation were not in this request; it carries "
                         f"3 (5 KB encoded). 2 tool results in the request show Hermes's '{PLACEHOLDER}' placeholder.")


def test_placeholders_already_in_the_history_are_not_counted_as_this_request_eviction(fp):
    """History retirement at compaction writes the same text into the stored conversation: it is in both lists."""
    hist = [chat_tool(0, evicted=True), chat_tool(1, evicted=True)] + [chat_tool(i) for i in range(2, 8)]
    req = [chat_tool(0, evicted=True), chat_tool(1, evicted=True)] + [chat_tool(i) for i in range(2, 7)]
    (obs,) = send(fp, hist, req)
    assert (obs.numbers["held"], obs.numbers["sent"], obs.numbers["marked"]) == (6, 5, 0)
    req2 = req[:2] + [chat_tool(2, evicted=True)] + req[3:]
    (obs2,) = send(fp, hist, req2, turn="t2")
    assert (obs2.numbers["missing"], obs2.numbers["marked"]) == (2, 1)


def test_a_tool_result_that_only_mentions_the_marker_is_not_eviction(fp):
    """Output text that quotes the placeholder inside a longer string is not core's part list."""
    req = [{"role": "user", "content": "go"},
           {"role": "tool", "tool_call_id": "c", "content": f"cat notes.md: {PLACEHOLDER} appears here"},
           {"role": "user", "content": [{"type": "tool_result", "content": f"see {PLACEHOLDER} and {PLACEHOLDER}"}]}]
    (obs,) = send(fp, history(3), req)
    assert obs.numbers["marked"] == 0 and obs.cause == "absent:3"


def test_same_state_on_the_next_request_is_one_observation_and_a_larger_gap_is_a_new_one(fp):
    req8 = [{"role": "user", "content": "go"}] + [chat_tool(i, evicted=i < 8) for i in range(30)]
    req16 = [{"role": "user", "content": "go"}] + [chat_tool(i, evicted=i < 16) for i in range(38)]
    assert len(send(fp, history(30), req8)) == 1
    assert len(send(fp, history(30), req8)) == 1              # the next request of the turn: same fact
    assert len(send(fp, history(38), req16)) == 2             # the gap grew: a new observation
    assert sorted(o.cause for o in send(fp, history(38), req16)) == ["evict:16", "evict:8"]


def test_nothing_when_every_image_was_sent_or_more_than_the_history_holds(fp):
    assert send(fp, history(30), [{"role": "user", "content": "go"}] + [chat_tool(i) for i in range(30)]) == []
    assert send(fp, history(3), [{"role": "user", "content": "go"}] + [chat_tool(i) for i in range(5)], turn="t2") == []


def test_text_only_conversation_returns_at_once(fp):
    hist = [{"role": "user", "content": "x" * 50_000}, {"role": "assistant", "content": "y"}] * 200
    assert send(fp, hist, hist) == []
    assert REGISTRY.failed == {}


@pytest.mark.parametrize("hist,req", [
    (None, None), ([], []), ("text", 5), ([None, 3, "x"], [None]), ([{"role": "tool", "content": {"_multimodal": True}}], []),
    ([{"role": "tool", "content": [None, 1, {"type": "image_url"}, {"type": "image_url", "image_url": 7}]}], [{"content": [[]]}]),
    ([{"content": [{"type": "tool_result", "content": [{"type": "tool_result", "content": [{"type": "image"}] * 3}]}]}], [{}]),
])
def test_garbage_payloads_never_raise(fp, hist, req):
    send(fp, hist, req)
    send(fp, hist, req, mode="codex_responses", turn="t2")
    assert REGISTRY.failed == {}


# ── openai-detail-never-set ───────────────────────────────────────────────────────────────────────
def detail_obs(fp, req, mode="codex_responses", hist=None, turn="t1"):
    send(fp, hist if hist is not None else history(4), req, mode=mode, turn=turn)
    return [o for o in fp.buffer.get("s1", turn).obs if o.rule_id == "openai-detail-never-set"]


def test_responses_images_without_detail_are_counted(fp):
    req = responses_request(3, 0) + responses_request(1, 0, detail="high")
    (obs,) = detail_obs(fp, req)
    assert obs.numbers == {"plain": 3, "images": 4} and obs.severity == "log" and obs.cause == "detail"
    assert line(obs) == ("claude-sonnet-5: 3 of the 4 images in this Responses request have no 'detail' field, "
                         "so the provider's default applies.")


def test_detail_rule_misses(fp):
    assert detail_obs(fp, responses_request(2, 0, detail="auto")) == []                      # every part has one
    assert detail_obs(fp, [anthropic_user(2, 0)], mode="anthropic_messages", turn="t2") == []  # other API mode
    assert detail_obs(fp, responses_request(2, 0), hist=[], turn="t3") == []                  # no image in history
    assert detail_obs(fp, [{"type": "message", "content": None}], turn="t4") == []
    assert REGISTRY.failed == {}


# ── transport-retries-hidden ──────────────────────────────────────────────────────────────────────
def fail(fp, turn="t1", **kw):
    args = dict(session_id="s1", turn_id=turn, model="gpt-5.5", provider="openai-codex", api_mode="codex_responses",
                status_code=503, retry_count=2, max_retries=3, retryable=True, reason="server_error",
                error={"type": "InternalServerError", "message": "secret body"}, api_call_count=1)
    args.update(kw)
    fp.api_error(**args)
    return [o for o in fp.buffer.get("s1", turn).obs if o.rule_id == "transport-retries-hidden"]


def test_a_failure_after_earlier_retries_is_one_digest_line(fp):
    (obs,) = fail(fp)
    assert obs.numbers == {"retries": 2, "max": 3, "status": 503, "kind": "InternalServerError"}
    assert obs.severity == "digest"
    assert line(obs) == "gpt-5.5: a provider request failed (HTTP 503) after 2 earlier retries (retry limit 3)."
    assert "secret" not in str(obs.numbers) + line(obs)
    assert len(fail(fp)) == 1 and len(fail(fp, retry_count=3)) == 1          # one per turn, the first one is kept


def test_connection_error_without_a_status_and_a_single_retry(fp):
    (obs,) = fail(fp, status_code=None, retry_count=1, max_retries=None, error={"type": "APIConnectionError"})
    assert line(obs) == "gpt-5.5: a provider request failed (APIConnectionError) after 1 earlier retry."


def test_first_failure_and_unretryable_failures_are_not_reported(fp):
    assert fail(fp, retry_count=0) == []                       # nothing was retried yet
    assert fail(fp, retryable=False, turn="t2") == []
    assert fail(fp, retryable=None, turn="t3") == []
    assert fail(fp, retry_count=None, turn="t4") == []


@pytest.mark.parametrize("kw", [
    {"status_code": "503"}, {"retry_count": True}, {"max_retries": "x"}, {"error": "boom"}, {"error": None},
    {"model": None, "reason": None}, {"retryable": "yes"}, {"api_call_count": None},
])
def test_api_error_garbage_never_raises(fp, kw):
    fail(fp, **kw)
    assert REGISTRY.failed == {}
