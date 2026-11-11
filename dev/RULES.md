# Adding a detector

A detector is one function plus its user-facing text, in one module under `fine_print/rules/`. The
engine, the card policy, the ledger, `/limits` and the history report pick it up from the registry.

```python
from ..cards import Cfg, text
from ..engine import Obs, Recover, ToolEvent, rule

@rule("long-line-clamps", driver="tool", tools={"read_file"}, core_tells="model", status="fixture")
def long_lines(ev: ToolEvent):
    obj = ev.json                       # parsed top-level JSON object, or None
    if obj is None or obj.get("truncated_lines") is not True:
        return
    yield Obs("long-line-clamps", numbers={"clamped": 3, "limit": 2000}, subject="notes.md",
              cause=str(ev.args.get("path", "")), severity="digest")

@text("long-line-clamps")
def _long_lines(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"read_file · {s}: {n['clamped']} lines were cut at {n['limit']:,} chars.",
            "ask the agent to read those lines with execute_code. ...")
```

## The contract

- **`rule(id, driver, tools, prefixes, core_tells, status)`**
  - `id` is the limit's id in `fine_print/catalog.json`.
  - `driver="tool"`: the function gets a `ToolEvent` (`tool`, `args`, `result`, `.text`, `.json`,
    `.replay`) built from the tool's own output before spill, wrapping and budgets. `driver="commit"`:
    it gets a `CommitRow` (`tool`, `args`, `content`, `raw_len`) with the row as committed to the
    conversation. Both run live and over stored history (`report.py`), where `ev.replay` is true and
    the content is the committed text; a rule that needs something only the live call has must return
    early when `ev.replay` is true.
  - `tools` are exact tool names; `prefixes` (e.g. `("mcp_",)`) match by prefix. Always gate by tool.
  - `core_tells`: who Hermes itself informs when the limit bites: `nobody`, `model`, `user`, `both`.
    A model note is delivered only when this is `nobody`.
  - `status`: `fixture` until the rule has been replayed on real history and sampled hits were
    reviewed; then `verified`. Only verified rules raise cards.
- **Structure only (P2).** Parse top-level JSON keys (`ev.json`) and anchored regexes for the exact
  v0.23.0 strings at the position core writes them (start or end of the result, or a known key).
  Never search content for a substring: tool output is full of files that mention the markers. An
  unknown format yields nothing; never guess.
- **`Obs`**
  - `numbers`: ints, floats and short strings only; `shown`/`total` when the limit is a cut (the
    policy and the report read them). Every number must come from the result or the arguments.
  - `subject`: a short label that is safe to store and show: a URL host, a file basename, a tool
    name. Never a full URL, a path, a command or a search pattern.
  - `cause`: the dedup key inside a session (stored hashed); the same cause is carded once.
  - `recover`: alternatives, any of which counts as a follow-up on the missing part later in the
    turn: `Recover.names(basename, tools=())` (a reading tool names the saved file; default tools are
    read_file, search_files, terminal, execute_code), `Recover.region(image)`,
    `Recover.search_more(pattern, path, seen, glob, target)`, `Recover.refetch(urls)`,
    `Recover.refetch_bigger(url, budget)`. Only successful calls count. A follow-up is evidence, not
    proof of a complete recovery, and user-facing text says "followed up". Empty when nothing the
    model does can fetch the missing part.
  - `severity`: `card` when an unrecovered hit likely changed the answer and there is something the
    user can do; `digest` for the rest (logged, counted, shown in `/limits`); `log` for bookkeeping.
  - `note`: one neutral, third-person sentence for the model, at most 200 characters, stating the
    fact and the existing lever. No imperatives addressed to the model.
- **Text (P5).** `(line, fix)`: the line says what happened with before and after numbers and claims
  only what was observed, with no word on follow-ups (the card adds "No follow-up was seen this turn."
  to a bullet that has `recover` predicates; `/limits` tags each hit with its state); the fix is a `hermes config
  set <key> <value>` command with a real key, a concrete workaround, or "Hardcoded in v0.23.0."
  Return `None` as the fix to use the fix of the limit's catalogue entry. Every number comes from the observation, `constants.py` or the live config (`cfg(key,
  default)`). Never advise switching a safety feature off, never tell the user to edit the config
  file by hand.
- **Budget.** A rule runs inside a hook on every matching tool call: no I/O beyond a bounded,
  read-only look at a file the tool itself just read; microseconds for the common "no signal" path.
- **Fixtures.** For every rule: at least one `hit` and one `miss` file in `tests/fixtures/` with the
  schema of the existing ones (`tool`, `args`, `result`, optional `committed`, `facts`, `expect`,
  `limit_id`, `source`), produced by calling the real core function on synthetic input.
  `tests/test_fixtures.py` runs them automatically; `tests/test_texts.py` checks note length and
  scanner phrases.
- **Register** the module in `fine_print/rules/__init__.py`.

## Promotion to verified

`python3 dev/replay.py --samples 10` prints per-rule hits, recovery and would-card counts plus sampled
hits as metadata. Look at the samples (are they what the rule claims?), then flip `status`.
