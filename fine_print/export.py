"""A self-contained HTML page for the history report (``report_html``).

The page shows the same aggregates as ``/limits report``: counts, shares, the report's own sentences.
It carries no record subject, no path and no message content. It has no script, loads nothing and
links to nothing: inline CSS and inline SVG only, so it renders offline and is safe to share.
Every string that comes from data goes through ``html.escape``.
"""

from __future__ import annotations

import html
from typing import Any

from . import catalog, rules as _rules  # noqa: F401 - importing the rules fills REGISTRY (status of each rule)
from .engine import REGISTRY
from .report import _lines, _share

_TOLD = {"nobody": "nobody", "model": "the model", "user": "the user", "both": "the model and the user"}

# Bar segments, in order: follow-up seen in the turn (solid), later in the session (hatched), none seen (outline).
_SEGMENTS = (("in the turn", "solid"), ("later in the session", "hatch"), ("none seen", "open"))
BAR_HEIGHT = 14

_CSS = """
:root{color-scheme:light dark;--bg:#fbfbf9;--fg:#1d1f22;--muted:#5b6068;--rule:#d4d6da;--panel:#f1f2f0;--accent:#1c5fb0}
@media (prefers-color-scheme:dark){:root{--bg:#16181b;--fg:#e3e5e8;--muted:#9aa0a8;--rule:#32363c;--panel:#1e2125;--accent:#7eaee8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main{max-width:920px;margin:0 auto;padding:28px 16px 40px}
h1{margin:0;font-size:1.6rem;letter-spacing:0}
h2{margin:32px 0 10px;font-size:1rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
.tagline{margin:2px 0 10px;font-size:1.1rem}
.scope,.note,.legend,.foot{color:var(--muted);font-size:.9rem}
.scope{margin:0}
.note{margin:12px 0 0;padding:8px 12px;border:1px solid var(--accent);border-left-width:4px;color:var(--fg)}
.figures{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:24px 0 0;padding:0;list-style:none}
.figures li{border:1px solid var(--rule);background:var(--panel);padding:12px 14px;border-radius:4px}
.figures b{display:block;font-size:1.9rem;line-height:1.15;font-variant-numeric:tabular-nums}
.figures span{display:block;font-size:.88rem;color:var(--muted)}
.tag{display:inline-block;margin-left:4px;padding:0 6px;border:1px solid var(--muted);border-radius:3px;font-size:.75rem;color:var(--muted);vertical-align:middle}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 6px}
.legend span{display:inline-flex;align-items:center;gap:6px}
ol.rules{margin:0;padding:0;list-style:none;border-top:1px solid var(--rule)}
ol.rules>li{display:grid;grid-template-columns:minmax(0,1fr);gap:4px 24px;padding:12px 0;border-bottom:1px solid var(--rule)}
.head{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.title{font-weight:600;overflow-wrap:anywhere}
.count{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.meta{display:flex;flex-wrap:wrap;gap:2px 12px;align-items:center;font-size:.88rem;color:var(--muted)}
.badge{padding:0 7px;border:1px solid var(--accent);border-radius:3px;font-size:.78rem;color:var(--accent)}
.badge.fixture{border-style:dashed;border-color:var(--muted);color:var(--muted)}
code{font:.82rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--muted);overflow-wrap:anywhere}
.split{font-size:.82rem;color:var(--muted);font-variant-numeric:tabular-nums}
svg.bar{display:block;width:100%;height:14px}
.track{fill:var(--panel)}
.solid{fill:var(--accent);stroke:var(--accent)}
.hatch{stroke:var(--accent)}
.open{fill:var(--bg);stroke:var(--accent)}
.hl{stroke:var(--accent);stroke-width:2}
.bar rect{stroke-width:1}
ul.sentences{margin:0;padding-left:20px}
ul.sentences li{margin:6px 0}
.none{margin:8px 0;padding:12px 14px;border:1px solid var(--rule);background:var(--panel)}
footer{margin-top:36px;padding-top:12px;border-top:1px solid var(--rule)}
.foot{margin:6px 0}
@media (min-width:720px){ol.rules>li{grid-template-columns:minmax(0,1fr) minmax(0,1fr);align-items:center}.figures b{font-size:2.1rem}}
@media (max-width:520px){.figures{grid-template-columns:minmax(0,1fr)}}
"""


