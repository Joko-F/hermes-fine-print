"""File, terminal and code-result limits, parsed only from core's structural output."""

from __future__ import annotations

import re
from typing import Iterator

from .. import constants as C
from ..cards import Cfg, text
from ..engine import CommitRow, Obs, Recover, ToolEvent, rule
from ..signals import basename, safe_label, strip_untrusted
from .webmcp import WEB_CACHE_PATH

# tools/file_operations.py:488-502, :813-819: the flag belongs to the read result;
# the marker belongs at the end of a numbered content line. The cap is configurable.
_CLAMPED_LINE = re.compile(r"^\d+\|(.*)\.\.\. \[truncated\]$", re.M)
# tools/tool_result_storage.py:26, :36-37, :43-46, :67-72: spill files live in the reserved
# $HERMES_HOME/cache/spillover directory under a safe name; a "~/" spelling names the same file.
_SPILL_PATH = re.compile(r"\A(?:/|~/|[A-Za-z]:/)(?:.*/)?cache/spillover/[A-Za-z0-9_.-]+\.txt\Z")
# tools/terminal_tool_result.py:190-193, tools/tool_output_truncate.py:15-16.
_CAPTURE_NOTE = re.compile(
    r"\AOutput exceeded the capture window \(head\+tail shown\)\. "
    r"Full output \(([\d,]+) chars\) saved to (.+) — search it with "
    r"search_files or page it with read_file instead of re-running the command\.\Z")
_CAPTURE_WINDOW = re.compile(
    r"\A[\s\S]*\n\n\.\.\. \[OUTPUT TRUNCATED - ([\d,]+) chars omitted out of "
    r"([\d,]+) total\] \.\.\.\n\n[\s\S]*\Z")
# tools/environments/base.py:569-576, :769-776; finalize strips outer whitespace.
_TERMINAL_TIMEOUT = re.compile(r"(?:\A|\n)\[Command timed out after (\d+)s\]\Z")
# tools/code_kernel.py:776-780; state_reset describes acquisition, not this timeout.
_KERNEL_TIMEOUT = re.compile(
    r"\ACell timed out after (\d+)s; the session kernel was killed and its state was lost\. "
    r"The next execute_code call starts a fresh kernel\.\Z")
# agent/tool_guardrails.py:51, :542-557. Applies to every tool, but the embedded
# tool name must equal the committed row's tool name.
_IDENTICAL_MIN_CHARS = 512
_IDENTICAL_STUB = re.compile(
    r"\A\[hermes note: this result is byte-identical to the ([A-Za-z0-9_.:-]+) "
    r"result earlier this turn(?: \(tool_call_id ([^\s)]+)\))?\. "
    r"Refer to that result; it has not changed\. Args: [^\r\n]*\](?=\n|\Z)")
# tools/file_operations_search.py:883-885, :912-922, :1299-1306. The
# count-only probe reads at most 50 output rows, so these are not global totals.
_HIDDEN_PROBE = re.compile(
    r"(?:\A| )0 matches in visible files, but (\d+) match\(es\) in (\d+) "
    r"hidden or gitignored file\(s\): [^\r\n]+ — these are excluded by default\.\Z")


def _spill_path(path: str) -> bool:
    return bool(_SPILL_PATH.fullmatch(path.replace("\\", "/")))


_NUMBERED_LINE = re.compile(r"^\d+\|", re.M)


def _clamped(obj: dict) -> tuple[int, int, int, int] | None:
    """(marked lines, longest kept prefix, lines returned, lines cut for sure) of a read whose lines
    were cut. ``truncated_lines`` says that at least one line was cut, not which: a file may contain
    the marker text itself. Every real cut keeps the same number of chars (the cap), so the lines
    whose kept prefix is the longest one are the ones counted as cut for sure."""
    if obj.get("truncated_lines") is not True or not isinstance(obj.get("content"), str):
        return None
    lengths = [len(m.group(1)) for m in _CLAMPED_LINE.finditer(obj["content"])]
    if not lengths or max(lengths) <= 0:
        return None
    # Redaction runs after clipping and can shorten a prefix. A literal marker
    # may also occur in a file, so count marked lines, not purported original lengths.
    longest = max(lengths)
    return len(lengths), longest, len(_NUMBERED_LINE.findall(obj["content"])), lengths.count(longest)


