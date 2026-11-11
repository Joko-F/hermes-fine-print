"""Structural web, MCP and delegation limits visible through public tool hooks."""

from __future__ import annotations

import re
from typing import Iterator

from .. import constants as C
from ..cards import Cfg, text
from ..engine import Obs, Recover, ToolEvent, rule
from ..signals import basename, json_object, parse_web_footer, safe_label, url_host

# plugins/web/_common.py:19; tavily/provider.py:104, parallel/provider.py:62, perplexity/provider.py:66,229,
# brave_free/provider.py:33, keenable/provider.py:53 cap at 20; exa, firecrawl, searxng and xai pass the limit through.
_SEARCH_CAP = 20
_STORED_PAGE_CAP = C.WEB_STORED_COPY_CHARS

# tools/web_tools_truncate.py:67-69; tools/file_operations.py:488-502.
# The directory is cache/web, or web_cache on a home that still has the legacy folder (:67).
WEB_CACHE_PATH = re.compile(r'(?:\A|[/\\])(?:cache[/\\]web|web_cache)[/\\][a-zA-Z0-9_.-]+-[0-9a-f]{10}\.md\Z')
_PAST_EOF = re.compile(r'Note: offset (\d+) is beyond the end of the file \(1 lines total\)\. '
                       r'Retry with offset <= 1\.\Z')
_CLAMPED_WEB_LINE = re.compile(r'1\|([^\n]*)\.\.\. \[truncated\]\Z')


@rule('web_extract.pdf_from_url', driver='tool', tools={'read_file'}, core_tells='model', status='fixture')
def web_copy_recovery(ev: ToolEvent) -> Iterator[Obs]:
    """Only the observed recovery failures, never an inference from a PDF URL or missing text."""
    path = ev.args.get('path')
    if not isinstance(path, str) or not WEB_CACHE_PATH.search(path):
        return
    obj = ev.json
    if obj is None or type(obj.get('total_lines')) is not int or obj['total_lines'] != 1:
        return
    offset = ev.args.get('offset', 1)
    if not isinstance(offset, int) or isinstance(offset, bool):
        return
    content = obj.get('content')
    phase, shown = '', 0
    hint = obj.get('hint')
    if content == '' and offset > 1 and isinstance(hint, str):
        match = _PAST_EOF.fullmatch(hint)
        if match and int(match[1]) == offset:
            phase = 'past_eof'
    elif obj.get('truncated_lines') is True and offset == 1 and isinstance(content, str):
        match = _CLAMPED_WEB_LINE.fullmatch(content)
        if match:
            phase, shown = 'line_clamp', len(match[1])
    if not phase:
        return
    yield Obs('web_extract.pdf_from_url', numbers={'phase': phase, 'shown': shown, 'lines': 1, 'offset': offset},
              subject=safe_label(path), cause=f'{path}:{phase}',
              recover=(Recover.names(basename(path), ('terminal', 'execute_code')),),
              severity='card' if phase == 'line_clamp' else 'digest',
              dead_end=True)        # this read returned nothing new: it is no follow-up on the cut page


