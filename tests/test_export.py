"""The HTML export of the history report: well-formed, offline, safe, and consistent with the text report."""

import html as htmllib
import re
from html.parser import HTMLParser
from pathlib import Path

from fine_print.engine import REGISTRY, Registry
from fine_print.export import report_html, segment_widths
from fine_print.report import _lines, render_report, scan

SUBJECTS = ("secret-host.example", "/home/alice/private.txt")


def rec(numbers, recovered=False, later=False, subject=SUBJECTS[0], card=False):
    return {"numbers": numbers, "recovered": recovered, "later": later, "severity": "digest",
            "subject": subject, "card": card}


def web(n, in_turn, later):
    return [rec({"shown": 1500, "total": 12_000 + i}, recovered=i < in_turn, later=in_turn <= i < in_turn + later)
            for i in range(n)]


def sample_result():
    return {
        "rules": {
            "web_extract.head_tail_truncation": web(40, 6, 4),
            "native-embed-downscale-1568": [rec({"ratio": 2.5, "orig_w": 3000, "orig_h": 2000, "w": 1568, "h": 1045},
                                                recovered=i < 3, subject=SUBJECTS[1]) for i in range(9)],
            "long-line-clamps": [rec({"marked": 3, "lines": 3, "cut": 3}, later=i < 2) for i in range(5)],
            "terminal-capture-window-and-timeout": [rec({"shown": 4000, "total": 90_000, "kind": "window"})
                                                    for _ in range(1_234)],
            "web_extract.url_cap_5": [rec({"dropped": 2}) for _ in range(3)],
        },
        "sessions": 12, "turns": 3_456, "tool_rows": 7_890, "tool_turns": 800, "card_turns": 120, "cards": 130,
        "partial": False,
    }


