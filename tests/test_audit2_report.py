"""Audit 2 · findings 12 and 14: the history replay applies the live card policy and stays inside its
time budget. Each policy test feeds the same turns to the live path (FinePrint with the fake host) and
to ``scan`` over a synthetic SQLite database, and compares."""

import json
import sqlite3
import time

from fine_print import constants as C
from fine_print.cards import PER_SESSION_CARDS_PER_RULE
from fine_print.core import FinePrint
from fine_print.engine import Call, Obs, Recover, REGISTRY, Registry, Rule
from fine_print.ledger import Ledger
from fine_print.report import _Session, render_report, scan
from tests.conftest import FakeHost


# ── helpers ────────────────────────────────────────────────────────────────────────────────────────
def cut_page(url):
    footer = ("\n\n──────── [TRUNCATED] ────────\nShowing 1 chars (head) + 1 chars (tail) of 10,000 total clean "
              "characters.\nFull text saved to: /cache/web/page-0123456789.md\nTo read the omitted middle: read_file "
              'path="/cache/web/page-0123456789.md" offset=2 limit=200  (the file is the complete page; raise/lower '
              "offset to page through it).\n─────────────────────────────")
    return {"url": url, "content": "x" + footer, "error": None}


def web_turn(*urls):
    """One web_extract call over ``urls``; every page was cut."""
    return [("web_extract", {"urls": list(urls)}, json.dumps({"results": [cut_page(u) for u in urls]}))]


def make_db(path, turns, session="s1"):
    """The stored shape: user row, then per call an assistant row with its tool_calls and a tool row."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                 "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    rows, n = [], 0
    for t, calls in enumerate(turns):
        n += 1
        rows.append((n, session, "user", f"go {t}", None, None, None, 1000.0 + n))
        for c, (tool, args, result) in enumerate(calls):
            tcid = f"call{t}_{c}"
            n += 1
            rows.append((n, session, "assistant", "", None,
                         json.dumps([{"id": tcid, "function": {"name": tool, "arguments": json.dumps(args)}}]),
                         None, 1000.0 + n))
            n += 1
            rows.append((n, session, "tool", result, tcid, None, tool, 1000.0 + n))
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def replay(tmp_path, turns, registry=REGISTRY, **kw):
    db = tmp_path / "state.db"
    make_db(str(db), turns)
    return scan(registry, db, 0.0, **kw)


def live(turns, registry=REGISTRY):
    """Cards the live path posts, one entry (text or None) per turn."""
    host = FakeHost()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    fp = FinePrint(host, Ledger(lambda: conn), registry)
    cards = []
    for t, calls in enumerate(turns):
        for c, (tool, args, result) in enumerate(calls):
            fp.tool_result(tool_name=tool, args=args, result=result, session_id="s1", turn_id=f"t{t}",
                           tool_call_id=f"call{t}_{c}", status="ok")
        cards.append(fp.turn_end(session_id="s1", turn_id=f"t{t}", platform="cli"))
    return cards


def card_flags(out, rule_id):
    return [bool(r["card"]) for r in out["rules"][rule_id]]


def bullets(card):
    return sum(1 for line in card.splitlines() if line.startswith("• "))


def toy_registry(n):
    """``n`` verified tool rules, each cutting one tool's result: the numbers come from the row."""
    reg = Registry()
    for i in range(n):
        rid = f"toy.rule{i}"

        def fn(ev, rid=rid):
            return [Obs(rid, {"shown": 1, "total": 10 + len(ev.result)}, subject="toy", cause=ev.result)]
        reg.add(Rule(rid, "tool", frozenset({f"toy{i}"}), (), "nobody", "verified", fn))
    return reg


# ── finding 12: the live card policy ───────────────────────────────────────────────────────────────
def test_five_cut_pages_in_one_turn_make_one_bullet_and_a_second_turn_a_second_card(tmp_path):
    urls = [f"https://example.com/{i}" for i in range(5)]
    turns = [web_turn(*urls), web_turn("https://example.com/5")]
    cards = live(turns)
    assert cards[0] and cards[1]                         # both turns end with a card live
    assert bullets(cards[0]) == 1 and "+4 more like it" in cards[0]
    out = replay(tmp_path, turns)
    assert out["card_turns"] == 2 and out["cards"] == 2
    assert [r["card"] for r in out["rules"]["web_extract.head_tail_truncation"]] == [True, False, False, False, False, True]
    assert out["tool_turns"] == 2 and out["partial"] is False


def test_a_rule_stops_carding_at_the_same_turn_as_live(tmp_path):
    turns = [web_turn(f"https://example.com/{i}") for i in range(PER_SESSION_CARDS_PER_RULE + 2)]
    cards = live(turns)
    assert [bool(c) for c in cards] == [True] * PER_SESSION_CARDS_PER_RULE + [False, False]
    out = replay(tmp_path, turns)
    assert card_flags(out, "web_extract.head_tail_truncation") == [bool(c) for c in cards]
    assert out["card_turns"] == PER_SESSION_CARDS_PER_RULE and out["cards"] == PER_SESSION_CARDS_PER_RULE


