"""The ledger: metadata only (sizes, counts, dimensions, a host or a file basename, hashes).

Never content, never full paths, never search patterns or URLs. One SQLite file in the plugin's own
data directory (``plugin_db("fine-print")``, WAL); rows of a deleted session are purged.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Optional

from .signals import safe_label, short_hash

_TEXT_OK = re.compile(r"[^A-Za-z0-9 _.:+>/-]")


def clean_numbers(numbers: Any) -> dict:
    """What may enter the ledger from an observation: ints, floats, bools and short plain tokens.
    Anything else is dropped; a string loses every character outside a small safe set."""
    out: dict = {}
    for key, value in (numbers.items() if isinstance(numbers, dict) else ()):
        key = str(key)[:32]
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = _TEXT_OK.sub("", value)[:40]
    return out


_PATHISH = re.compile(r"\A(?:[/~]|[A-Za-z]:[\\/]|file:|\\\\)")


def clean_subject(subject: Any) -> str:
    """A label is a host, a file basename, a tool or a model name: never a query string or a path.
    Anything that looks like a path (a local model file, say) is kept as its last segment only;
    a ``vendor/model`` identifier is left as it is."""
    text = str(subject or "")
    if _PATHISH.match(text) or "\\" in text:
        text = safe_label(text)
    text = text.split("?", 1)[0].split("#", 1)[0]
    return text.replace("\n", " ")[:80]

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    cause_hash TEXT NOT NULL DEFAULT '',
    numbers_json TEXT NOT NULL DEFAULT '{}',
    recovered INTEGER NOT NULL DEFAULT 0,
    surfaced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    note_chars INTEGER NOT NULL DEFAULT 0,
    notes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mutes (rule_id TEXT PRIMARY KEY, ts REAL NOT NULL);
"""