@rule("long-line-clamps", driver="tool", tools={"read_file"}, core_tells="model", status="verified")
def long_lines(ev: ToolEvent) -> Iterator[Obs]:
    path = str(ev.args.get("path") or "")
    if _spill_path(path):
        return  # The spill-read rule owns the same clipping fact in this directory.
    obj = ev.json or {}
    if obj.get("total_lines") == 1 and WEB_CACHE_PATH.search(path):
        return  # A one-line cached web page belongs to web_extract.pdf_from_url (rules/webmcp.py).
    clipped = _clamped(obj)
    if clipped is None:
        return
    count, prefix_max, lines, cut = clipped
    name = basename(path)
    # Core does not report how long a cut line was, so the size of the loss is unknown. A card only
    # when every line the model received was cut for sure (a one-line JSON, a minified file); one
    # long line in an otherwise readable file is logged.
    yield Obs("long-line-clamps",
              numbers={"marked": count, "prefix_max": prefix_max, "lines": lines, "cut": cut},
              subject=safe_label(path), cause=path,
              recover=(Recover.names(name, ("execute_code", "terminal")),) if name else (),
              severity="card" if cut >= lines else "digest")


@text("long-line-clamps")
def _long_lines(n: dict, s: str, cfg: Cfg) -> tuple[str, str | None]:
    marked, lines = n["marked"], n.get("lines", 0)
    cut = n.get("cut", marked)
    kept = f"{n['prefix_max']:,} chars"
    if lines and cut >= lines:
        which = "the only line returned was" if lines == 1 else f"all {lines:,} lines returned were"
        rest = "the rest of that line" if lines == 1 else "the rest of each line"
        return f"read_file · {s}: {which} cut at {kept}; {rest} was not returned.", None
    of = f" of {lines:,} lines returned" if lines else " line" + ("" if marked == 1 else "s")
    return (f"read_file · {s}: {marked:,}{of} end{'s' if marked == 1 else ''} with the '... [truncated]' marker "
            f"(longest kept prefix {kept}); the rest of a cut line was not returned.",
            None)


@rule("terminal-capture-window-and-timeout", driver="tool", tools={"terminal"},
      core_tells="model", status="verified")
def terminal_window(ev: ToolEvent) -> Iterator[Obs]:
    obj = ev.json or {}
    output = obj.get("output")
    if not isinstance(output, str):
        return
    timed_out = _TERMINAL_TIMEOUT.search(output) if obj.get("exit_code") == 124 else None
    if timed_out and int(timed_out.group(1)) > 0:
        yield Obs("terminal-capture-window-and-timeout",
                  numbers={"kind": "timeout", "timeout": int(timed_out.group(1))}, subject="terminal",
                  cause=ev.tool_call_id or "terminal-timeout", severity="digest")
    total = obj.get("output_total_chars")
    path, note = obj.get("full_output_path"), obj.get("truncation_note")
    if (type(total) is not int or total <= 0 or not isinstance(path, str) or not path
            or not isinstance(note, str) or not isinstance(output, str)):
        return
    meta, cut = _CAPTURE_NOTE.fullmatch(note), _CAPTURE_WINDOW.fullmatch(output)
    if not meta or not cut or meta.group(2) != path:
        return
    if int(meta.group(1).replace(",", "")) != total or int(cut.group(2).replace(",", "")) != total:
        return
    omitted = int(cut.group(1).replace(",", ""))
    if not 0 < omitted < total:
        return
    # Head and tail are usually what matters in a build log: a card only when under half was kept.
    yield Obs("terminal-capture-window-and-timeout", numbers={"shown": total - omitted, "total": total},
              subject="terminal", cause=path, recover=(Recover.names(basename(path)),),
              severity="card" if 2 * omitted > total else "digest")


@text("terminal-capture-window-and-timeout")
def _terminal_window(n: dict, s: str, cfg: Cfg) -> tuple[str, str | None]:
    if n.get("kind") == "timeout":
        return (f"terminal: the foreground command hit its {n['timeout']:,} s timeout.",
                "pass a larger timeout= to terminal (Hermes names 600 s as the foreground maximum) "
                "or run the job with background=true.")
    # tools/tool_output_limits.py:13, :32-52: default 50,000, cached for the life of the process.
    # tools/environments/base_output.py:29-35: the full stream is teed to a log up to 5,000,000 chars.
    log = ("Hermes saved the stream to a log file" if n["total"] <= C.TERMINAL_LOG_CAP_CHARS else
           f"the log file Hermes saved stops at {C.TERMINAL_LOG_CAP_CHARS:,} chars")
    return (f"terminal: the capture kept {n['shown']:,} of {n['total']:,} chars (head and tail); "
            f"the middle was left out of the result; {log}.",
            "ask the agent to search the saved log with search_files (its path is in the result), or raise "
            "tool_output.max_bytes (default 50000). Applies: gateway restart.")


@rule("spill-format-dead-end-v0.23", driver="tool", tools={"read_file"},
      core_tells="model", status="fixture")
