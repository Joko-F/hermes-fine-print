"""Hermes v0.23.0 numbers the detectors and the advice rely on.

The plugin never imports ``tools.*`` or ``agent.*``; each value is copied from core with its file:line and
re-checked against the core functions' own output by ``dev/gen_fixtures.py``.
"""

HERMES_VERSION = "0.23.0"

# web_extract
WEB_EXTRACT_CHAR_LIMIT = 15_000        # web.extract_char_limit default (hermes_cli/config_defaults.py:384)
WEB_EXTRACT_CHAR_LIMIT_MIN = 2_000     # clamp (tools/web_tools_truncate.py:24-44)
WEB_EXTRACT_CHAR_LIMIT_MAX = 500_000
WEB_EXTRACT_URL_CAP = 5                # urls[:5] (tools/web_tools.py:543-548)
WEB_STORED_COPY_CHARS = 2_000_000      # MAX_STORED_TEXT_CHARS (tools/web_tools_truncate.py:22, :70-74)

# search_files
SEARCH_DEFAULT_LIMIT = 50              # DEFAULT_SEARCH_LIMIT (tools/file_operations_search.py:1323)

# tool-result spill (tools/budget_config.py:13-15, tools/tool_result_storage.py)
SPILL_PREVIEW_CHARS = 1_500
SPILL_RESULT_CHARS_MAX = 100_000       # per result: min(100K, max(8K, 0.15 * ctx_tokens * 4))
SPILL_RESULT_CHARS_FLOOR = 8_000
SPILL_TURN_CHARS_MAX = 200_000

# images (tools/vision_tools.py:365, tools/vision_tools_history_budget.py:24-26)
EMBED_MAX_DIMENSION = 1_568
EMBED_TARGET_BYTES_DEFAULT = 262_144   # vision.embed_target_bytes, clamped 65,536..4,194,304
EMBED_TARGET_BYTES_MIN = 65_536
EMBED_TARGET_BYTES_MAX = 4_194_304

# read_file
READ_LINE_CLAMP = 2_000                # tool_output.max_line_length default
XLSX_MAX_ROWS = 5_000                  # _MAX_XLSX_ROWS_PER_SHEET (tools/read_extract.py:30-38)
XLSX_MAX_COLS = 256

# plugin-side budgets
NOTE_MAX_CHARS = 200
NOTES_PER_TURN = 2
CARD_MAX_BULLETS = 3
SHEET_MAX_CHARS = 1_000
IMAGE_HEADER_MAX_B64 = 87_384          # base64 chars decoded at most (64 KB of bytes)

# Tools whose results another bundled plugin (security-guidance) annotates; transform_tool_result is
# first-str-wins, so Fine Print never returns a string for them.
NEVER_ANNOTATE = frozenset({"write_file", "patch", "skill_manage"})

LOCAL_SURFACES = frozenset({"", "cli", "tui", "desktop", "local", "acp"})
