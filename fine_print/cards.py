"""User-facing text: one line and one fix per rule, the card policy (P4) and the card renderer.

Every number in a line comes from the observation; every number in a fix comes from ``constants`` or
the live config (P5). Wording is neutral and third-person: these strings are shown to the user only,
but they still pass the install scanner's phrase list (tests/test_scanner_phrases.py).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import constants as C
from .signals import short_hash

Cfg = Callable[[str, Any], Any]
TextFn = Callable[[dict, str, Cfg], tuple[str, Optional[str]]]


def _pct(shown: int, total: int) -> str:
    return f"{max(1, round(100 * shown / total))}%" if total else "?"


def spill_threshold(context_tokens: Any) -> int:
    """Chars one tool result may have before core replaces it with a preview: min(100K, max(8K,
    15% of the window in chars)). With an unknown window, assume a small one (64K tokens)."""
    tokens = context_tokens if isinstance(context_tokens, int) and context_tokens > 0 else 64_000
    return min(C.SPILL_RESULT_CHARS_MAX, max(C.SPILL_RESULT_CHARS_FLOOR, int(0.15 * tokens * 4)))


def _g(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


# ── per-rule text ──────────────────────────────────────────────────────────────────────────────────
def _web_cut(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    line = (f"web_extract · {s}: the model saw {n['shown']:,} of {n['total']:,} chars "
            f"({_pct(n['shown'], n['total'])}); the middle was cut.")
    saved = n.get("saved", 1)
    if saved and n["total"] > C.WEB_STORED_COPY_CHARS:     # the saved copy itself stops there
        return line, (f"ask the agent to download the page with terminal (curl) and read the file in parts: "
                      f"the saved copy stops at {C.WEB_STORED_COPY_CHARS:,} chars.")
    ask = ("ask the agent to read the rest of that page from its saved copy" if saved
           else "ask the agent to fetch that page again with a larger char_limit")
    want = (n["total"] // 5_000 + 1) * 5_000
    # A limit is only worth suggesting when a page that long still fits under the spill threshold,
    # which scales with the model's context window (known once a response has been seen).
    if want * 1.1 <= spill_threshold(cfg("@context_length", 0)):
        fix = f"{ask}, or `hermes config set web.extract_char_limit {want}` (read on the next call)."
    else:
        fix = (f"{ask}. A web.extract_char_limit large enough for this page would push the result over the "
               f"spill threshold for this model, which leaves a {C.SPILL_PREVIEW_CHARS:,}-char preview instead.")
    return line, fix


def _url_cap(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"web_extract: {n['asked']} URLs requested, only the first {n['fetched']} fetched; "
            f"{n['dropped']} skipped with no error entry.",
            "ask the agent to fetch the remaining URLs in another call. Hardcoded in v0.23.0.")


def _rescue(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    empty = " It returned 0 results, which can be throttling rather than 'nothing found'." if n.get("results") == 0 else ""
    if n.get("kind") == "managed":
        return (f"web_search: the managed primary backend failed this call; results came from the managed "
                f"fallback ({n['served_by']}).{empty}",
                "nothing to change on your side; if it keeps happening, check the managed backend's status.")
    return (f"web_search: the configured backend '{n['failed']}' failed this call; results came from "
            f"{n['served_by']} on the keyless free tier.{empty}",
            f"check the {n['failed']} key or service status. `hermes config set web.keyless_rescue false` "
            "makes such failures visible as errors instead of a silent swap.")


def _search_page(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"search_files ({n.get('mode', 'content')}): a full page of {n['shown']} came back with no "
            "'more' flag; the real total is unknown.",
            f"ask the agent to continue from offset={n['offset'] + n['shown']}, or narrow the pattern. "
            "Hardcoded in v0.23.0.")


def _image(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    line = (f"vision_analyze · {s}: {n['orig_w']}x{n['orig_h']} was sent as {n['w']}x{n['h']} "
            f"({_g(n['ratio'])}x fewer pixels); small text and digits may be unreadable.")
    zoom = (f"ask the agent to zoom in with a vision_analyze region crop: a crop of at most "
            f"{C.EMBED_MAX_DIMENSION:,} px on its long edge is sent unscaled when it also fits the byte "
            "budget, and its scale note shows what arrived.")
    if n.get("byte_cap"):
        cur = cfg("vision.embed_target_bytes", C.EMBED_TARGET_BYTES_DEFAULT)
        cur = cur if isinstance(cur, int) and not isinstance(cur, bool) else C.EMBED_TARGET_BYTES_DEFAULT
        if cur < C.EMBED_TARGET_BYTES_MAX:
            want = 1_048_576 if cur < 1_048_576 else C.EMBED_TARGET_BYTES_MAX
            return line, (f"`hermes config set vision.embed_target_bytes {want}` raises the byte budget that "
                          f"forced the extra shrink (now {cur:,}; max {C.EMBED_TARGET_BYTES_MAX:,}). Or " + zoom)
    return line, zoom + f" The {C.EMBED_MAX_DIMENSION:,} px edge is hardcoded in v0.23.0."


def _spill(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    if n.get("lost"):
        return (f"{s}: a {n['total']:,}-char result was cut to {n['shown']:,} chars and the rest could not be "
                "saved anywhere.",
                "ask the agent to rerun it with narrower output (a filter, a range, or a redirect to a file).")
    return (f"{s}: a {n['total']:,}-char result was replaced by a {n['shown']:,}-char preview and a saved file.",
            "ask the agent to page through the saved output (read_file offset/limit) or process it with "
            "execute_code. The threshold scales with the model's context window.")


def _office(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    if n.get("kind") == "docx":
        parts = [f"{n[key]} {label}" for key, label in (
            ("tables", "table(s) flattened"), ("images", "image(s) dropped"), ("footnotes", "footnote(s) dropped"),
            ("endnotes", "endnote(s) dropped"), ("comments", "comment(s) dropped"),
            ("headers", "header/footer part(s) dropped")) if n.get(key)]
        return (f"read_file · {s}: read as paragraphs only; " + ", ".join(parts) + "; nothing in the result said so.",
                "ask the agent to read the document with execute_code for the missing parts. "
                "Hardcoded in v0.23.0.")
    parts = []
    if n.get("rows", 0) > C.XLSX_MAX_ROWS:
        parts.append(f"{n['rows_kept']:,} of {n['rows']:,} rows read")
    if n.get("cols", 0) > C.XLSX_MAX_COLS:
        parts.append(f"{C.XLSX_MAX_COLS} of {n['cols']} columns read")
    if n.get("hidden_sheets"):
        parts.append(f"{n['hidden_sheets']} hidden sheet(s) skipped")
    return (f"read_file · {s}: " + ", ".join(parts) + "; nothing in the result said so.",
            "ask the agent to load the workbook with execute_code for the full data. Hardcoded in v0.23.0.")


TEXT: dict[str, TextFn] = {
    "web_extract.head_tail_truncation": _web_cut,
    "web_extract.url_cap_5": _url_cap,
    "web_search.rescue_nested_error": _rescue,
    "search-files-silent-page-cap": _search_page,
    "native-embed-downscale-1568": _image,
    "tool_result.spill_trap": _spill,
    "office-extraction-silent-loss": _office,
}


def text(rule_id: str):
    """Register the user-facing text of a rule next to the rule itself:

        @text("some-limit-id")
        def _some_limit(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
            return ("what happened, with the numbers from n", "the fix")
    """
    def deco(fn: TextFn) -> TextFn:
        if rule_id in TEXT:
            raise ValueError(f"duplicate text for {rule_id!r}")
        TEXT[rule_id] = fn
        return fn
    return deco

_TOLD = {
    "model": "Hermes told the model, not you.",
    "user": "Hermes showed you a status line; the model was not told.",
    "both": "",
}


def told(core_tells: str, noted: bool) -> str:
    if core_tells == "nobody":
        return "Hermes told nobody; Fine Print noted it for the model." if noted else "Hermes told nobody."
    return _TOLD.get(core_tells, "")


def describe(rule_id: str, numbers: dict, subject: str, cfg: Cfg) -> tuple[str, str]:
    """(line, fix) for one observation; a generic line when a rule has no text yet or its numbers
    are incomplete (an old ledger row), never an exception. A rule whose text returns no fix of its
    own gets the fix of its catalogue entry, which is checked against the Hermes config."""
    fn = TEXT.get(rule_id)
    line = fix = None
    if fn is not None:
        try:
            line, fix = fn(numbers, subject, cfg)
        except (KeyError, TypeError, ValueError):
            line = fix = None
    if line is None:
        line = f"{rule_id}{' · ' + subject if subject else ''}"
    if fix is None:
        from . import catalog
        entry = catalog.get(rule_id)
        if entry and entry.get("fix"):          # the same text /limits explain shows, with its timing
            fix = entry["fix"] + (f" Applies: {entry['applies']}." if entry.get("applies") else "")
        else:
            fix = "see /limits explain " + rule_id
    return line, fix


# ── policy (P4) ────────────────────────────────────────────────────────────────────────────────────
PER_SESSION_CARDS_PER_RULE = 4


def badness(numbers: dict) -> float:
    try:
        if numbers.get("total"):
            return 1.0 - float(numbers.get("shown", 0)) / float(numbers["total"])
        if numbers.get("ratio"):
            return 1.0 - 1.0 / float(numbers["ratio"])
        if numbers.get("asked"):
            return float(numbers.get("dropped", 0)) / float(numbers["asked"])
        if numbers.get("rows"):
            return 1.0 - float(numbers.get("rows_kept", 0)) / float(numbers["rows"])
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return 0.5


def decide(observations: list, *, status_of: Callable[[str], str], mutes: set,
           surfaced_causes: set, surfaced_counts: dict) -> tuple[list, int]:
    """Pick what becomes a card bullet. A bullet needs all of: a verified rule, not muted, the limit
    bit, no follow-up by the model within the turn, card severity, and a cause not already shown in
    this session. Returns (groups, more): each group is ``(worst_obs, members)`` with the fresh
    card-worthy hits of one rule; ``more`` counts the open hits that stay in /limits only. Nothing
    is marked here: ``mark_surfaced`` runs after the card was really posted."""
    order: list[str] = []
    by_rule: dict[str, list] = {}
    more = 0
    for obs in observations:
        if obs.recovered or obs.rule_id in mutes or obs.severity == "log":
            continue
        if obs.rule_id not in by_rule:
            order.append(obs.rule_id)
            by_rule[obs.rule_id] = []
        by_rule[obs.rule_id].append(obs)
    groups = []
    for rule_id in order:
        items = by_rule[rule_id]
        fresh = [o for o in items if o.severity == "card"
                 and (rule_id, short_hash(o.cause) if o.cause else "") not in surfaced_causes]
        if (status_of(rule_id) != "verified" or not fresh
                or surfaced_counts.get(rule_id, 0) >= PER_SESSION_CARDS_PER_RULE):
            more += len(items)
            continue
        worst = max(fresh, key=lambda o: badness(o.numbers))
        groups.append((worst, fresh))
        more += len(items) - len(fresh)
    groups.sort(key=lambda g: -badness(g[0].numbers))
    for _worst, members in groups[C.CARD_MAX_BULLETS:]:
        more += len(members)
    return groups[: C.CARD_MAX_BULLETS], more


def mark_surfaced(groups: list) -> None:
    """After a successful post: the bullet's own observation counts as one card (1); the others it
    stands for are marked 2, so their causes are not shown again but do not use up the rule's quota."""
    for worst, members in groups:
        for obs in members:
            obs.surfaced = 2
        worst.surfaced = 1


# ── card ───────────────────────────────────────────────────────────────────────────────────────────
def render(groups: list, more: int, *, core_tells_of: Callable[[str], str], cfg: Cfg,
           child_hits: int = 0) -> Optional[str]:
    """The card text, or None when nothing deserves one (hits that only count as 'more' never post)."""
    if not groups:
        return None
    k = len(groups)
    lines = [f"Fine Print · {k} silent limit{'s' if k != 1 else ''} hit this turn"]
    for worst, members in groups:
        line, fix = describe(worst.rule_id, worst.numbers, worst.subject, cfg)
        if len(members) > 1:
            line += f" (+{len(members) - 1} more like it)"
        if worst.recover:            # a bullet exists only when none of its follow-ups was seen
            line += " No follow-up was seen this turn."
        who = told(core_tells_of(worst.rule_id), worst.noted)
        lines.append(f"• {line}{' ' + who if who else ''}")
        lines.append(f"  Fix: {fix}")
    tail = []
    if more:
        tail.append(f"+{more} more")
    if child_hits:
        tail.append(f"{child_hits} in subagents")
    tail.append("details: /limits")
    lines.append(" · ".join(tail))
    return "\n".join(lines)
