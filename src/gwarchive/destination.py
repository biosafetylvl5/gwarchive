"""Where a folder is going: destination resolution and the pre-flight guards.

`resolve_destination` is shared by mv, cp, rename and oldify. Put destination
logic here, not in a command -- cp's half-implemented version of it was a real
bug, and the four commands drifting apart is what this module exists to stop.

The guards run before any filesystem call. `warn_if_taken` is the read-only
half of `check_no_overwrite`, which exists because check_no_overwrite DELETES
under --force and so cannot run under --dry-run: without the split, a dry run
printed "Would move" and exited 0 for a destination the real command rejects.
"""

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import typer

from gwarchive import clock
from gwarchive.naming import (
    CATEGORIES,
    CATEGORY_NAMES,
    category_letter_for,
    folder_descriptor,
    folder_prefix,
    normalize_prefix,
    rename_preserving_prefix,
)
from gwarchive.output import die, note, program, warn
from gwarchive.paths import (
    allocate_number,
    display,
    find_folders_by_prefix,
    rel,
    resolve_prefix,
)


def name_in_category(
    source_name: str,
    dest_letter: str,
    base_path: Path,
    date_override: str | None = None,
    fresh_number: bool = False,
) -> str:
    prefix = folder_prefix(source_name)
    if prefix is None:
        return source_name

    descriptor = folder_descriptor(source_name) or "Unnamed"

    if fresh_number:
        # A copy is a new thing, so it gets a new identity in the category it
        # is created in. Reusing the source's prefix would mean two folders
        # claiming to be the same one.
        prefix = f"{dest_letter}{allocate_number(dest_letter, base_path):04d}"

    if dest_letter == "O":
        date = date_override or clock.today()
        return f"{date}-{prefix}-{descriptor}"

    # Prefixes are permanent: the letter is not rewritten to match the
    # destination. Rewriting it is what used to collide with whatever already
    # held those digits in the destination category.
    return f"{prefix} {descriptor}"


def _literal_destination(destination: str, base_path: Path) -> Path:
    """The literal-path form of a destination, anchored inside the archive.

    A relative path resolved against the shell's cwd, so ``mv P1 Archive/`` --
    what tab-completion produces -- moved the folder to ``./Archive`` outside
    the archive and reported a green "Moved". Same class as ``push --remote
    unraid`` writing into ./unraid/. An absolute path, or one spelled with
    ``~``, is a place somebody meant.
    """
    if destination.startswith("~"):
        try:
            return Path(destination).expanduser()
        except RuntimeError as exc:  # ~nosuchuser has no home to expand to
            raise die(f"Cannot expand {destination}: no such user.", code=2) from exc
    literal = Path(destination)
    return literal if literal.is_absolute() else base_path / literal


def resolve_destination(
    source_path: Path,
    destination: str,
    base_path: Path,
    rename: str | None = None,
    date_override: str | None = None,
    fresh_number: bool = False,
) -> Path:
    """Work out the full target path for a move or a copy.

    ``destination`` accepts four forms, in this order of precedence:

    * a category name or letter (``Archive`` / ``A``) -- file under that category
    * a prefix (``P0001``)                            -- file inside that folder
    * a path containing a separator                   -- a literal filesystem path
    * anything else                                   -- a new descriptor, in place
    """
    letter = category_letter_for(destination)
    if letter is not None:
        dest_dir = base_path / CATEGORIES[letter]
        target = dest_dir / name_in_category(source_path.name, letter, base_path, date_override, fresh_number)
    elif normalize_prefix(destination):
        dest_folder = resolve_prefix(destination, base_path)
        if dest_folder is None:
            raise typer.Exit(1)
        target = dest_folder / source_path.name
    elif os.sep in destination or destination.startswith("~"):
        literal = _literal_destination(destination, base_path)
        target = literal / source_path.name if literal.is_dir() else literal
    else:
        # A copy is a new thing wherever it lands, so the descriptor form has to
        # honour fresh_number too. Without it `cp P1 "Alpha v2"` built a second
        # P0001 and then died blaming permanence -- refusing the most natural
        # way to duplicate a folder, and blaming the wrong thing for it.
        name = source_path.name
        if fresh_number and (prefix := folder_prefix(name)):
            name = f"{prefix[0]}{allocate_number(prefix[0], base_path):04d} {folder_descriptor(name) or ''}"
        target = source_path.parent / rename_preserving_prefix(name.strip(), destination)

    if rename:
        target = target.parent / rename_preserving_prefix(target.name, rename)
    return target


def check_not_nested(source_path: Path, target: Path) -> None:
    """Refuse to move a folder into itself."""
    source_resolved = source_path.resolve()
    target_resolved = target.resolve()
    if source_resolved == target_resolved:
        raise die(f"Source and destination are the same: {source_path}")
    if source_resolved in target_resolved.parents:
        raise die(f"Cannot move {source_path.name} inside itself")


def destination_taken(target: Path) -> bool:
    """True when something is already at the destination, symlinks included."""
    return target.exists() or target.is_symlink()


def check_no_overwrite(target: Path, base_path: Path, force: bool, fix: str | None = None) -> None:
    """Refuse to clobber an existing destination unless told to.

    ``fix`` comes from the call site because the escape route differs: mv and
    cp have --force and --rename, rename and oldify have neither, and the
    message used to advertise both to all four.
    """
    if not destination_taken(target):
        return
    if force:
        warn(f"Overwriting: {display(target, base_path)}")
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        return
    raise die(f"Destination already exists: {display(target, base_path)}", fix=fix)


def check_prefix_available(target: Path, base_path: Path, source_path: Path | None) -> None:
    """Refuse a move or copy that would produce two folders with one prefix."""
    if rel(target.parent, base_path) not in CATEGORY_NAMES:
        return

    prefix = folder_prefix(target.name)
    if not prefix:
        warn(f"{target.name} has no GWArchive prefix -- 'verify' will flag it.")
        return

    source_resolved = source_path.resolve() if source_path else None
    for existing in find_folders_by_prefix(prefix, base_path):
        if source_resolved and existing.resolve() == source_resolved:
            continue
        raise die(
            f"{prefix} is already taken by {display(existing, base_path)}\n"
            f"Prefixes are permanent identifiers, so two folders cannot share one.",
            fix=f"{program()} list {prefix[0]}",
        )


def warn_if_taken(target: Path, base_path: Path, force: bool) -> None:
    """The dry-run half of ``check_no_overwrite``, which cannot run under one.

    ``check_no_overwrite`` *deletes* under --force, so a dry run has to skip
    it -- and skipping it made --dry-run predict a success the real run
    refuses. See AGENTS.md, "Conventions".
    """
    if not destination_taken(target):
        return
    shown = display(target, base_path)
    if force:
        note(f"Would overwrite {shown}")
    else:
        warn(f"Destination already exists: {shown} -- the real run would refuse this")


def run_fs(description: str, func: Callable[..., object], *args: str | Path) -> None:
    """Run a filesystem operation, turning OS errors into clean messages."""
    try:
        func(*args)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise die(f"Could not {description}: {reason}") from exc


def locate_source(source: str, base_path: Path) -> Path:
    """Resolve a source argument, which may be a path or a prefix."""
    source_path = Path(source).expanduser()
    if source_path.exists():
        return source_path
    found = resolve_prefix(source, base_path, quiet=True)
    if found:
        return found
    raise die(f"Source not found: {source}", fix=f"{program()} find {source}")
