"""Timestamps. Small on purpose.

This is one of three SEAM modules (with `external` and `tarball`): it wraps
non-determinism, so tests monkeypatch it. Callers must import the MODULE and
write `clock.archive_stamp()`, never `from gwarchive.clock import
archive_stamp` -- a from-import binds the real function at import time and the
patch then has nothing to reach. tests/test_structure.py enforces this.

Keeping the seams small and few is what keeps that rule cheap. Folding these
three functions into `naming` would make a module that everything from-imports
into a seam, and the rule would spread across the whole package.
"""

from datetime import datetime


def now_stamp() -> str:
    """The timestamp every tombstone field is written with."""
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    """The date segment of an ``Old/`` name and of a quarantine directory."""
    return datetime.now().strftime("%Y-%m-%d")


def archive_stamp() -> str:
    """The timestamp segment of an object name: sortable, and colon-free.

    Not ISO 8601 -- a colon in an object name is trouble on several backends.
    """
    return datetime.now().strftime("%Y%m%d-%H%M%S")