def test_the_same_cause_is_not_carded_twice_in_replay_either(tmp_path):
    turns = [web_turn("https://example.com/a"), web_turn("https://example.com/a")]
    assert [bool(c) for c in live(turns)] == [True, False]
    out = replay(tmp_path, turns)
    assert out["card_turns"] == 1 and out["cards"] == 1


def test_four_card_worthy_rules_in_a_turn_give_at_most_the_bullet_cap(tmp_path):
    reg = toy_registry(4)
    turns = [[(f"toy{i}", {}, "r" * (i + 1)) for i in range(4)]]
    card = live(turns, reg)[0]
    assert bullets(card) == C.CARD_MAX_BULLETS
    out = replay(tmp_path, turns, reg)
    assert out["cards"] == C.CARD_MAX_BULLETS == 3 and out["card_turns"] == 1
    flagged = sum(1 for records in out["rules"].values() for r in records if r["card"])
    assert flagged == C.CARD_MAX_BULLETS and sum(len(v) for v in out["rules"].values()) == 4


def test_a_bullet_cut_by_the_cap_stays_available_for_a_later_turn(tmp_path):
    reg = toy_registry(4)
    first = [(f"toy{i}", {}, "r" * (i + 1)) for i in range(4)]
    turns = [first, first]                               # same causes again: the fourth was never shown
    live_cards = live(turns, reg)
    out = replay(tmp_path, turns, reg)
    assert [bool(c) for c in live_cards] == [True, True]
    assert out["card_turns"] == 2 and out["cards"] == bullets(live_cards[0]) + bullets(live_cards[1])


def test_report_sentence_reads_as_an_estimate(tmp_path):
    out = replay(tmp_path, [web_turn("https://example.com/a")])
    text = render_report(out, 30)
    sentence = text.splitlines()[-1]
    assert "estimate" in sentence and "no mutes" in sentence and "about 1 of 1" in sentence
    assert len(sentence) < 300


def test_callers_keep_their_shape(tmp_path):
    seen = []
    out = replay(tmp_path, [web_turn("https://example.com/a")], on_obs=lambda sid, mid, obs: seen.append((sid, obs.rule_id)))
    assert seen == [("s1", "web_extract.head_tail_truncation")]
    for key in ("rules", "sessions", "turns", "tool_rows", "tool_turns", "card_turns", "cards", "partial"):
        assert key in out
    record = out["rules"]["web_extract.head_tail_truncation"][0]
    assert set(record) >= {"numbers", "recovered", "later", "severity", "subject", "card"}


def test_a_later_follow_up_still_marks_later(tmp_path):
    saved = "/cache/web/page-0123456789.md"
    follow = ("read_file", {"path": saved}, json.dumps({"content": "the middle", "total_lines": 3}))
    out = replay(tmp_path, [web_turn("https://example.com/a"), [follow]])
    record = out["rules"]["web_extract.head_tail_truncation"][0]
    assert record["recovered"] is False and record["later"] is True and record["card"] is True


# ── finding 14: the deadline bounds Python work ────────────────────────────────────────────────────
def test_a_deadline_in_the_past_returns_partial_quickly(tmp_path):
    turns = [web_turn(f"https://example.com/{i}") for i in range(300)]
    t0 = time.perf_counter()
    out = replay(tmp_path, turns, deadline=time.time() - 1)
    assert out["partial"] is True and time.perf_counter() - t0 < 2.0
    flagged = sum(1 for records in out["rules"].values() for r in records if r["card"])
    assert flagged == out["cards"]                       # what was gathered is consistent


def test_session_finalisation_stops_at_the_deadline():
    calls = [Call("read_file", {"path": f"/other/{i}"}) for i in range(2000)]

    def pending(deadline):
        out = {"rules": {}, "turns": 0, "tool_turns": 0, "card_turns": 0, "cards": 0, "partial": False}
        s = _Session(REGISTRY, out, None, "s", deadline)
        s.calls = list(calls)
        for i in range(2000):
            o = Obs("tool_result.spill_trap", {"shown": 1, "total": 10}, cause=str(i),
                    recover=(Recover.names("never-matches"),), call_index=i)
            s.pending.append((o, {"later": False}))
        return s, out

    s, out = pending(time.time() - 1)
    t0 = time.perf_counter()
    s.close_session()
    assert out["partial"] is True and time.perf_counter() - t0 < 0.5
    s, out = pending(time.time() + 60)                   # a generous budget changes nothing
    s.close_session()
    assert out["partial"] is False


def test_follow_up_lookup_is_not_quadratic():
    calls = [Call("read_file", {"path": f"/other/{i}"}) for i in range(2000)] + [Call("read_file", {"path": "/x/found.md"})]
    out = {"rules": {}, "turns": 0, "tool_turns": 0, "card_turns": 0, "cards": 0, "partial": False}
    s = _Session(REGISTRY, out, None, "s")
    s.calls = calls
    records = [{"later": False} for _ in range(2000)]
    for i, record in enumerate(records):
        rec = Recover.names("found.md") if i % 2 else Recover.names("never-matches")
        s.pending.append((Obs("tool_result.spill_trap", {}, cause=str(i), recover=(rec,), call_index=i), record))
    t0 = time.perf_counter()
    s.close_session()
    assert time.perf_counter() - t0 < 1.5
    assert [r["later"] for r in records[:4]] == [False, True, False, True]
