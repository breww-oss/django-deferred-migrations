import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from typing import TextIO

from deferred_migrations.context import current_output

try:
    from rich.console import Console
    from rich.progress import BarColumn
    from rich.progress import Progress
    from rich.progress import TaskID
    from rich.progress import TextColumn
    from rich.progress import TimeRemainingColumn
except ImportError:  # rich is the optional [rich] extra
    Console = None


@dataclass(frozen=True)
class BatchReport:
    position: object
    rows_written: int
    rows_per_second: float
    batch_size: int


class ProgressReporter(Protocol):
    def start(self, lowest: object, highest: object) -> None: ...

    def advance(self, report: BatchReport) -> None: ...

    def finish(self) -> None: ...


def is_integer_key(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def key_fraction(lowest: object, highest: object, position: object) -> float | None:
    if not all(is_integer_key(value) for value in (lowest, highest, position)):
        return None

    if highest == lowest:
        return 1.0

    return (position - lowest) / (highest - lowest)


def format_duration(seconds: float) -> str:
    minutes, remaining_seconds = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}h{minutes:02d}m"

    if minutes:
        return f"{minutes}m"

    return f"{remaining_seconds}s"


class NullReporter:
    def start(self, lowest: object, highest: object) -> None:
        pass

    def advance(self, report: BatchReport) -> None:
        pass

    def finish(self) -> None:
        pass


# A Job's log is a pipe the container runtime splits into timestamped lines, so progress there must be whole, flushed lines with no cursor codes.
class LineReporter:
    INTERVAL_SECONDS = 10.0

    def __init__(self, stream: TextIO, label: str, clock: Callable[[], float] = time.monotonic, interval: float = INTERVAL_SECONDS) -> None:
        self.stream = stream
        self.label = label
        self.clock = clock
        self.interval = interval
        self.lowest: object = None
        self.highest: object = None
        self.started = 0.0
        self.last_line = 0.0
        self.latest: BatchReport | None = None

    def start(self, lowest: object, highest: object) -> None:
        self.lowest = lowest
        self.highest = highest
        self.started = self.last_line = self.clock()

    def advance(self, report: BatchReport) -> None:
        self.latest = report

        if self.clock() - self.last_line >= self.interval:
            self.write_line(report)

    def finish(self) -> None:
        if self.latest is not None:
            self.write_line(self.latest)

    def write_line(self, report: BatchReport) -> None:
        self.last_line = self.clock()
        self.stream.write(f"{self.describe(report)}\n")
        self.stream.flush()

    def describe(self, report: BatchReport) -> str:
        fraction = key_fraction(self.lowest, self.highest, report.position)
        parts = [f"at key {report.position}" if fraction is None else f"{report.position:,}/{self.highest:,} ({fraction:.0%})"]
        parts += [f"{report.rows_written:,} written", f"{report.rows_per_second:,.0f} rows/s", f"batch {report.batch_size:,}"]
        elapsed = self.clock() - self.started

        if fraction is not None and 0 < fraction < 1 and elapsed > 0:
            parts.append(f"~{format_duration(elapsed * (1 - fraction) / fraction)} left")

        return f"{self.label}: {', '.join(parts)}"


class RichReporter:
    def __init__(self, stream: TextIO, label: str) -> None:
        self.label = label
        self.progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            TextColumn("{task.fields[written]:,} written"),
            TextColumn("{task.fields[rate]:,.0f} rows/s"),
            TextColumn("batch {task.fields[batch]:,}"),
            TimeRemainingColumn(),
            console=Console(file=stream),
        )
        self.task: TaskID | None = None
        self.lowest: object = None
        self.total: int | None = None

    def start(self, lowest: object, highest: object) -> None:
        self.lowest = lowest
        self.total = max(highest - lowest, 1) if key_fraction(lowest, highest, lowest) is not None else None
        self.progress.start()
        self.task = self.progress.add_task(self.label, total=self.total, written=0, rate=0.0, batch=0)

    def advance(self, report: BatchReport) -> None:
        completed = report.position - self.lowest if self.total is not None else None
        self.progress.update(self.task, completed=completed, written=report.rows_written, rate=report.rows_per_second, batch=report.batch_size)

    def finish(self) -> None:
        if self.task is not None and self.total is not None:
            self.progress.update(self.task, completed=self.total)

        self.progress.stop()


# A live bar redraws in place with carriage returns and cursor codes, which only a terminal interprets.
def is_interactive(stream: TextIO) -> bool:
    if Console is not None:
        return Console(file=stream).is_interactive

    isatty = getattr(stream, "isatty", None)
    return isatty is not None and isatty() and os.environ.get("TERM") != "dumb"


def make_reporter(label: str) -> ProgressReporter:
    target = current_output()

    if target.verbosity < 1:
        return NullReporter()

    if Console is not None and is_interactive(target.stream):
        return RichReporter(target.stream, label)

    return LineReporter(target.stream, label)
