"""Wiring: everything Fine Print registers with Hermes, through public plugin APIs only.

Hooks, one slash command and one system-prompt section, all registered unconditionally. No
middleware, no tools, no monkeypatching, no LLM or network calls.
"""

from __future__ import annotations

import logging
from typing import Any

from .commands import Commands
from .core import FinePrint, guarded
from .ledger import Ledger
from .sheet import SECTION_ID, build_sheet
from . import constants as C

logger = logging.getLogger("fine_print")

PLUGIN_ID = "fine-print"


class HermesHost:
    """The only place that touches Hermes. Every call fails soft."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def setting(self, key: str, default: Any) -> Any:
        try:
            return self.ctx.get_config(key, default)
        except Exception:  # noqa: BLE001
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.ctx.set_config(key, value)

    def config(self, dotted: str, default: Any) -> Any:
        try:
            from hermes_cli.config import load_config_readonly
            node: Any = load_config_readonly() or {}
            for part in dotted.split("."):
                node = node[part]
            return node
        except Exception:  # noqa: BLE001
            return default

    def post(self, text: str) -> bool:
        try:
            return bool(getattr(self.ctx.post_message(text), "ok", False))
        except Exception:  # noqa: BLE001
            logger.debug("fine-print: post_message failed", exc_info=True)
            return False

    def surface(self) -> str:
        try:
            return str(self.ctx.session_surface() or "")
        except Exception:  # noqa: BLE001
            return ""


def _connect():
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN_ID)


def _hermes_version() -> str:
    try:
        from importlib.metadata import version
        return str(version("hermes-agent"))
    except Exception:  # noqa: BLE001
        return ""


def register(ctx: Any) -> None:
    host = HermesHost(ctx)
    fp = FinePrint(host, Ledger(_connect))

    def observer(fn):
        inner = guarded(fn, fp.stats)

        def hook(**kwargs: Any) -> None:
            inner(**kwargs)
        hook.__name__ = inner.__name__
        return hook

    # tool driver: may return an annotated string; None leaves the result untouched
    ctx.register_hook("transform_tool_result", guarded(fp.tool_result, fp.stats))
    # commit driver: one card per turn; on_session_end also closes failed or interrupted turns
    ctx.register_hook("post_llm_call", observer(fp.turn_end))
    ctx.register_hook("on_session_end", observer(fp.turn_end))
    # wire driver: what goes into a request compared with the conversation (image parts by count and size)
    ctx.register_hook("pre_api_request", observer(fp.api_request))
    ctx.register_hook("api_request_error", observer(fp.api_error))
    # route driver: the user message as a turn starts (an observer: it returns nothing, so it injects nothing)
    ctx.register_hook("pre_llm_call", observer(fp.user_turn))
    # response driver: finish reason and token counts of each provider response
    ctx.register_hook("post_api_request", observer(fp.api_response))
    # compaction and aux drivers: a committed compaction, whether its summary call failed, helper-model calls
    ctx.register_hook("post_auxiliary_call", observer(fp.aux_call))
    ctx.register_hook("post_compaction", observer(fp.compaction))
    # bookkeeping
    ctx.register_hook("subagent_start", observer(fp.subagent_start))
    ctx.register_hook("on_session_delete", observer(fp.session_deleted))

    report = None
    try:
        from .report import history_report
        report = lambda days, html=False: history_report(fp.registry, days, host.config, html)  # noqa: E731
    except Exception:  # noqa: BLE001
        logger.debug("fine-print: history report unavailable", exc_info=True)
    commands = Commands(fp, host.set_setting, report, _hermes_version)

    def limits(raw_args: str = "", session_id: str = "", platform: str = "", **_: Any) -> str:
        try:
            return commands.handle(raw_args, session_id=session_id, platform=platform)
        except Exception:  # noqa: BLE001
            logger.warning("fine-print: /limits failed", exc_info=True)
            return "Fine Print: /limits failed; see the agent log."

    ctx.register_command("limits", handler=limits,
                         description="What Hermes silently cut, shrank or swapped, and how to fix it",
                         args_hint="[last|topics|explain <id>|report|self-test|notes on|off]")

    def sheet(_session_info: Any = None) -> str:
        try:
            return build_sheet(host.config) if commands.sheet_on() else ""
        except Exception:  # noqa: BLE001
            return ""

    ctx.register_system_prompt_section(SECTION_ID, sheet, max_chars=C.SHEET_MAX_CHARS)
