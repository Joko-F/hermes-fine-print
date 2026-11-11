"""``/limits report html [days]``: the same scan as the text report, written as one page into the
profile's cache folder; the reply names the file and nothing from the history."""

import time

from fine_print.commands import Commands
from fine_print.engine import REGISTRY
from fine_print.report import history_report
from tests.test_audit2_report import make_db, web_turn


def commands(fp):
    return Commands(fp, lambda key, value: None, lambda days, html=False: history_report(REGISTRY, days, None, html))


def history(tmp_path, monkeypatch, turns):
    db = tmp_path / "state.db"
    make_db(str(db), turns)
    monkeypatch.setenv("FINE_PRINT_STATE_DB", str(db))
    monkeypatch.setenv("FINE_PRINT_REPORT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(time, "time", lambda: 2000.0)          # the synthetic rows are stamped just after 1000


def test_report_html_writes_one_page_and_names_it(fp, tmp_path, monkeypatch):
    history(tmp_path, monkeypatch, [web_turn("https://secret-host.example/a"), web_turn("https://secret-host.example/b")])
    reply = commands(fp).handle("report html 7", "s1")
    page = tmp_path / "out" / "limits-report.html"
    assert page.exists() and str(page) in reply
    assert reply.startswith("Fine Print · report page written: ") and "aggregates only" in reply
    html = page.read_text(encoding="utf-8")
    assert html.lower().startswith("<!doctype html>") and "The last 7 days" in html
    assert "secret-host" not in html and "secret-host" not in reply
    assert "<script" not in html.lower() and "http://" not in html and "https://" not in html


def test_report_html_defaults_to_30_days_and_text_report_is_unchanged(fp, tmp_path, monkeypatch):
    history(tmp_path, monkeypatch, [web_turn("https://h.example/a")])
    text = commands(fp).handle("report", "s1")
    assert not (tmp_path / "out").exists()                        # the text report writes nothing
    assert "report page written" not in text
    commands(fp).handle("report html", "s1")
    assert "The last 30 days" in (tmp_path / "out" / "limits-report.html").read_text(encoding="utf-8")


def test_report_html_with_a_bad_number_shows_usage(fp, tmp_path, monkeypatch):
    history(tmp_path, monkeypatch, [])
    assert commands(fp).handle("report html soon", "s1") == "Usage: /limits report [html] [days]"
    assert not (tmp_path / "out").exists()


def test_report_html_without_a_database_says_so_and_writes_nothing(fp, tmp_path, monkeypatch):
    monkeypatch.setenv("FINE_PRINT_STATE_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FINE_PRINT_REPORT_DIR", str(tmp_path / "out"))
    assert commands(fp).handle("report html", "s1") == "Fine Print: no session database found for this profile yet."
    assert not (tmp_path / "out").exists()


def test_a_folder_that_cannot_be_written_is_reported_not_raised(fp, tmp_path, monkeypatch):
    history(tmp_path, monkeypatch, [web_turn("https://h.example/a")])
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    monkeypatch.setenv("FINE_PRINT_REPORT_DIR", str(blocker / "sub"))
    reply = commands(fp).handle("report html", "s1")
    assert reply.startswith("Fine Print: the report page could not be written (")


def test_html_may_stand_on_either_side_of_the_number(fp, tmp_path, monkeypatch):
    history(tmp_path, monkeypatch, [web_turn("https://h.example/a")])
    assert commands(fp).handle("report 7 html", "s1").startswith("Fine Print · report page written: ")
    assert "The last 7 days" in (tmp_path / "out" / "limits-report.html").read_text(encoding="utf-8")
    assert commands(fp).handle("report HTML", "s1").startswith("Fine Print · report page written: ")


def test_extra_words_after_report_show_usage(fp, tmp_path, monkeypatch):
    history(tmp_path, monkeypatch, [])
    assert commands(fp).handle("report html 7 8", "s1") == "Usage: /limits report [html] [days]"
    assert commands(fp).handle("report 7 8", "s1") == "Usage: /limits report [html] [days]"