@text('web_extract.pdf_from_url')
def _web_copy_recovery(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    if n['phase'] == 'past_eof':
        line = (f"read_file · {s}: offset {n['offset']} is past the end of this cached web copy "
                f"({n['lines']} line); nothing was returned.")
    else:
        line = (f"read_file · {s}: the cached web copy has {n['lines']} line, clamped to {n['shown']:,} chars; "
                'line offsets cannot reach its remaining text.')
    return line, 'ask the agent to process the saved text by character range with execute_code or terminal, not line offsets.'


@rule('web_search.caps_and_snippets', driver='tool', tools={'web_search'},
      core_tells='nobody', status='fixture')
def search_cap(ev: ToolEvent) -> Iterator[Obs]:
    """Exactly 20 rows after a request for more: the cap of several backends, not proof of which one ran."""
    asked = ev.args.get('limit')
    if not isinstance(asked, int) or isinstance(asked, bool) or asked <= _SEARCH_CAP:
        return
    obj = ev.json
    if obj is None or obj.get('success') is not True:
        return
    data = obj.get('data')
    web = data.get('web') if isinstance(data, dict) else None
    if not isinstance(web, list) or len(web) != _SEARCH_CAP or not all(isinstance(row, dict) for row in web):
        return
    yield Obs('web_search.caps_and_snippets', numbers={'asked': asked, 'shown': len(web), 'cap': _SEARCH_CAP},
              subject='web_search', cause=str(ev.args.get('query') or ''), severity='digest',
              note=(f'web_search returned {len(web)} results for limit={asked}; several bundled backends return at '
                    f'most {_SEARCH_CAP}. More matches may exist. Narrower queries or web_extract can add coverage.'))


@text('web_search.caps_and_snippets')
def _search_cap(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"web_search: {n['asked']} results requested, {n['shown']} returned; several bundled backends stop at "
            f"{n['cap']}, so the real match count is unknown.",
            'ask the agent for several narrower queries, or to web_extract the relevant hits.')


# tools/delegate_tool_results.py:211-238 (Hermes v0.23.0).
_SUMMARY_FOOTER = re.compile(
    r'─{8} \[SUMMARY TRUNCATED\] ─{8}\n'
    r'Showing ([\d,]+) chars \(head\) \+ ([\d,]+) chars \(tail\) '
    r"of ([\d,]+) total — trimmed to protect the parent's context window\.\n"
    r'(?:Full subagent output saved to: ([^\n]+)\n'
    r'To read the omitted middle: read_file path="([^"\n]+)" offset=\d+ limit=200  '
    r'\(the file is the complete summary; raise/lower offset to page through it\)\.\n'
    r'|Full output could not be stored to disk; the head\+tail above is all that was preserved\.\n)'
    r'─{37}\Z'
)


@rule('subagent-summary-budget', driver='tool', tools={'delegate_task'}, core_tells='model', status='fixture')
def summary_budget(ev: ToolEvent) -> Iterator[Obs]:
    rows = (ev.json or {}).get('results')
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict) or row.get('summary_truncated') is not True:
            continue
        summary = row.get('summary')
        if not isinstance(summary, str):
            continue
        match = _SUMMARY_FOOTER.search(summary[-1600:])
        if not match:
            continue
        head, tail, total = (int(match[i].replace(',', '')) for i in (1, 2, 3))
        shown = head + tail
        saved = match[4] or ''
        if shown >= total or (saved and (saved != match[5] or saved != row.get('summary_full_path'))):
            continue
        index = row.get('task_index')
        index = index if isinstance(index, int) and not isinstance(index, bool) else -1
        yield Obs('subagent-summary-budget', numbers={'shown': shown, 'total': total, 'head': head,
                                                     'tail': tail, 'stored': int(bool(saved))},
                  subject=f'child {index}' if index >= 0 else 'child summary',
                  cause=saved or f'{ev.tool_call_id}:{index}',
                  recover=(Recover.names(basename(saved)),) if saved else (), severity='card' if shown < 0.8 * total else 'digest')


@text('subagent-summary-budget')
def _summary_budget(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"delegate_task · {s}: {n['shown']:,} of {n['total']:,} summary chars reached the parent; "
            + ('the full summary was saved to a file.' if n['stored'] else 'the omitted middle could not be saved.'),
            None)


@rule('web-extract-stored-copy-2m-cap', driver='tool', tools={'web_extract'},
      core_tells='nobody', status='fixture')
def stored_copy_cap(ev: ToolEvent) -> Iterator[Obs]:
    rows = (ev.json or {}).get('results')
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get('url') or '')
        footer = parse_web_footer(row.get('content'), url)
        if not footer or not footer['path'] or footer['total'] <= _STORED_PAGE_CAP:
            continue
        total = footer['total']
        yield Obs('web-extract-stored-copy-2m-cap', numbers={'shown': _STORED_PAGE_CAP, 'total': total},
                  subject=url_host(url) or safe_label(footer['path']), cause=url or footer['path'],
                  severity='digest')      # the page's own card (head + tail) carries the advice; no model
        # note: the footer this is read from is text inside the page (see signals.parse_web_footer)


@text('web-extract-stored-copy-2m-cap')
def _stored_copy_cap(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"web_extract · {s}: the saved copy keeps only the first {n['shown']:,} of {n['total']:,} source chars, "
            'despite its footer calling it complete.',
            'ask the agent to download the source directly with terminal and process the local file in parts. '
            'Hardcoded in v0.23.0.')


# tools/web_tools_extract.py:175-182 (Hermes v0.23.0); only the error value, not page text.
_BATCH_TIMEOUT = re.compile(r'Extract timed out after (\d+)s via ([a-zA-Z0-9_.-]+)\Z')


@rule('web_extract.batch_timeout', driver='tool', tools={'web_extract'}, core_tells='model', status='fixture')
def batch_timeout(ev: ToolEvent) -> Iterator[Obs]:
    rows = (ev.json or {}).get('results')
    if not isinstance(rows, list) or len(rows) < 2:  # one URL timing out is a plain timeout, not a batch loss
        return
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('error'), str):
            return
        match = _BATCH_TIMEOUT.fullmatch(row['error'])
        if not match:
            return
        matches.append(match.groups())
    seconds, provider = matches[0]
    if any(m != matches[0] for m in matches):
        return
    yield Obs('web_extract.batch_timeout', numbers={'urls': len(rows), 'seconds': int(seconds)},
              subject='web_extract', cause='|'.join(str(r.get('url') or '') for r in rows), severity='digest')


@text('web_extract.batch_timeout')
def _batch_timeout(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"web_extract: the provider batch timed out after {n['seconds']}s; all {n['urls']} URLs returned timeout errors.",
            'ask the agent to split the URLs into smaller calls: the time limit covers the whole batch '
            '(core reads it from web.extract_timeout, a key that is not in the default config).')


# tools/browser_use_cli.py:97,884-888 (Hermes v0.23.0).
_BROWSER_STDERR_CAP = 4000
_STDERR_END = '\n… (stderr truncated)'


