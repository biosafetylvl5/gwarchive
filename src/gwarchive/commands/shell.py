"""Shell integration: clears, cd, shell-init.

cd uses a bare print() and a silent sys.exit(1). Its stdout is consumed by
`cd $(...)`, so a failed lookup must never emit a string that could be
substituted into a cd argument. Keep that path unformatted."""

import os
import shlex
import sys
import zipfile
from pathlib import Path
from typing import Annotated

import typer

from gwarchive import external
from gwarchive.options import (
    BasePath,
    app,
)
from gwarchive.output import (
    console,
    detail,
)
from gwarchive.paths import (
    resolve_prefix,
)

# The sprite ``clears`` greets with when neither the positional argument nor
# $GWARCHIVE_POKEMON says otherwise.
DEFAULT_POKEMON = "bulbasaur"


def launcher() -> list[str]:
    """The argv prefix that re-runs *this* build of gwarchive from a shell.

        console script   ['/Users/x/.local/bin/gwarchive']
        zipapp           ['/usr/bin/python3', '/opt/g.pyz']
        checkout / test  ['/path/.venv/bin/python', '-m', 'gwarchive']

    This is not `program()`. That one answers "what should I tell the user to
    type"; this one answers "what will actually run", and its result is
    interpolated into shell functions that get executed rather than read.

    Deliberately not sys.argv[0] unconditionally: under pytest argv[0] is
    pytest's own path, so the fallback has to be a form that runs from anywhere
    the package is importable. The previous implementation used
    Path(__file__).resolve(), which pointed at g.py when g.py was the whole
    tool and points at a package submodule now -- a path that exists but is not
    runnable, because its absolute imports fail when it is executed as a script.
    """
    argv0 = Path(sys.argv[0])
    if argv0.name == "gwarchive" and argv0.is_file():
        return [str(argv0.resolve())]
    if zipfile.is_zipfile(sys.argv[0]):
        return [sys.executable, str(argv0.resolve())]
    return [sys.executable, "-m", "gwarchive"]


@app.command()
def clears(
    pokemon: Annotated[
        str | None, typer.Argument(help="Pokemon to display; defaults to $GWARCHIVE_POKEMON or bulbasaur")
    ] = None,
) -> None:
    """Clear the screen and greet with a pokemon sprite (via pokeget).

    The shell-init output defines a 'clears' function wrapping this command;
    pass --greet to shell-init to run it once per shell startup.
    """
    chosen = pokemon or os.environ.get("GWARCHIVE_POKEMON") or DEFAULT_POKEMON
    if console.is_terminal:
        console.clear()
    if external.run_pokeget([chosen, "--hide-name"]) is None:
        detail("pokeget is not on your PATH; cleared without a greeter.")


@app.command()
def cd(
    prefix: Annotated[
        str | None,
        typer.Argument(help="Folder prefix (e.g., 'P1', 'P01', 'P001', or 'P0001'). Omit for the root."),
    ] = None,
    *,
    base_path: BasePath,
) -> None:
    """Print the path to a folder, for shell navigation.

    Wire up the 'gcd' / 'ggd' shell functions with:

        eval "$(gwarchive shell-init)"

    Then:  gcd P1

    This command prints nothing and exits 1 on failure, so a failed lookup can
    never be substituted into a 'cd' argument.
    """

    if not prefix:
        # Absolute, like the prefix branch below: `cd $(...)` from another
        # directory has to land in the same place either way.
        print(base_path.absolute())
        return

    folder = resolve_prefix(prefix, base_path, quiet=True)
    if not folder:
        sys.exit(1)

    print(folder.absolute())


@app.command("shell-init")
def shell_init(
    path: Annotated[
        Path | None, typer.Option("--path", help="Bake a fixed base path into the functions")
    ] = None,
    greet: Annotated[bool, typer.Option("--greet", help="Run 'clears' once when the shell starts")] = False,
) -> None:
    """Emit shell functions for bash/zsh. Use with: eval "$(gwarchive shell-init)"

    Defines 'gcd' and 'ggd' (same body, two names) and 'clears', which clears
    the screen and shows a pokemon. With --greet, 'clears' also runs once as
    the eval happens -- the pokemon-at-shell-startup greeting.
    """
    # "launch", not "prefix": in this codebase a prefix is P0001.
    launch = " ".join(shlex.quote(part) for part in launcher())
    base_arg = f" --path {shlex.quote(str(path))}" if path else ""
    greet_line = "\nclears" if greet else ""

    print(
        f"""_gwarchive_cd() {{
    local target
    target="$({launch} cd "$@"{base_arg})" || return 1
    if [ -z "$target" ]; then
        echo "gwarchive: no folder matching '$*'" >&2
        return 1
    fi
    cd "$target" || return 1
}}
gcd() {{ _gwarchive_cd "$@"; }}
ggd() {{ _gwarchive_cd "$@"; }}
clears() {{ {launch} clears "$@"; }}{greet_line}"""
    )
