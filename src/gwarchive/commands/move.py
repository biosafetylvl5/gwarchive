"""Commands that relocate a folder: mv, rename, cp, oldify.

All four share resolve_destination(), which is why they agree about the four
destination forms and about what --force means. cp allocates a FRESH prefix:
a copy is a new thing. The other three carry the prefix along."""

import os
import shutil
from datetime import datetime
from typing import Annotated

import typer

from gwarchive import clock
from gwarchive.destination import (
    check_name,
    check_no_overwrite,
    check_not_nested,
    check_prefix_available,
    is_same_entry,
    locate_source,
    resolve_destination,
    run_fs,
    warn_if_taken,
)
from gwarchive.naming import (
    _reserved_member,
    rename_preserving_prefix,
)
from gwarchive.options import (
    BasePath,
    DryRun,
    Force,
    SourceArg,
    app,
)
from gwarchive.output import (
    die,
    note,
    ok,
    program,
    warn,
)
from gwarchive.paths import (
    display,
    ensure_directory,
    resolve_prefix,
    transfer_message,
)
from gwarchive.tombstone import is_offloaded


def _ignore_reserved(directory: str, names: list[str]) -> list[str]:
    """shutil.copytree's ignore hook: a copy is a new identity, so it must
    not inherit the source's tombstone -- carrying it along is what let
    pushing the copy delete the original's only remote backup.
    """
    return [name for name in names if _reserved_member(name)]


@app.command()
def mv(
    source: SourceArg,
    destination: Annotated[
        str, typer.Argument(metavar="DESTINATION", help="See the four accepted forms below")
    ],
    rename: Annotated[str | None, typer.Option("--rename", help="New descriptor for the moved item")] = None,
    force: Force = False,
    dry_run: DryRun = False,
    *,
    base_path: BasePath,
) -> None:
    """Move files/folders while maintaining the GWArchive structure.

    DESTINATION accepts four forms:

    \b
      Archive  or  A    file the folder under that category
      P0001             move the folder inside P0001
      ./some/path       move to a literal filesystem path
      Some New Name     rename in place (see also: the 'rename' command)

    The prefix is a permanent identifier and does not change when a folder
    moves between categories -- P0001 stays P0001 inside Archive/. Moving into
    Old also date-stamps the name.
    """
    source_path = locate_source(source, base_path)

    target = resolve_destination(source_path, destination, base_path, rename=rename)
    check_not_nested(source_path, target)
    check_prefix_available(target, base_path, source_path)

    if dry_run:
        if not is_same_entry(source_path, target):
            warn_if_taken(target, base_path, force)
        note(transfer_message("Would move", source_path, target, base_path))
        return

    if is_same_entry(source_path, target):
        # A case-only or Unicode-normalisation-only rename: target is already
        # "taken", by the source itself. os.rename is what actually performs
        # that rename on a volume that folds them; check_no_overwrite's
        # --force path would otherwise rmtree the very directory being renamed.
        run_fs(f"rename {source_path.name}", os.rename, source_path, target)
        ok(transfer_message("Moved", source_path, target, base_path))
        return

    check_no_overwrite(
        target, base_path, force, source_path, fix=f"{program()} mv {source} {destination} --force"
    )
    ensure_directory(target.parent)
    run_fs(f"move {source_path.name}", shutil.move, str(source_path), str(target))
    ok(transfer_message("Moved", source_path, target, base_path))


@app.command()
def rename(
    prefix: Annotated[str, typer.Argument(help="Folder prefix (e.g., 'P0001')")],
    name: Annotated[str, typer.Argument(help="New descriptor, without the prefix")],
    dry_run: DryRun = False,
    *,
    base_path: BasePath,
) -> None:
    """Give a folder a new name, keeping its permanent prefix."""
    check_name(name)

    folder = resolve_prefix(prefix, base_path)
    if not folder:
        raise typer.Exit(1)

    target = folder.parent / rename_preserving_prefix(folder.name, name)
    if target == folder:
        note(f"Already named {display(folder, base_path)}")
        return

    if dry_run:
        if not is_same_entry(folder, target):
            warn_if_taken(target, base_path, force=False)
        note(transfer_message("Would rename", folder, target, base_path))
        return

    if is_same_entry(folder, target):
        # A case-only or Unicode-normalisation-only rename: see mv's identical
        # shortcut for why this cannot go through check_no_overwrite.
        run_fs(f"rename {folder.name}", os.rename, folder, target)
        ok(transfer_message("Renamed", folder, target, base_path))
        return

    check_no_overwrite(
        target, base_path, force=False, source_path=folder, fix=f'{program()} rename {prefix} "{name} 2"'
    )
    run_fs(f"rename {folder.name}", folder.rename, target)
    ok(transfer_message("Renamed", folder, target, base_path))


