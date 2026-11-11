"""The history driver: the same rules over the user's stored tool rows (``/limits report``).

It opens Hermes's session database read-only, keeps nothing and returns aggregates only: counts,
medians, shares. Rows are in their committed shape, so web rows are unwrapped first and spilled rows
are recognised by the block at their start. Live cards and this report share every rule function.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .cards import PER_SESSION_CARDS_PER_RULE
from .engine import Call, CommitRow, Obs, Registry, ToolEvent
from .signals import short_hash, strip_untrusted

MAX_SECONDS = 25.0


def _state_db() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "state.db"


def _tools_with_rules(registry: Registry) -> list[str]:
    return sorted({tool for rule in registry.all() if rule.driver == "tool" for tool in rule.tools})


def _calls_of(tool_calls: Optional[str]) -> list[Call]:
    if not tool_calls:
        return []
    try:
        items = json.loads(tool_calls)
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else ():
        fn = item.get("function") if isinstance(item, dict) else None
        if not isinstance(fn, dict):
            continue
        raw = fn.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            args = {}
        out.append(Call(str(fn.get("name") or ""), args if isinstance(args, dict) else {},
                        str(item.get("id") or item.get("call_id") or "")))
    return out


def scan(registry: Registry, db_path: Path, since_ts: float, *, deadline: Optional[float] = None,
         on_obs: Optional[Callable[[str, int, Obs], None]] = None) -> dict:
    """Replay stored rows since ``since_ts``. Returns ``{rules: {id: [records]}, sessions, turns,
    tool_rows, partial}`` where each record is numbers plus recovered flags; no content leaves here."""
    tools = _tools_with_rules(registry)
    marks = ",".join("?" * len(tools))
    # Fetch content only for rows a rule could match: a tool with a rule, a spill block at the start
    # (possibly inside the untrusted wrapper), or the lost-tail marker at the end.
    sql = (
        "SELECT session_id, id, role, tool_call_id, tool_calls, tool_name, "
        "CASE WHEN role = 'tool' AND (tool_name IN (%s) "
        "       OR substr(content, 1, 18) = '<persisted-output>' "
        "       OR (substr(content, 1, 23) = '<untrusted_tool_result ' AND instr(substr(content, 1, 700), '<persisted-output>') > 0) "
        "       OR substr(content, -9) = 'sandbox.]') THEN content END "
        "FROM messages WHERE timestamp >= ? AND role IN ('user', 'assistant', 'tool') ORDER BY session_id, id" % marks
    )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    out: dict[str, Any] = {"rules": {}, "sessions": 0, "turns": 0, "tool_rows": 0, "tool_turns": 0,
                           "card_turns": 0, "cards": 0, "partial": False}
    try:
        cur = conn.execute(sql, (*tools, since_ts))
        session: Optional[str] = None
        state = _Session(registry, out, on_obs)
        for sid, mid, role, tcid, tool_calls, tool_name, content in cur:
            if sid != session:
                state.close_session()
                session = sid
                state = _Session(registry, out, on_obs, sid)
                out["sessions"] += 1
                if deadline is not None and time.time() > deadline:
                    out["partial"] = True
                    break
            if role == "user":
                state.close_turn()
            elif role == "assistant":
                state.calls.extend(_calls_of(tool_calls))
            else:
                out["tool_rows"] += 1
                state.tool_row(mid, str(tcid or ""), str(tool_name or ""), content)
        state.close_session()
    finally:
        conn.close()
    return out


class _Session:
    def __init__(self, registry: Registry, out: dict, on_obs, sid: str = "") -> None:
        self.registry, self.out, self.on_obs, self.sid = registry, out, on_obs, sid
        self.calls: list[Call] = []            # every call of the session, in order
        self.turn_start = 0
        self.turn_obs: list[tuple[Obs, int]] = []
        self.pending: list[tuple[Obs, dict]] = []   # unrecovered in turn; may be recovered later
        self.seen: set = set()
        self.carded: dict[str, int] = {}
        self.had_tools = False

    def tool_row(self, mid: int, tcid: str, tool_name: str, content: Any) -> None:
        self.had_tools = True
        index = next((i for i in range(len(self.calls) - 1, self.turn_start - 1, -1)
                      if tcid and self.calls[i].tool_call_id == tcid), -1)
        if index < 0:
            self.calls.append(Call(tool_name, {}, tcid))
            index = len(self.calls) - 1
        if not isinstance(content, str):
            return
        call = self.calls[index]
        tool = call.tool or tool_name
        inner = strip_untrusted(content)
        found = self.registry.run("tool", tool, ToolEvent(tool, call.args, inner, self.sid, "", tcid, replay=True))
        found += self.registry.run("commit", tool, CommitRow(tool, call.args, content, None, tcid))
        for obs in found:
            obs.call_index = index
            self.turn_obs.append((obs, mid))

    def close_turn(self) -> None:
        self.out["turns"] += 1
        if self.had_tools:
            self.out["tool_turns"] += 1
        cards = 0
        for obs, mid in self.turn_obs:
            later = self.calls[obs.call_index + 1:]
            obs.recovered = any(r.satisfied(later) for r in obs.recover)
            record = {"numbers": obs.numbers, "recovered": obs.recovered, "later": False,
                      "severity": obs.severity, "subject": obs.subject}
            self.out["rules"].setdefault(obs.rule_id, []).append(record)
            if self.on_obs:
                self.on_obs(self.sid, mid, obs)
            if not obs.recovered:
                self.pending.append((obs, record))
                # the card policy, replayed: verified rule, card severity, a cause not shown before
                key = (obs.rule_id, short_hash(obs.cause) if obs.cause else "")
                rule = self.registry.get(obs.rule_id)
                if (rule is not None and rule.status == "verified" and obs.severity == "card"
                        and key not in self.seen and self.carded.get(obs.rule_id, 0) < PER_SESSION_CARDS_PER_RULE):
                    self.seen.add(key)
                    self.carded[obs.rule_id] = self.carded.get(obs.rule_id, 0) + 1
                    record["card"] = True
                    cards += 1
        if cards:
            self.out["card_turns"] += 1
            self.out["cards"] += cards
        self.turn_obs = []
        self.turn_start = len(self.calls)
        self.had_tools = False

    def close_session(self) -> None:
        if self.turn_obs or self.had_tools:
            self.close_turn()
        for obs, record in self.pending:
            later = self.calls[obs.call_index + 1:]
            record["later"] = any(r.satisfied(later) for r in obs.recover)
        self.pending = []


# ── text ───────────────────────────────────────────────────────────────────────────────────────────
def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "0%"


def _followed(records: list[dict]) -> tuple[int, int]:
    in_turn = sum(1 for r in records if r["recovered"])
    return in_turn, in_turn + sum(1 for r in records if r["later"])


def _lines(rules: dict) -> Iterator[str]:
    r = rules.get("web_extract.head_tail_truncation")
    if r:
        pct = [100 * x["numbers"]["shown"] / x["numbers"]["total"] for x in r]
        in_turn, ever = _followed(r)
        yield (f"• web_extract cut {len(r):,} pages to head + tail: the model saw a median "
               f"{round(statistics.median(pct))}% of a page ({_share(sum(1 for p in pct if p < 50), len(r))} of "
               f"pages under half). It went back for the middle in {_share(in_turn, len(r))} within the turn "
               f"({_share(ever, len(r))} by the end of the session).")
    r = rules.get("native-embed-downscale-1568")
    if r:
        ratios = [x["numbers"]["ratio"] for x in r]
        in_turn, ever = _followed(r)
        yield (f"• {len(r):,} images were downscaled before the model saw them: median "
               f"{statistics.median(ratios):g}x fewer pixels (max {max(ratios):g}x). It zoomed in with a "
               f"region crop in {_share(in_turn, len(r))} within the turn ({_share(ever, len(r))} by session end).")
    r = rules.get("web_search.rescue_nested_error")
    if r:
        empty = sum(1 for x in r if x["numbers"].get("results") == 0)
        yield (f"• web_search was answered by the keyless free tier instead of the configured backend "
               f"{len(r):,} times" + (f" ({empty} of them came back empty)" if empty else "") + ".")
    r = rules.get("search-files-silent-page-cap")
    if r:
        in_turn, ever = _followed(r)
        yield (f"• search_files returned a full page with no 'more' flag {len(r):,} times; the search was "
               f"continued in {_share(ever, len(r))}.")
    r = rules.get("tool_result.spill_trap")
    if r:
        in_turn, ever = _followed(r)
        lost = sum(1 for x in r if x["numbers"].get("lost"))
        yield (f"• {len(r):,} tool results were replaced by a short preview and a saved file; the model opened "
               f"the file in {_share(ever, len(r))}" + (f"; {lost} could not be saved at all" if lost else "") + ".")
    r = rules.get("web_extract.url_cap_5")
    if r:
        dropped = sum(x["numbers"]["dropped"] for x in r)
        yield f"• web_extract was asked for more than 5 URLs {len(r):,} times: {dropped:,} URLs were never fetched."
    known = {"web_extract.head_tail_truncation", "native-embed-downscale-1568", "web_search.rescue_nested_error",
             "search-files-silent-page-cap", "tool_result.spill_trap", "web_extract.url_cap_5"}
    for rule_id in sorted(set(rules) - known):
        yield f"• {rule_id}: {len(rules[rule_id]):,}"


def render_report(result: dict, days: int) -> str:
    rules = result["rules"]
    head = (f"Fine Print · the last {days} days of your own history: {result['tool_rows']:,} tool results, "
            f"{result['turns']:,} turns, {result['sessions']:,} sessions (read-only; aggregates only)")
    lines = [head]
    body = list(_lines(rules))
    if not body:
        lines.append("No catalogued limit left a trace in this window.")
    lines.extend(body)
    if body:
        lines.append("Hermes told you about none of these. With Fine Print running, "
                     f"{result['card_turns']:,} of {result['tool_turns']:,} turns with tool calls "
                     f"({_share(result['card_turns'], result['tool_turns'])}) would have ended with a card.")
    if result.get("partial"):
        lines.append("(Stopped early to stay within the time budget; use a smaller window for exact totals.)")
    return "\n".join(lines)


def history_report(registry: Registry, days: int, cfg: Any = None) -> str:
    try:
        db = _state_db()
        if not db.exists():
            return "Fine Print: no session database found for this profile yet."
        result = scan(registry, db, time.time() - days * 86400, deadline=time.time() + MAX_SECONDS)
    except Exception as exc:  # noqa: BLE001
        return f"Fine Print: the history report could not read the session database ({type(exc).__name__})."
    return render_report(result, days)