class Strict(HTMLParser):
    """Collects tags and fails on mismatched nesting of the elements the page uses."""
    VOID = {"meta", "br", "hr", "img", "input", "link"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.tags, self.text, self.errors = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> with {self.stack[-3:]}")
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def parse(page):
    p = Strict()
    p.feed(page)
    p.close()
    assert p.errors == [] and p.stack == []
    return p


def page_text(page):
    return " ".join(" ".join(parse(page).text).split())


def row_widths(page):
    """Per list item: the track-relative widths of the bar segments, as (row_width, [segment widths])."""
    out = []
    for item in re.findall(r"<li data-rule=.*?</li>", page, flags=re.S):
        widths = [float(w) for w in re.findall(r'<rect class="(?:solid|hatch|open)"[^>]*? width="([\d.]+)%"', item)]
        out.append(widths)
    return out


# ── content ────────────────────────────────────────────────────────────────────────────────────────
def test_page_parses_and_has_the_content():
    result = sample_result()
    page = report_html(result, 30, registry=REGISTRY, generated_at="2026-11-11 18:00 UTC")
    assert page.startswith("<!doctype html>")
    text = page_text(page)
    assert "Fine Print" in text and "What Hermes changed before your model saw it" in text
    assert "The last 30 days of your own history: 7,890 tool results, 3,456 turns, 12 sessions." in text
    assert "Read-only; aggregates only." in text
    for rule_id, records in result["rules"].items():
        assert htmllib.escape(rule_id) in page
        assert f"{len(records):,}" in text
    for title in ("web_extract keeps only the head and tail of long pages", "read_file cuts lines longer than 2,000 characters",
                  "Images loaded by tools are downscaled to 1568 px on the long edge"):
        assert title in text
    # headline figures: hits, share with no follow-up in the turn, the card estimate
    hits = 40 + 9 + 5 + 1_234 + 3
    assert f"{hits:,}" in text
    no_follow = hits - 6 - 3
    assert f"{round(100 * no_follow / hits)}%" in text
    assert "about 120 of 800 turns with tool calls would have ended with a card" in text and "estimate" in text
    assert "15%" in text
    # the report's own sentences, so page and text report agree
    sentences = [line[2:] for line in _lines(result["rules"])]
    assert len(sentences) >= 5
    for sentence in sentences:
        assert htmllib.escape(sentence) in page
    assert not any(s.startswith("• ") for s in sentences)
    assert all(f"• {s}" in render_report(result, 30) for s in sentences)
    assert "evidence" in text and "not proof" in text and "no rule muted" in text
    assert "Generated 2026-11-11 18:00 UTC." in text


def test_sorted_verified_first_then_by_hits():
    result = sample_result()
    result["rules"]["web_extract.pdf_from_url"] = [rec({}) for _ in range(5_000)]      # a fixture rule, most hits
    page = report_html(result, 30)
    order = re.findall(r'<li data-rule="([^"]+)"', page)
    assert order[0] == "terminal-capture-window-and-timeout"
    assert "fixture-only" in page and ">verified<" in page
    verified = [r for r in order if getattr(REGISTRY.get(r), "status", "") == "verified"]
    assert order[:len(verified)] == verified and len(verified) == 4
    assert order[len(verified)] == "web_extract.pdf_from_url"       # fixture rules follow, most hits first
    counts = [len(result["rules"][r]) for r in verified]
    assert counts == sorted(counts, reverse=True)


def test_row_shows_told_and_explain_hint():
    page = report_html(sample_result(), 30)
    text = page_text(page)
    assert "Hermes tells: the model" in text
    assert "/limits explain web_extract.url_cap_5" in text
    assert "Hermes tells: nobody" in text          # web_extract.url_cap_5 and search-files: nobody
    assert "<code>web_extract.url_cap_5</code>" in page


# ── safety ─────────────────────────────────────────────────────────────────────────────────────────
def test_offline_and_inert():
    page = report_html(sample_result(), 30, generated_at="2026-11-11 18:00 UTC")
    low = page.lower()
    for bad in ("<script", "http://", "https://", "javascript:", "<img", "<iframe", "<link", "@import", "<a ", "onload=",
                "onclick=", "<object", "<embed", "<form"):
        assert bad not in low, bad
    for tag, attrs in parse(page).tags:
        assert "src" not in attrs and "href" not in attrs, (tag, attrs)
        assert not any(name.startswith("on") for name in attrs), (tag, attrs)
    for target in re.findall(r"url\(([^)]*)\)", page):
        assert target.startswith("#") or target.startswith('"#'), target       # a fragment of this very page
    assert "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'" in page


def test_no_subject_or_content_in_the_page():
    result = sample_result()
    result["rules"]["web_extract.url_cap_5"].append(rec({"dropped": 1}, subject="/home/alice/private.txt"))
    result["rules"]["long-line-clamps"].append(rec({"marked": 1, "lines": 1}, subject="secret-host.example"))
    page = report_html(result, 30)
    for subject in SUBJECTS + ("alice", "private.txt"):
        assert subject not in page


def test_data_strings_are_escaped():
    evil = '<b>&"x'
    result = {"rules": {evil: [rec({}), rec({})]}, "sessions": 1, "turns": 2, "tool_rows": 2, "tool_turns": 2,
              "card_turns": 1, "cards": 1, "partial": False}
    page = report_html(result, 7, generated_at='<i>"now"</i>')
    assert evil not in page and "<b>&" not in page and "<i>" not in page
    assert htmllib.escape(evil) in page
    assert "&lt;i&gt;&quot;now&quot;&lt;/i&gt;" in page
    parse(page)


# ── degenerate input ───────────────────────────────────────────────────────────────────────────────
def test_empty_result():
    page = report_html({"rules": {}, "sessions": 0, "turns": 0, "tool_rows": 0, "tool_turns": 0, "card_turns": 0,
                        "cards": 0, "partial": False}, 30)
    text = page_text(page)
    assert "No catalogued limit left a trace in this window." in text
    assert "about 0 of 0 turns" in text
    assert "<ol" not in page and "Partial scan" not in page
    parse(page)


def test_partial_result_is_flagged():
    result = sample_result()
    result["partial"] = True
    assert "The scan stopped at its time budget" in page_text(report_html(result, 30))
    assert "Partial scan" not in report_html(sample_result(), 30)


def test_unknown_rule_and_registry():
    result = {"rules": {"made-up-rule": [rec({"anything": 1}, recovered=True), rec({})]}, "sessions": 1, "turns": 1,
              "tool_rows": 1, "tool_turns": 1, "card_turns": 0, "cards": 0, "partial": False}
    page = report_html(result, 1, registry=Registry())
    text = page_text(page)
    assert "made-up-rule" in text and "status unknown" in text and "not in the catalogue" in text
    assert "The last 1 day of" in text and "2 limit hits" in text
    assert "/limits explain made-up-rule" in text
    parse(page)


def test_missing_numbers_and_zero_denominators_never_raise():
    bad = {"rules": {"web_extract.head_tail_truncation": [{"recovered": False}, {"numbers": {}}, "junk", None],
                     "native-embed-downscale-1568": [], "long-line-clamps": "nope"},
           "sessions": None, "turns": "x", "partial": None}
    page = report_html(bad, -5)
    parse(page)
    assert report_html({}, 0).startswith("<!doctype html>")
    assert report_html(None, 3).startswith("<!doctype html>")                  # type: ignore[arg-type]
    assert "–" in page_text(report_html({"rules": {}}, 3))          # no hits: no share to show


# ── bars ───────────────────────────────────────────────────────────────────────────────────────────
def test_segment_widths_add_up():
    for hits, a, b in ((3, 1, 1), (7, 2, 3), (1234, 400, 500), (9, 0, 0), (9, 9, 0), (1, 0, 1)):
        for row in (100.0, 33.33, 0.81):
            widths = segment_widths(hits, a, b, row)
            assert abs(sum(widths) - row) < 0.011 and min(widths) >= 0
    assert segment_widths(0, 0, 0, 50.0) == [0.0, 0.0, 0.0]


def test_bars_in_the_page_add_up_and_the_largest_is_full():
    result = sample_result()
    page = report_html(result, 30)
    order = re.findall(r'<li data-rule="([^"]+)"', page)
    rows = row_widths(page)
    assert len(rows) == len(order) == 5
    top = max(len(r) for r in result["rules"].values())
    for rule_id, widths in zip(order, rows):
        want = 100 * len(result["rules"][rule_id]) / top
        assert abs(sum(widths) - want) < 0.05, (rule_id, widths, want)
    full = rows[order.index("terminal-capture-window-and-timeout")]
    assert abs(sum(full) - 100) < 0.05
    # no follow-up at all: one outline segment, the whole row
    lonely = rows[order.index("web_extract.url_cap_5")]
    assert len(lonely) == 1


# ── size and the real pipeline ─────────────────────────────────────────────────────────────────────
def test_size_bound_with_fifteen_rules():
    result = sample_result()
    ids = [r.id for r in REGISTRY.all()][:15]
    result["rules"] = {rid: web(50 + 7 * i, 5, 5) for i, rid in enumerate(ids)}
    page = report_html(result, 3650, generated_at="2026-11-11 18:00 UTC")
    assert len(ids) == 15 and len(page.encode("utf-8")) < 60_000
    parse(page)


def test_page_from_a_real_scan(tmp_path):
    from tests.test_audit2_report import make_db, web_turn
    db = tmp_path / "state.db"
    make_db(str(db), [web_turn("https://secret-host.example/a"), web_turn("https://secret-host.example/b"), []])
    result = scan(REGISTRY, Path(db), 0.0)
    page = report_html(result, 30)
    assert "web_extract.head_tail_truncation" in page and "secret-host.example" not in page
    assert page_text(page).count("2 limit hits") == 1
    parse(page)
