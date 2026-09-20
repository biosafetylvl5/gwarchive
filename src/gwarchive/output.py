"""Rendering. Every human-facing line in gwarchive comes out of this module.

Everything renders through ``rich.text.Text``, so user data is never parsed as
markup: a folder called ``P0001 [draft] Notes`` prints with the ``[draft]``
intact rather than having it swallowed as a style tag. Style comes from the
``style=`` argument, never from inline tags. That bug class ate bracketed names
for the whole first version of the tool.

Layer 1. This module imports ``gwarchive.naming`` and stdlib and nothing else
from the package -- it is what every other layer calls ``die()`` on, so if it
reached upward the whole graph would cycle. ``tests/test_structure.py``
enforces that.
"""

import json
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

console = Console()
err_console = Console(stderr=True)


QUIET = False
VERBOSE = False


Renderable = str | Text


# The visual grammar. Every human-facing line is a symbol, a gutter, then
# content -- so the kind of a line is legible before the line is read. Each
# entry is (glyph, ascii fallback), both one cell wide so the content column
# lands in the same place whether or not the terminal can draw the first.
# ``symbol()`` is the only thing allowed to know what a kind looks like.
SYMBOLS: dict[str, tuple[str, str]] = {
    "ok": ("\u2713", "+"),
    "info": ("\u2022", "-"),
    "warn": ("!", "!"),
    "error": ("\u2717", "x"),
    "transfer": ("\u2192", ">"),
}


# Green/cyan/yellow/red is a stoplight and nothing else, so one warm note is
# reserved as the accent: the thing you searched for, and the number you came
# to read. It was already in here twice, hardcoded, before it had a name.
STYLES: dict[str, str] = {
    "ok": "green",
    "info": "cyan",
    "warn": "yellow",
    "error": "red",
    "accent": "magenta",
    "transfer": "magenta",
    "label": "dim",
    "caption": "dim",
}


# One spinner everywhere. bouncingBar is plain ASCII where Rich's default is
# braille, so it draws correctly on terminals that render U+280x as boxes --
# the same reason ``symbol`` keeps an ASCII fallback for every glyph.
SPINNER = "bouncingBar"


GUTTER = "  "  # between the symbol and the content
INDENT = " " * (1 + len(GUTTER))  # continuation lines align under the content
LABEL_WIDTH = 6  # "from", "to", "frees" -- one column across every command


def _as_text(message: Renderable) -> Text:
    return message if isinstance(message, Text) else Text(message)


def pl(count: int, one: str, many: str = "") -> str:
    """The noun alone, for mid-sentence use. ``many`` covers the irregulars."""
    return one if count == 1 else (many or one + "s")


def plural(count: int, one: str, many: str = "") -> str:
    """``1 error`` / ``2 errors``. One place decides how the tool counts things."""
    return f"{count} {pl(count, one, many)}"


def plain() -> bool:
    """True when output is going somewhere that can't render box drawing."""
    return not console.is_terminal or os.environ.get("TERM", "") == "dumb"


def interactive(as_json: bool = False) -> bool:
    """True when transient UI -- spinners, prompts -- is worth drawing."""
    return console.is_terminal and not QUIET and not as_json


def symbol(kind: str) -> str:
    """The single place that knows what a message kind looks like."""
    glyph, fallback = SYMBOLS[kind]
    return fallback if plain() else glyph


def _decorate(kind: str, message: Renderable, indent: str = "") -> Text:
    """Prefix a message with its symbol; continuation lines keep the column.

    The only place that puts a symbol in front of a line. ``indent`` is for a
    nested finding, which used to hand-roll this prefix and lose the
    continuation column doing it.
    """
    body = _as_text(message)
    if not body.plain.strip():
        return body  # a deliberate blank spacer keeps its own shape
    out = Text(indent)
    out.append(symbol(kind), style=STYLES[kind])
    out.append(GUTTER)
    out.append_text(Text("\n" + indent + INDENT).join(body.split()))
    return out


def ok(message: Renderable) -> None:
    """Report a completed action on stdout."""
    if not QUIET:
        console.print(_decorate("ok", message), soft_wrap=True)


def note(message: Renderable) -> None:
    """Report unstyled information on stdout."""
    if not QUIET:
        console.print(_decorate("info", message), soft_wrap=True)


def caption(message: Renderable) -> None:
    """A trailing summary. Reads as chrome rather than as content."""
    if not QUIET:
        console.print(_decorate("info", message), style=STYLES["caption"], soft_wrap=True)


def detail(message: Renderable) -> None:
    """Report information that only matters under --verbose."""
    if VERBOSE and not QUIET:
        console.print(_decorate("info", message), style="dim", soft_wrap=True)


def warn(message: Renderable) -> None:
    """Report a non-fatal problem on stderr."""
    err_console.print(_decorate("warn", message), style=STYLES["warn"], soft_wrap=True)


def error(message: Renderable) -> None:
    """Report a fatal problem on stderr."""
    err_console.print(_decorate("error", message), style=STYLES["error"], soft_wrap=True)