@rule('browser-exec-stderr-head-only', driver='tool', tools={'browser_exec'},
      core_tells='model', status='fixture')
def browser_stderr(ev: ToolEvent) -> Iterator[Obs]:
    obj = ev.json
    if obj is None and isinstance(ev.result, dict) and ev.result.get('_multimodal') is True:
        # Native screenshot envelopes retain the exact result JSON in text_summary (:319-322).
        obj = json_object(ev.result.get('text_summary'))
    if obj is None:
        return
    stderr = obj.get('stderr')
    if (not isinstance(stderr, str) or len(stderr) != _BROWSER_STDERR_CAP + len(_STDERR_END)
            or not stderr.endswith(_STDERR_END)):
        return
    yield Obs('browser-exec-stderr-head-only',
              numbers={'shown': _BROWSER_STDERR_CAP, 'total_at_least': _BROWSER_STDERR_CAP + 1},
              subject='browser_exec', cause=ev.tool_call_id or str(ev.args.get('code') or ''), severity='digest')


@text('browser-exec-stderr-head-only')
def _browser_stderr(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"browser_exec: stderr kept only its first {n['shown']:,} chars from at least {n['total_at_least']:,}; "
            'its ending was dropped, with no saved copy.',
            'ask the agent to catch exceptions and print the final traceback lines to stdout. Hardcoded in v0.23.0.')


# tools/vision_tools_history_budget.py:106-112 (Hermes v0.23.0).
_REPEAT_REFUSAL = re.compile(
    r'vision_analyze refused: this image has already been loaded into context (\d+) time\(s\) '
    r'in this session \(region crops of the same file count too\), and every native load re-sends '
    r'the full image on each later API call\. Answer from what you can already see, or ask the '
    r'user\. \(vision\.max_calls_per_image = (\d+); 0 = unlimited\)\Z'
)


@rule('subagent-image-repeat-cap', driver='tool', tools={'vision_analyze'}, core_tells='model', status='fixture')
def image_repeat_cap(ev: ToolEvent) -> Iterator[Obs]:
    obj = ev.json
    if obj is None or obj.get('success') is not False or not isinstance(obj.get('error'), str):
        return
    match = _REPEAT_REFUSAL.fullmatch(obj['error'])
    if not match:
        return
    loads, cap = int(match[1]), int(match[2])
    if cap <= 0 or loads < cap:
        return
    image = str(ev.args.get('image_url') or '')
    yield Obs('subagent-image-repeat-cap', numbers={'loads': loads, 'cap': cap},
              subject=url_host(image) or safe_label(image) or 'image', cause=image, severity='digest')


@text('subagent-image-repeat-cap')
def _image_repeat_cap(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"vision_analyze · {s}: another native load was refused after {n['loads']} loads "
            f"at the per-image cap of {n['cap']}.",
            None)


# tools/session_search_tool.py:237-251 (_shape_message), :299-308,347 (discover: 4000 for the matched window,
# 1200 for bookends), :456 (read: 2000). The scroll path at :569 passes no cap.
def _session_excerpts(obj: dict):
    if obj.get('mode') == 'read':
        groups = [(obj.get('session_id'), obj.get('messages'), 2000)]
    elif obj.get('mode') == 'discover' and isinstance(obj.get('results'), list):
        groups = [(row.get('session_id'), row.get(key), cap) for row in obj['results'] if isinstance(row, dict)
                  for key, cap in (('messages', 4000), ('bookend_start', 1200), ('bookend_end', 1200))]
    else:
        return
    best = {}
    for sid, messages, cap in groups:
        if not isinstance(sid, str) or not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict) or message.get('content_truncated') is not True:
                continue
            content, total, mid = message.get('content'), message.get('original_content_chars'), message.get('id')
            if (not isinstance(content, str) or not content.endswith('…') or type(total) is not int
                    or type(mid) is not int or len(content) - 1 != cap or total <= cap):
                continue
            previous = best.get((sid, mid))
            if previous is None or cap > previous[0]:
                best[(sid, mid)] = (cap, total)
    for (sid, mid), (shown, total) in best.items():
        yield sid, mid, shown, total


@rule('session-search-recovery-cap-4k', driver='tool', tools={'session_search'},
      core_tells='model', status='fixture')
def session_message_cap(ev: ToolEvent) -> Iterator[Obs]:
    obj = ev.json
    if obj is None or obj.get('success') is not True:
        return
    for sid, mid, shown, total in _session_excerpts(obj):
        yield Obs('session-search-recovery-cap-4k', numbers={'shown': shown, 'total': total, 'message_id': mid},
                  subject='stored message', cause=f'{sid}:{mid}', severity='digest')


@text('session-search-recovery-cap-4k')
def _session_message_cap(n: dict, s: str, cfg: Cfg) -> tuple[str, str]:
    return (f"session_search: this excerpt returned {n['shown']:,} of {n['total']:,} chars of one stored message.",
            f"ask the agent to scroll that session with around_message_id={n['message_id']}; "
            'the v0.23.0 scroll path has no per-message character cap.')