# ── small helpers ──────────────────────────────────────────────────────────────────────────────────
def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun if count == 1 else noun + 's'}"


def _records(value: Any) -> list[dict]:
    return [r for r in value if isinstance(r, dict)] if isinstance(value, (list, tuple)) else []


def _pct(part: int, whole: int) -> str:
    return _share(part, whole) if whole else "–"


def _swatch(kind: str, uid: str) -> str:
    """A 22x12 legend swatch drawn like the bar segments."""
    if kind == "hatch":
        return (f'<svg width="22" height="12" viewBox="0 0 22 12" aria-hidden="true">{_pattern(uid)}'
                f'<rect class="hatch" x=".5" y=".5" width="21" height="11" fill="url(#{uid})"/></svg>')
    return (f'<svg width="22" height="12" viewBox="0 0 22 12" aria-hidden="true">'
            f'<rect class="{kind}" x=".5" y=".5" width="21" height="11"/></svg>')


def _pattern(uid: str) -> str:
    return (f'<defs><pattern id="{uid}" width="6" height="6" patternUnits="userSpaceOnUse" '
            f'patternTransform="rotate(45)"><line class="hl" x1="0" y1="0" x2="0" y2="6"/></pattern></defs>')


def segment_widths(hits: int, in_turn: int, later: int, row_width: float) -> list[float]:
    """Widths (percent of the track) of the three segments of one bar; they add up to ``row_width``."""
    if hits <= 0 or row_width <= 0:
        return [0.0, 0.0, 0.0]
    first = round(row_width * in_turn / hits, 2)
    second = round(row_width * later / hits, 2)
    third = max(0.0, round(row_width - first - second, 2))
    return [first, second, third]


def _bar(index: int, widths: list[float], parts: list[int]) -> str:
    uid = f"h{index}"
    x, rects = 0.0, []
    for (label, kind), width, count in zip(_SEGMENTS, widths, parts):
        if width > 0:
            fill = f' fill="url(#{uid})"' if kind == "hatch" else ""
            rects.append(f'<rect class="{kind}" x="{x:.2f}%" y=".5" width="{width:.2f}%" '
                         f'height="{BAR_HEIGHT - 1}"{fill}><title>{_e(label)}: {count:,}</title></rect>')
        x += width
    return (f'<svg class="bar" height="{BAR_HEIGHT}" preserveAspectRatio="none" role="img" '
            f'aria-label="{_e(" / ".join(f"{label} {n:,}" for (label, _k), n in zip(_SEGMENTS, parts)))}">'
            f'{_pattern(uid)}<rect class="track" x="0" y="0" width="100%" height="{BAR_HEIGHT}"/>'
            f'{"".join(rects)}</svg>')


def _row_data(rule_id: Any, records: Any, registry: Any) -> dict:
    recs = _records(records)
    hits = len(recs)
    in_turn = sum(1 for r in recs if r.get("recovered"))
    later = sum(1 for r in recs if r.get("later") and not r.get("recovered"))
    entry = None
    try:
        entry = catalog.get(str(rule_id)) or None
    except Exception:  # noqa: BLE001 - a broken catalogue must not break the page
        entry = None
    entry = entry if isinstance(entry, dict) else {}
    try:
        status = str(getattr(registry.get(rule_id), "status", "") or "")
    except Exception:  # noqa: BLE001
        status = ""
    return {"id": str(rule_id), "hits": hits, "in_turn": in_turn, "later": later, "none": hits - in_turn - later,
            "title": str(entry.get("title") or rule_id), "told": _TOLD.get(str(entry.get("told")), ""),
            "status": status}


def _badge(status: str) -> str:
    if status == "verified":
        return '<span class="badge">verified</span>'
    if status in ("fixture", "reference"):
        return '<span class="badge fixture">fixture-only</span>'
    return '<span class="badge fixture">status unknown</span>'


def _row(index: int, row: dict, top: int) -> str:
    width = round(100 * row["hits"] / top, 2) if top else 0.0
    parts = [row["in_turn"], row["later"], row["none"]]
    widths = segment_widths(row["hits"], row["in_turn"], row["later"], width)
    told = row["told"] or "not in the catalogue"
    return (
        f'<li data-rule="{_e(row["id"])}"><div><div class="head"><span class="title">{_e(row["title"])}</span>'
        f'<span class="count">{row["hits"]:,}</span></div>'
        f'<div class="meta">{_badge(row["status"])}<span>Hermes tells: {_e(told)}</span></div>'
        f'<div class="meta"><code>{_e(row["id"])}</code><span>/limits explain {_e(row["id"])}</span></div></div>'
        f'<div>{_bar(index, widths, parts)}'
        f'<div class="split">{row["in_turn"]:,} in the turn · {row["later"]:,} later · {row["none"]:,} none seen</div></div></li>'
    )


