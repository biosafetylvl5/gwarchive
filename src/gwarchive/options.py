"""The Typer app, the root callback, and the Annotated aliases every command is built from.

One declaration per option, however many commands take it. `--path` had been
written out sixteen times, and three real defects had grown in that drift:
verify was missing -y, and find/stats had lost the [P|R|M|A|O] metavar their
siblings show. An option whose help text genuinely differs per command stays
inline -- a shared alias would flatten it, which is why there are three --json
wordings rather than one.

BasePath resolves itself via default_factory=get_base_path, so no command body
opens by resolving --path. default_factory forbids a `=` default, which is why
base_path is keyword-only (the bare `*`) in every signature, and
show_default=False is what keeps "[default: (dynamic)]" out of --help.

Two things about these aliases that are easy to get wrong:

`default_factory=get_base_path` and `callback=validate_category` capture the
FUNCTION OBJECT at import time. They are therefore not monkeypatchable, unlike
the clock/external/tarball seams. That is fine today -- get_base_path reads
$GWARCHIVE_BASE at call time and the test fixture sets the env var instead --
but anyone reaching for setattr(paths, "get_base_path", ...) will find it inert.

Do NOT add `from __future__ import annotations` to this module. Typer resolves
Annotated metadata at runtime via get_type_hints, and postponed evaluation has
known edge cases with the typer.Option objects living inside Annotated.
"""

from pathlib import Path
from typing import Annotated

import typer

from gwarchive import __version__, output
from gwarchive.naming import CATEGORIES, CATEGORY_METAVAR
from gwarchive.paths import get_base_path

app = typer.Typer(
    help="GWArchive CLI tool for file organization",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def validate_category(value: str | None) -> str | None:
    """Accept a category letter in any case; reject anything else loudly.

    An invalid category must never fall through to "search everything" -- a
    silent wrong answer is worse than an error.
    """
    if value is None:
        return None
    candidate = value.strip().upper()
    if candidate not in CATEGORIES:
        raise typer.BadParameter(
            f"{value!r} is not a category. Choose one of: "
            + ", ".join(f"{k} ({v})" for k, v in CATEGORIES.items())
        )
    return candidate


def version_callback(value: bool) -> None:
    if value:
        print(f"gwarchive {__version__}")
        raise typer.Exit()


@app.callback()
def root_callback(
    version: bool = typer.Option(
        False, "--version", callback=version_callback, is_eager=True, help="Show the version and exit"
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress success output; errors still go to stderr"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show extra detail"),
) -> None:
    """GWArchive CLI tool for file organization."""
    output.set_verbosity(quiet, verbose)


BasePath = Annotated[
    Path,
    typer.Option("--path", default_factory=get_base_path, show_default=False, help="Base path for GWArchive"),
]
DryRun = Annotated[bool, typer.Option("--dry-run", help="Show what would happen without making changes")]
# Three --json wordings, because there are three output shapes to replace.
AsJsonTable = Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table")]
AsJsonReport = Annotated[bool, typer.Option("--json", help="Emit JSON instead of a report")]
AsJsonSync = Annotated[bool, typer.Option("--json", help="Emit JSON output")]
Force = Annotated[bool, typer.Option("--force", help="Replace the destination if it already exists")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")]
CategoryArg = Annotated[
    str,
    typer.Argument(callback=validate_category, metavar=CATEGORY_METAVAR, help="Category (P, R, M, A, O)"),
]
CategoryOpt = Annotated[
    str | None,
    typer.Option(
        "--category",
        "-c",
        callback=validate_category,
        metavar=CATEGORY_METAVAR,
        help="Limit to one category (P, R, M, A, O)",
    ),
]
SourceArg = Annotated[str, typer.Argument(help="Source prefix or path")]
Selector = Annotated[str, typer.Argument(help="Category (P, R, M, A, O) or folder prefix/path")]
PushRemotes = Annotated[
    list[str] | None,
    typer.Option(
        "--remote", help="Remote destination, e.g. nas:archive (default $GWARCHIVE_REMOTE). Can be repeated."
    ),
]
PullRemote = Annotated[
    str | None,
    typer.Option("--remote", help="Remote source (defaults to recorded remote or $GWARCHIVE_REMOTE)"),
]
ArchiveVersion = Annotated[
    str | None,
    typer.Option("--version", help="Archived copy to fetch: 1 is newest (default), or an object name"),
]
Keep = Annotated[
    int | None,
    typer.Option("--keep", help="Archived copies to retain per remote (default 1; 0 keeps every copy)"),
]
NoCompressPush = Annotated[
    bool, typer.Option("--no-compress", help="Copy files individually instead of one compressed archive")
]
NoCompressPull = Annotated[
    bool, typer.Option("--no-compress", help="Copy files individually, ignoring any recorded archive")
]
RestoreKeepLocal = Annotated[
    bool,
    typer.Option(
        "--keep-local", help="Clear offloaded status using the files already present; fetch nothing"
    ),
]
