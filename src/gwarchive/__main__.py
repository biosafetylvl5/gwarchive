"""Entry point for both `gwarchive` (the console script) and `python -m gwarchive`.

The version guard is written in syntax that parses on Python 3.8 and runs
before any other import, because /usr/bin/env python3 on a stranger's machine
may be older than this package supports. Without it, the first failure is a
`TypeError: unsupported operand type(s) for |` raised from deep inside typer
while it resolves the PEP 604 unions in the Annotated option aliases -- a
traceback that never mentions Python versions.
"""

import sys

if sys.version_info < (3, 11):  # noqa: UP036
    sys.exit(
        f"gwarchive requires Python 3.11 or newer; "
        f"this interpreter is {sys.version_info[0]}.{sys.version_info[1]}"
    )

from gwarchive import commands  # noqa: E402,F401  registers all 19 commands on `app`
from gwarchive.options import app  # noqa: E402


def main() -> None:
    """Run the CLI. This is the console-script entry point."""
    app()


if __name__ == "__main__":
    main()