def hint(command: str) -> None:
    """The command that gets the user unstuck, under the message it follows."""
    err_console.print(Text(f"{INDENT}try:  {command}"), style=STYLES["caption"], soft_wrap=True)


def die(message: Renderable, code: int = 1, fix: str | None = None) -> typer.Exit:
    """Report a fatal problem and exit. Returns the exception so callers can
    ``raise die(...)`` and keep control flow obvious to readers and to mypy.

    ``fix`` is a runnable command, printed under the message.
    """
    error(message)
    if fix:
        hint(fix)
    return typer.Exit(code)


def panel(body: Renderable, title: str | Text | None = None, kind: str = "info") -> Panel:
    """A framed block: rounded where we can draw it, ASCII where we can't."""
    return Panel(
        _as_text(body),
        title=title,
        border_style=STYLES[kind],
        box=box.ASCII if plain() else box.ROUNDED,
        expand=False,
        padding=(0, 1),
    )


def confirm_destructive(summary: Renderable, prompt: str, *, yes: bool) -> bool:
    """Show what is about to be destroyed, then ask.

    Every irreversible prompt goes through here, so --yes is honored in exactly
    one place and no call site can accidentally skip the summary.
    """
    if yes:
        return True
    # Summary and prompt both on stderr: stdout may be carrying a --json
    # document, and click writes its prompt to stdout unless told otherwise.
    err_console.print(panel(summary, kind="warn"))
    return typer.confirm(prompt, err=True)


def new_table(title: str | Text | None = None) -> Table:
    """Tables recede: one dim rule under the header, no vertical rules.

    ``title`` takes a ``Text`` for the same reason every other helper here does:
    Rich markup-parses a bare ``str``, so a folder or a search pattern
    interpolated into one loses its ``[draft]`` -- or raises MarkupError and
    turns a successful search into a traceback.
    """
    if plain():
        return Table(title=title, box=box.ASCII, safe_box=True)
    return Table(
        title=title,
        box=box.SIMPLE_HEAD,
        title_style="bold",
        header_style="bold dim",
        border_style="dim",
        pad_edge=False,
    )


@contextmanager
def spinner(label: str | None, *, as_json: bool = False) -> Iterator[Status | None]:
    """A Rich status on a real terminal, and nothing at all anywhere else.

    The one place that decides when transient UI is drawn, so piped output and
    --json stay byte-identical to a run without it. ``label=None`` means no
    spinner, which is how a caller gates on its own condition. Yields the
    Status so a long walk can relabel itself.
    """
    if label is None or not interactive(as_json):
        yield None
        return
    with console.status(Text(label), spinner=SPINNER) as status:
        yield status


def emit_json(payload: object) -> None:
    """Write structured output. Bypasses rich so nothing is wrapped or styled."""
    print(json.dumps(payload, indent=2, sort_keys=False))


def labelled_message(verb: str, rows: Sequence[tuple[str, str]]) -> Text:
    """A verb, then one labelled value per line -- so nothing wraps mid-token.

    Every transfer in the tool renders through here, local or remote, so mv,
    cp, push and restore read as one family. The symbol added by ``ok`` /
    ``note`` carries the colour; the verb only needs weight.
    """
    message = Text()
    message.append(verb, style="bold")
    for label, value in rows:
        message.append("\n")
        message.append(f"{label:<{LABEL_WIDTH}}", style=STYLES["label"])
        message.append(value)
    return message


def remote_message(verb: str, source: str, targets: Sequence[str]) -> Text:
    """transfer_message's shape for a local/remote pair; remotes are strings."""
    return labelled_message(verb, [("from", source), *(("to", target) for target in targets)])


def set_verbosity(quiet: bool, verbose: bool) -> None:
    """Set the verbosity flags. The root callback calls this; nothing else should.

    This exists because a ``global`` statement binds in the *enclosing module's*
    namespace. The root callback used to do ``global QUIET, VERBOSE`` directly,
    which worked while it lived in the same file as the flags. Split across
    modules it silently writes the caller's globals instead, leaving these two
    False forever and turning --quiet and --verbose into no-ops -- with no
    import error and no failing layer check, because a ``global`` write is a
    Store, not a Load, and creates no edge in any import graph. Reproduced
    before it was fixed. The write has to happen where the names live.
    """
    global QUIET, VERBOSE
    QUIET = quiet
    VERBOSE = verbose


def program() -> str:
    """What to tell the user to type. Not necessarily how this process was started.

    Under the console script that is "gwarchive". Under the zipapp there is no
    `gwarchive` on PATH -- that is the whole premise of a self-contained .pyz --
    so advising it would send the reader to a command-not-found. A hint is
    advice about what to type; `launcher()` in commands/shell.py answers the
    different question of what to EXECUTE, and its answer goes into the emitted
    shell functions, where the string is run rather than read.
    """
    argv0 = Path(sys.argv[0])
    if argv0.suffix == ".pyz":
        return f"python {argv0.name}"
    return "gwarchive"