def spill_read_clamped(ev: ToolEvent) -> Iterator[Obs]:
    """The failed recovery is visible on the read, not on the original pre-spill tool hook."""
    path = str(ev.args.get("path") or "")
    if not _spill_path(path):
        return
    clipped = _clamped(ev.json or {})
    if clipped is None:
        return
    count, prefix_max, _lines, _cut = clipped
    name = basename(path)
    yield Obs("spill-format-dead-end-v0.23", numbers={"marked": count, "prefix_max": prefix_max},
              subject=safe_label(path), cause=path, recover=(Recover.names(name, ("execute_code", "terminal")),),
              severity="card", dead_end=True)      # this read is no follow-up on the spilled result


@text("spill-format-dead-end-v0.23")
def _spill_read(n: dict, s: str, cfg: Cfg) -> tuple[str, str | None]:
    return (f"saved result · {s}: reading the saved file returned {n['marked']} line(s) cut with a "
            f"'... [truncated]' marker (longest kept prefix {n['prefix_max']:,} chars); "
            "read_file offset and limit select lines, so they cannot return the rest of a cut line.",
            None)


@rule("execute-code-timeout-call-cap", driver="tool", tools={"execute_code"},
      core_tells="model", status="fixture")
def kernel_timeout(ev: ToolEvent) -> Iterator[Obs]:
    obj = ev.json or {}
    if obj.get("status") != "timeout" or not isinstance(obj.get("error"), str):
        return
    match = _KERNEL_TIMEOUT.fullmatch(obj["error"])
    if not match or obj.get("exit_code") != -1:
        return
    timeout = int(match.group(1))
    if timeout <= 0:
        return
    yield Obs("execute-code-timeout-call-cap", numbers={"timeout": timeout},
              subject="execute_code", cause=ev.tool_call_id or "kernel-timeout", severity="digest")


@text("execute-code-timeout-call-cap")
def _kernel_timeout(n: dict, s: str, cfg: Cfg) -> tuple[str, str | None]:
    return (f"execute_code: a cell passed its {n['timeout']:,} s timeout; Hermes reported the kernel "
            "killed and its state lost.",
            None)


@rule("identical-result-stub", driver="commit", core_tells="model", status="fixture")
def identical_stub(row: CommitRow) -> Iterator[Obs]:
    content = strip_untrusted(row.content)
    if not isinstance(content, str) or not content.startswith("[hermes note: this result is byte-identical"):
        return
    # run_agent.py:1285-1297 can append loop advice and a streak notice after
    # this exact first-line block. The reference is still the committed payload.
    match = _IDENTICAL_STUB.match(content)
    if not match or match.group(1) != row.tool:
        return
    numbers = {"shown": len(match.group(0)), "minimum": _IDENTICAL_MIN_CHARS}   # the stub, without later advice
    if type(row.raw_len) is int and row.raw_len >= _IDENTICAL_MIN_CHARS and row.raw_len > len(content):
        numbers["total"] = row.raw_len
    # Informational: the earlier copy is in the same turn's context, so nothing is lost. Logged only.
    yield Obs("identical-result-stub", numbers=numbers, subject=row.tool,
              cause=f"{row.tool}:{match.group(2) or row.args}", severity="log")


@text("identical-result-stub")
def _identical_stub(n: dict, s: str, cfg: Cfg) -> tuple[str, str | None]:
    original = f"{n['total']:,}" if "total" in n else f"at least {n['minimum']:,}"
    return (f"{s}: a repeated {original}-char result was committed as a {n['shown']:,}-char reference to the earlier result.",
            None)


@rule("search-skips-hidden-ignored", driver="tool", tools={"search_files"},
      core_tells="model", status="fixture")
def hidden_probe(ev: ToolEvent) -> Iterator[Obs]:
    obj = ev.json or {}
    if (ev.args.get("target") or "content") != "content" or type(obj.get("total_count")) is not int:
        return
    warning = obj.get("warning")
    if obj["total_count"] != 0 or not isinstance(warning, str) or obj.get("error"):
        return
    match = _HIDDEN_PROBE.search(warning)        # core appends the hint to an earlier warning (:1306)
    if not match:
        return
    matches, files = int(match.group(1)), int(match.group(2))
    if not 0 < files <= matches:
        return
    path = str(ev.args.get("path") or "")
    yield Obs("search-skips-hidden-ignored", numbers={"shown": 0, "probe_matches": matches, "probe_files": files},
              subject=safe_label(path) or "search_files", cause=f"{path}:{ev.args.get('pattern', '')}", severity="digest")


@text("search-skips-hidden-ignored")
def _hidden_probe(n: dict, s: str, cfg: Cfg) -> tuple[str, str | None]:
    return (f"search_files · {s}: {n['shown']} visible matches; Hermes' follow-up probe found "
            f"{n['probe_matches']:,} matches in {n['probe_files']:,} hidden or gitignored file(s) "
            "(the probe reads at most 50 count rows, so these may be partial).",
            None)