@app.command()
def cp(
    source: SourceArg,
    destination: Annotated[str, typer.Argument(metavar="DESTINATION", help="Same forms as 'mv'")],
    rename: Annotated[str | None, typer.Option("--rename", help="New descriptor for the copied item")] = None,
    link: Annotated[bool, typer.Option("--link", help="Create a symbolic link instead of copying")] = False,
    force: Force = False,
    dry_run: DryRun = False,
    *,
    base_path: BasePath,
) -> None:
    """Copy files/folders while maintaining the GWArchive structure.

    DESTINATION takes the same four forms as 'mv'. One difference: copying into
    a category root allocates a *fresh* prefix, because the copy is a new thing
    and two folders cannot share a permanent identifier.
    """
    source_path = locate_source(source, base_path)

    target = resolve_destination(source_path, destination, base_path, rename=rename, fresh_number=True)
    check_not_nested(source_path, target)
    check_prefix_available(target, base_path, None)

    verb = "link" if link else "copy"
    if dry_run:
        if is_same_entry(source_path, target):
            warn(f"{display(target, base_path)} is {source_path.name} -- the real run would refuse this")
            return
        if not link and source_path.is_dir() and is_offloaded(source_path):
            warn(f"{source_path.name} is offloaded -- the real run would refuse this")
            return
        warn_if_taken(target, base_path, force)
        note(transfer_message(f"Would {verb}", source_path, target, base_path))
        return

    if not link and source_path.is_dir() and is_offloaded(source_path):
        raise die(
            f"{source_path.name} is offloaded -- there is nothing local to copy.",
            fix=f"{program()} restore {source}",
        )

    check_no_overwrite(
        target, base_path, force, source_path, fix=f"{program()} cp {source} {destination} --force"
    )
    ensure_directory(target.parent)

    if link:
        run_fs(f"link {source_path.name}", os.symlink, str(source_path.absolute()), str(target))
        ok(transfer_message("Linked", source_path, target, base_path))
    elif source_path.is_dir():
        run_fs(
            f"copy {source_path.name}",
            shutil.copytree,
            str(source_path),
            str(target),
            ignore=_ignore_reserved,
        )
        ok(transfer_message("Copied", source_path, target, base_path))
    else:
        run_fs(f"copy {source_path.name}", shutil.copy2, str(source_path), str(target))
        ok(transfer_message("Copied", source_path, target, base_path))


@app.command()
def oldify(
    source: Annotated[str, typer.Argument(help="Source folder to move to Old")],
    date: Annotated[str | None, typer.Option("--date", help="Date (YYYY-MM-DD), defaults to today")] = None,
    dry_run: DryRun = False,
    *,
    base_path: BasePath,
) -> None:
    """Move a folder to the Old category with proper date formatting."""

    if date:
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise die(f"Invalid date format: {date}. Use YYYY-MM-DD.", code=2) from exc
        # strptime accepts unpadded dates like "2026-1-5"; normalising keeps
        # the folder name matching OLD_RE, which pads both fields to two
        # digits. Anything else makes the folder invisible to every scan.
        date = parsed.strftime("%Y-%m-%d")
    else:
        date = clock.today()

    source_path = locate_source(source, base_path)
    target = resolve_destination(source_path, "Old", base_path, date_override=date)
    check_not_nested(source_path, target)
    check_prefix_available(target, base_path, source_path)

    if dry_run:
        warn_if_taken(target, base_path, force=False)
        note(transfer_message("Would move", source_path, target, base_path))
        return

    check_no_overwrite(
        target,
        base_path,
        force=False,
        source_path=source_path,
        fix=f"{program()} oldify {source} --date {date}   # a different date",
    )
    ensure_directory(target.parent)
    run_fs(f"move {source_path.name}", shutil.move, str(source_path), str(target))
    ok(transfer_message("Retired", source_path, target, base_path))
