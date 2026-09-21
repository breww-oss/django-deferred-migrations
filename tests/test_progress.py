import itertools
import sys
from io import StringIO

import pytest

from deferred_migrations import progress
from deferred_migrations.context import current_output
from deferred_migrations.context import progress_output
from deferred_migrations.progress import BatchReport
from deferred_migrations.progress import LineReporter
from deferred_migrations.progress import NullReporter
from deferred_migrations.progress import RichReporter
from deferred_migrations.progress import make_reporter


class TerminalStringIO(StringIO):
    def isatty(self) -> bool:
        return True


class FlushCountingStringIO(StringIO):
    flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def test_line_reporter_writes_a_flushed_line_each_interval_and_a_final_line() -> None:
    stream = FlushCountingStringIO()
    ticks = itertools.count(step=4.0)
    reporter = LineReporter(stream, "backfill shop_order.pence", clock=lambda: next(ticks))

    reporter.start(1, 1001)
    reporter.advance(BatchReport(251, 10, 500.0, 250))
    reporter.advance(BatchReport(501, 20, 500.0, 250))
    reporter.advance(BatchReport(751, 30, 500.0, 250))
    reporter.finish()

    lines = stream.getvalue().splitlines()
    assert lines[0].startswith("backfill shop_order.pence: 751/1,001 (75%), 30 written, 500 rows/s, batch 250")
    assert lines[-1].startswith("backfill shop_order.pence: 751/1,001 (75%)")
    assert len(lines) == 2
    assert stream.flushes == 2


def test_line_reporter_output_has_no_terminal_control_codes() -> None:
    stream = StringIO()
    reporter = LineReporter(stream, "backfill t.c", clock=lambda: 0.0)

    reporter.start(1, 10)
    reporter.advance(BatchReport(10, 3, 1.0, 10))
    reporter.finish()

    assert "\x1b" not in stream.getvalue()
    assert "\r" not in stream.getvalue()


def test_line_reporter_reports_position_without_a_percentage_for_non_integer_keys() -> None:
    stream = StringIO()
    reporter = LineReporter(stream, "backfill t.c", clock=lambda: 0.0)

    reporter.start("0001", "ffff")
    reporter.advance(BatchReport("8000", 5, 100.0, 50))
    reporter.finish()

    assert stream.getvalue() == "backfill t.c: at key 8000, 5 written, 100 rows/s, batch 50\n"


def test_make_reporter_uses_a_live_bar_when_rich_says_the_output_is_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTY_INTERACTIVE", "1")
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    stream = StringIO()

    with progress_output(stream, 1):
        reporter = make_reporter("backfill t.c")

    reporter.start(1, 100)
    reporter.advance(BatchReport(50, 5, 10.0, 10))
    reporter.finish()

    assert isinstance(reporter, RichReporter)
    assert "\x1b[" in stream.getvalue()


def test_make_reporter_writes_plain_lines_when_the_output_is_not_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTY_INTERACTIVE", "0")

    with progress_output(TerminalStringIO(), 1):
        assert isinstance(make_reporter("backfill t.c"), LineReporter)


def test_verbosity_zero_silences_progress() -> None:
    with progress_output(StringIO(), 0):
        assert isinstance(make_reporter("backfill t.c"), NullReporter)


def test_without_rich_a_terminal_gets_plain_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "Console", None)
    monkeypatch.setenv("TERM", "xterm-256color")

    with progress_output(TerminalStringIO(), 1):
        assert isinstance(make_reporter("backfill t.c"), LineReporter)

    assert progress.is_interactive(TerminalStringIO())


def test_without_rich_a_dumb_terminal_is_not_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "Console", None)
    monkeypatch.setenv("TERM", "dumb")

    assert not progress.is_interactive(TerminalStringIO())


def test_output_defaults_to_stdout_when_no_command_set_it() -> None:
    target = current_output()

    assert (target.stream, target.verbosity) == (sys.stdout, 1)
