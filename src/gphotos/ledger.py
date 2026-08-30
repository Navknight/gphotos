"""Append-only TSV progress logs. Deliberately not a database.

Every pass that touches files writes one row per file before it moves on, so a
run that dies -- power cut, USB bridge dropping off the bus, Ctrl-C -- resumes
instead of starting over, and afterwards there is a plain-text record of what
was done to each file that `grep` can answer questions about at 3am.

Two rules the format depends on:

  * append only. A row is never edited. A file that failed and then succeeded
    appears twice; the LAST row wins. Rewriting rows in place is how a crash
    mid-write turns a ledger into a shorter ledger.
  * flush and fsync periodically, not per row (too slow) and not never (a
    crash then loses the tail, and the run repeats work it already did).

Statuses in SETTLED mean "done, never touch this file again". Everything else
is a failure whose cause may since have been fixed -- a read-only directory
chmod'd, a drive remounted -- so a later run must be free to retry it rather
than inherit the failure forever.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

SETTLED = {"OK", "NOOP"}
FSYNC_EVERY = 50


def latest(path, key_col: int = 0, min_cols: int = 2) -> dict[str, list[str]]:
    """{key: last row for that key}. Missing file means an empty dict."""
    out: dict[str, list[str]] = {}
    path = Path(path)
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if len(row) < min_cols or row[key_col] == "path":
                continue
            out[row[key_col]] = row
    return out


def statuses(path, status_col: int = 4) -> dict[str, str]:
    """{path: last recorded status}, for resume decisions."""
    return {k: r[status_col] for k, r in latest(path, min_cols=status_col + 1).items()}


class Writer:
    """Context manager appending rows, fsyncing every FSYNC_EVERY rows."""

    def __init__(self, path, header):
        self.path = Path(path)
        self.header = header
        self.n = 0
        self._fh = None
        self._w = None

    def __enter__(self):
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh, delimiter="\t", lineterminator="\n")
        if fresh:
            self._w.writerow(self.header)
        return self

    def write(self, row):
        self._w.writerow(row)
        self.n += 1
        if self.n % FSYNC_EVERY == 0:
            self.sync()

    def sync(self):
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def __exit__(self, *exc):
        self.sync()
        self._fh.close()
        return False
