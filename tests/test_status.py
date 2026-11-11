"""Which rules may raise cards. A promotion is a decision with evidence behind it (dev/RULES.md, "Promotion to
verified"), so the set is pinned here: a status flipped by accident fails this test."""

from fine_print.engine import REGISTRY

# replayed over real history with the hits reviewed
BY_HISTORY = {
    "web_extract.head_tail_truncation", "search-files-silent-page-cap", "web_search.rescue_nested_error",
    "native-embed-downscale-1568", "tool_result.spill_trap", "long-line-clamps",
    "terminal-capture-window-and-timeout", "execute-code-timeout-call-cap", "search-skips-hidden-ignored",
}
# no stored row, or no hit in the author's history: seen in a live session with the numbers checked independently
BY_LIVE_SESSION = {
    "compaction-stubs-old-tool-results-before-summary", "outbound-image-eviction", "cli-text-mode-image-description",
    "context-length-pin-ignored", "aux-vision-substitution", "web_extract.url_cap_5", "office-extraction-silent-loss",
}


def test_the_verified_set_is_exactly_the_rules_with_evidence():
    verified = {r.id for r in REGISTRY.all() if r.status == "verified"}
    assert verified == BY_HISTORY | BY_LIVE_SESSION
    assert not BY_HISTORY & BY_LIVE_SESSION


def test_every_other_rule_is_fixture_only():
    assert {r.status for r in REGISTRY.all()} == {"verified", "fixture"}
    assert len(REGISTRY.all()) == 35
