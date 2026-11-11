"""Fine Print plugin entry point. See README.md; the implementation lives in ``fine_print/``."""

from .fine_print.plugin import register

__all__ = ["register"]
