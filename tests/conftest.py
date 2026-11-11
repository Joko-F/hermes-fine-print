"""Test setup: the inner package imports without Hermes, so the suite runs on plain Python."""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fine_print.core import FinePrint  # noqa: E402
from fine_print.ledger import Ledger  # noqa: E402


class FakeHost:
    def __init__(self):
        self.settings = {}
        self.cfg = {}
        self.posted = []
        self.surface_name = "cli"

    def setting(self, key, default):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def config(self, dotted, default):
        return self.cfg.get(dotted, default)

    def post(self, text):
        self.posted.append(text)
        return True

    def surface(self):
        return self.surface_name


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def fp(host):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return FinePrint(host, Ledger(lambda: conn))


FIXTURES = ROOT / "tests" / "fixtures"