class Ledger:
    def __init__(self, connect: Callable[[], sqlite3.Connection], now: Callable[[], float] = time.time):
        self._connect, self._now = connect, now
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    # -- plumbing ---------------------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = self._connect()
            conn.executescript(SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def _write(self, sql: str, rows: Iterable[tuple]) -> None:
        with self._lock:
            db = self._db()
            db.executemany(sql, list(rows))
            db.commit()

    def _read(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db().execute(sql, params).fetchall()

    # -- writes -----------------------------------------------------------------------------------
    def touch_session(self, session_id: str, *, parent_id: str = "", model: str = "", platform: str = "") -> None:
        if not session_id or session_id in self._seen:
            return
        if len(self._seen) > 4096:           # only a cache in front of INSERT OR IGNORE
            self._seen.clear()
        self._write("INSERT OR IGNORE INTO sessions(id, first_seen, parent_id, model, platform) VALUES (?,?,?,?,?)",
                    [(session_id, self._now(), parent_id, clean_subject(model), platform[:40])])
        self._seen.add(session_id)           # only after the row exists

    def record(self, session_id: str, turn_id: str, observations: Iterable[Any]) -> None:
        """One transaction per turn; ``observations`` are engine ``Obs`` objects."""
        now = self._now()
        rows = [(now, session_id, turn_id, o.rule_id, clean_subject(o.subject),
                 short_hash(o.cause) if o.cause else "", json.dumps(clean_numbers(o.numbers), ensure_ascii=False),
                 int(bool(o.recovered)), int(o.surfaced or 0)) for o in observations]
        if rows:
            self._write("INSERT INTO events(ts, session_id, turn_id, rule_id, subject, cause_hash, numbers_json, "
                        "recovered, surfaced) VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def add_note_chars(self, session_id: str, chars: int, notes: int) -> None:
        if chars > 0 and session_id:
            self._write("UPDATE sessions SET note_chars = note_chars + ?, notes = notes + ? WHERE id = ?",
                        [(chars, notes, session_id)])

    def rename_session(self, old_id: str, new_id: str) -> None:
        """Compaction rotates the session id; keep one history."""
        if not old_id or not new_id or old_id == new_id:
            return
        with self._lock:
            db = self._db()
            db.execute("UPDATE events SET session_id = ? WHERE session_id = ?", (new_id, old_id))
            old = db.execute("SELECT first_seen, parent_id, model, platform, note_chars, notes FROM sessions "
                             "WHERE id = ?", (old_id,)).fetchone()
            if old is not None:              # move the row, or fold it into one that already has the new id
                db.execute("INSERT OR IGNORE INTO sessions(id, first_seen, parent_id, model, platform) "
                           "VALUES (?,?,?,?,?)", (new_id, *old[:4]))
                db.execute("UPDATE sessions SET note_chars = note_chars + ?, notes = notes + ?, "
                           "first_seen = MIN(first_seen, ?) WHERE id = ?", (old[4], old[5], old[0], new_id))
                db.execute("DELETE FROM sessions WHERE id = ?", (old_id,))
            db.execute("UPDATE sessions SET parent_id = ? WHERE parent_id = ?", (new_id, old_id))   # its subagents
            db.commit()
        self._seen.discard(old_id)           # the new id is marked only by touch_session, once its row exists

    def purge(self, session_ids: Iterable[str]) -> None:
        ids = [(s,) for s in session_ids if s]
        if ids:
            self._write("DELETE FROM events WHERE session_id = ?", ids)
            self._write("DELETE FROM sessions WHERE id = ?", ids)
            self._seen.difference_update(s for (s,) in ids)

    def mute(self, rule_id: str, on: bool = True) -> None:
        if on:
            self._write("INSERT OR REPLACE INTO mutes(rule_id, ts) VALUES (?,?)", [(rule_id, self._now())])
        else:
            self._write("DELETE FROM mutes WHERE rule_id = ?", [(rule_id,)])

    # -- reads ------------------------------------------------------------------------------------
    def mutes(self) -> set[str]:
        return {r[0] for r in self._read("SELECT rule_id FROM mutes")}

    def session_events(self, session_id: str, limit: int = 500) -> list[dict]:
        rows = self._read("SELECT ts, turn_id, rule_id, subject, numbers_json, recovered, surfaced FROM events "
                          "WHERE session_id = ? OR session_id IN (SELECT id FROM sessions WHERE parent_id = ?) "
                          "ORDER BY id DESC LIMIT ?", (session_id, session_id, limit))
        return [_event(r) for r in rows]

    def surfaced_counts(self, session_id: str) -> dict[str, int]:
        rows = self._read("SELECT rule_id, COUNT(*) FROM events WHERE session_id = ? AND surfaced = 1 "
                          "GROUP BY rule_id", (session_id,))      # 1 = a card bullet; 2 = grouped under one
        return {r[0]: r[1] for r in rows}

    def surfaced_causes(self, session_id: str) -> set[tuple]:
        rows = self._read("SELECT DISTINCT rule_id, cause_hash FROM events WHERE session_id = ? AND surfaced >= 1",
                          (session_id,))
        return {(r[0], r[1]) for r in rows}

    def session_counts(self, session_id: str) -> dict:
        """Exact totals for a session and its subagents (the event list itself is capped)."""
        rows = self._read("SELECT COUNT(*), COALESCE(SUM(surfaced >= 1), 0), COALESCE(SUM(recovered), 0) FROM events "
                          "WHERE session_id = ? OR session_id IN (SELECT id FROM sessions WHERE parent_id = ?)",
                          (session_id, session_id))
        total, shown, recovered = rows[0]
        return {"events": total, "shown": shown, "recovered": recovered}

    def session_info(self, session_id: str) -> dict:
        rows = self._read("SELECT first_seen, model, platform, note_chars, notes FROM sessions WHERE id = ?",
                          (session_id,))
        if not rows:
            return {}
        first, model, platform, chars, notes = rows[0]
        return {"first_seen": first, "model": model, "platform": platform, "note_chars": chars, "notes": notes}

    def totals(self) -> dict:
        rows = self._read("SELECT COUNT(*), COALESCE(SUM(surfaced >= 1),0), COALESCE(SUM(recovered),0), "
                          "COUNT(DISTINCT session_id) FROM events")
        n, surfaced, recovered, sessions = rows[0]
        return {"events": n, "surfaced": surfaced, "recovered": recovered, "sessions": sessions}


def _event(row: tuple) -> dict:
    ts, turn_id, rule_id, subject, numbers, recovered, surfaced = row
    try:
        nums = json.loads(numbers)
    except ValueError:
        nums = {}
    return {"ts": ts, "turn_id": turn_id, "rule_id": rule_id, "subject": subject, "numbers": nums,
            "recovered": bool(recovered), "surfaced": bool(surfaced)}