def _sentences(rules: dict) -> list[str]:
    try:
        return [line[2:] if line.startswith("• ") else line for line in _lines(rules)]
    except Exception:  # noqa: BLE001 - odd numbers in a record: leave the sentences out, keep the page
        return []


# ── the page ───────────────────────────────────────────────────────────────────────────────────────
def report_html(result: dict, days: int, *, registry: Any = REGISTRY, generated_at: str = "") -> str:
    """One complete HTML5 document for ``result`` (the dict ``report.scan`` returns)."""
    result = result if isinstance(result, dict) else {}
    raw_rules = result.get("rules")
    rules: dict = raw_rules if isinstance(raw_rules, dict) else {}
    days = _int(days)
    rows = [_row_data(rule_id, recs, registry) for rule_id, recs in rules.items()]
    rows.sort(key=lambda r: (r["status"] != "verified", -r["hits"], r["id"]))
    rows = [r for r in rows if r["hits"] > 0] if any(r["hits"] for r in rows) else []
    hits = sum(r["hits"] for r in rows)
    no_follow = sum(r["hits"] - r["in_turn"] for r in rows)
    card_turns, tool_turns = _int(result.get("card_turns")), _int(result.get("tool_turns"))
    top = max((r["hits"] for r in rows), default=0)
    sentences = _sentences(rules) if rows else []

    out = [
        '<!doctype html>', '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
        f'<title>Fine Print · the last {_plural(days, "day")}</title>', f'<style>{_CSS}</style>', '</head><body><main>',
        '<header><h1>Fine Print</h1>',
        '<p class="tagline">What Hermes changed before your model saw it</p>',
        f'<p class="scope">The last {_plural(days, "day")} of your own history: '
        f'{_plural(_int(result.get("tool_rows")), "tool result")}, {_plural(_int(result.get("turns")), "turn")}, '
        f'{_plural(_int(result.get("sessions")), "session")}. Read-only; aggregates only.</p>',
    ]
    if result.get("partial"):
        out.append('<p class="note"><strong>Partial scan.</strong> The scan stopped at its time budget, so the '
                   'figures below are lower bounds; use a smaller window for exact totals.</p>')
    out.append('</header>')
    out.append(
        '<ul class="figures">'
        f'<li><b>{hits:,}</b><span>limit {"hit" if hits == 1 else "hits"} in this window</span></li>'
        f'<li><b>{_e(_pct(no_follow, hits))}</b><span>of hits with no follow-up seen within the turn</span></li>'
        f'<li><b>{_e(_pct(card_turns, tool_turns))}</b><span>about {card_turns:,} of {tool_turns:,} turns with '
        'tool calls would have ended with a card<em class="tag">estimate</em></span></li></ul>')

    out.append('<h2>Limits by number of hits</h2>')
    if rows:
        out.append('<p class="legend"><span>Follow-up seen:</span>'
                   + "".join(f'<span>{_swatch(kind, f"hl{i}")} {_e(label)}</span>'
                             for i, (label, kind) in enumerate(_SEGMENTS))
                   + '</p>')
        out.append('<ol class="rules">' + "".join(_row(i, r, top) for i, r in enumerate(rows)) + '</ol>')
    else:
        out.append('<p class="none">No catalogued limit left a trace in this window.</p>')

    if sentences:
        out.append('<h2>In words</h2><ul class="sentences">' + "".join(f'<li>{_e(s)}</li>' for s in sentences) + '</ul>')

    out.append('<footer>')
    out.append('<p class="foot">The card figure is an estimate. It replays the same detectors and the same card '
               'policy as the live cards, with no rule muted; response and compaction events are not in stored '
               'rows, so they are not counted.</p>')
    out.append('<p class="foot">A follow-up by the model is evidence that it went looking for what was cut, '
               'not proof.</p>')
    if generated_at:
        out.append(f'<p class="foot">Generated {_e(generated_at)}.</p>')
    out.append('</footer></main></body></html>')
    return "\n".join(out) + "\n"
