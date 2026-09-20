#!/usr/bin/env python3
"""
gwarchive - A CLI tool for managing files according to the GWArchive standard.

Categories are P/R/M/A/O -> Project/Recurring/Material/Archive/Old.

Installing:
    uv tool install gwarchive       # or: pipx install gwarchive
    gwarchive --help

    Or run the self-contained zipapp, which vendors its dependencies and needs
    no install at all:

        ./g.pyz --help

A prefix (``P0001``) is a permanent identifier. It is allocated once, it is
never reissued, and it travels with the folder across category moves -- so a
folder created in Project keeps its ``P`` prefix even after it is moved into
Archive. A *copy* is a new thing and gets a fresh identifier.

Exit codes:
    0  success
    1  runtime failure -- not found, ambiguous prefix, refused overwrite,
       verification errors, no search matches, or a declined prompt
    2  invalid input -- a bad category or unknown option (both raised by
       Typer), a bad date, or a --remote / --version / $GWARCHIVE_KEEP /
       tombstone value the tool refuses before acting on it
"""

import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.filesize import decimal
from rich.text import Text

# gwarchive.output is imported as a MODULE as well as by name: QUIET is read
# through it so the read happens at call time, not at import time.
from gwarchive import clock, external, output, tarball
from gwarchive.destination import (
    check_no_overwrite,
    check_not_nested,
    check_prefix_available,
    locate_source,
    resolve_destination,
    run_fs,
    warn_if_taken,
)
from gwarchive.external import (
    RCLONE_EXCLUDES,
    get_remotes,
    rclone_or_die,
    remote_folder_target,
    remote_root_of,
    validate_remote_target,
)
from gwarchive.naming import (
    CATEGORIES,
    CATEGORY_METAVAR,
    CATEGORY_NAMES,
    FOLDER_RE,
    OLD_RE,
    SUBFOLDER_RE,
    TOMBSTONE_NAME,
    _reserved_member,
    folder_descriptor,
    folder_prefix,
    matches_pattern,
    rename_preserving_prefix,
)
from gwarchive.options import (
    ArchiveVersion,
    AsJsonReport,
    AsJsonSync,
    AsJsonTable,
    BasePath,
    CategoryArg,
    CategoryOpt,
    DryRun,
    Force,
    Keep,
    NoCompressPull,
    NoCompressPush,
    PullRemote,
    PushRemotes,
    Selector,
    SourceArg,
    Yes,
    app,
    validate_category,
)
from gwarchive.output import (
    INDENT,
    STYLES,
    _decorate,
    caption,
    confirm_destructive,
    console,
    detail,
    die,
    emit_json,
    new_table,
    note,
    ok,
    panel,
    pl,
    plural,
    remote_message,
    spinner,
    symbol,
    warn,
)
from gwarchive.paths import (
    allocate_number,
    allocate_subnumber,
    category_dirs,
    compute_folder_stats,
    display,
    ensure_directory,
    iter_archive_folders,
    require_archive,
    resolve_prefix,
    transfer_message,
)
from gwarchive.sync import (
    carry_remotes,
    compress_default,
    ensure_scratch_space,
    keep_default,
    offload_summary,
    pull_archive,
    resolve_fetch_source,
    resolve_sync_targets,
    stage_archive,
)
from gwarchive.tarball import (
    ARCHIVE_FORMATS,
    archive_meta_of,
    prune_versions,
    record_version,
    remote_archive_object,
    select_version,
    verify_archive,
    would_prune,
)
from gwarchive.tombstone import (
    get_recorded_remotes,
    is_offloaded,
    read_tombstone,
    read_tombstone_state,
    write_tombstone,
)

__version__ = "0.3.0"


# The sprite ``clears`` greets with when neither the positional argument nor
# $GWARCHIVE_POKEMON says otherwise.
DEFAULT_POKEMON = "bulbasaur"


# --- Output --------------------------------------------------------------------
#
# Every helper below renders its message as a rich ``Text``, which means user
# data is never parsed as markup. A folder called "P0001 [draft] Notes" prints
# with the "[draft]" intact instead of having it swallowed as a style tag.
# Style comes from the ``style=`` argument, never from inline tags.


# --- Paths and prefixes --------------------------------------------------------


# --- Destination resolution -- shared by mv, cp, rename and oldify -------------


# --- Remote sync helpers -------------------------------------------------------


# --- Archive layer -------------------------------------------------------------


# --- Parameter validation ------------------------------------------------------


# --- Shared option types -------------------------------------------------------
#
# One declaration per option, however many commands take it -- `--path` was
# written out sixteen times, and three real defects had grown in that drift
# (see AGENTS.md, "Conventions"). An option whose help text genuinely differs
# per command stays inline; a shared alias would flatten it.
#
# BasePath resolves itself, so no command body opens by resolving --path.
# `default_factory` runs at parse time only when the flag is absent, and it
# forbids a `=` default -- hence the bare `*` making it keyword-only below.
# Drop `show_default=False` and every --path line grows `[default: (dynamic)]`.


# --- Commands ------------------------------------------------------------------


@app.command()
def init(*, base_path: BasePath) -> None:
    """Initialize a new GWArchive structure."""

    if not output.QUIET:
        console.print(panel(Text("Initializing GWArchive structure"), title="GWArchive"))

    created = 0
    for category in CATEGORIES.values():
        category_path = base_path / category
        line = Text()
        if ensure_directory(category_path):
            created += 1
            line.append("created  ", style=STYLES["label"])
            line.append(display(category_path, base_path))
            note(line)
        else:
            line.append("exists   ", style=STYLES["label"])
            line.append(display(category_path, base_path))
            detail(line)

    if created:
        ok(f"GWArchive initialized at {base_path}")
    else:
        ok(f"GWArchive already initialized at {base_path}")


@app.command()
def create(
    category: CategoryArg,
    name: Annotated[str, typer.Argument(help="Folder name")],
    *,
    base_path: BasePath,
) -> None:
    """Create a new numbered folder in one of the main categories."""
    # Without this a typo'd --path silently grew a one-category archive
    # somewhere new, and the default is the user's real ~/gwarchive.
    require_archive(base_path)

    category_path = base_path / CATEGORIES[category]
    ensure_directory(category_path)

    next_num = allocate_number(category, base_path)
    new_folder_path = category_path / f"{category}{next_num:04d} {name}"

    ensure_directory(new_folder_path)
    ok(f"Created {display(new_folder_path, base_path)}")


@app.command()
def mksub(
    parent: Annotated[str, typer.Argument(help="Parent folder prefix (e.g., 'P0001')")],
    name: Annotated[str, typer.Argument(help="Subfolder name")],
    *,
    base_path: BasePath,
) -> None:
    """Create a numbered subfolder within an existing folder."""

    parent_folder = resolve_prefix(parent, base_path)
    if not parent_folder:
        raise typer.Exit(1)

    parent_prefix = folder_prefix(parent_folder.name)
    if not parent_prefix:
        raise die(f"Parent folder has no GWArchive prefix: {parent_folder.name}")

    next_sub_num = allocate_subnumber(parent_folder, parent_prefix)
    new_subfolder_path = parent_folder / f"{parent_prefix}.{next_sub_num:02d} {name}"

    ensure_directory(new_subfolder_path)
    ok(f"Created {display(new_subfolder_path, base_path)}")


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
        warn_if_taken(target, base_path, force)
        note(transfer_message("Would move", source_path, target, base_path))
        return

    check_no_overwrite(target, base_path, force, fix=f"g.py mv {source} {destination} --force")
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

    folder = resolve_prefix(prefix, base_path)
    if not folder:
        raise typer.Exit(1)

    target = folder.parent / rename_preserving_prefix(folder.name, name)
    if target == folder:
        note(f"Already named {display(folder, base_path)}")
        return

    if dry_run:
        warn_if_taken(target, base_path, force=False)
        note(transfer_message("Would rename", folder, target, base_path))
        return

    check_no_overwrite(target, base_path, force=False, fix=f'g.py rename {prefix} "{name} 2"')
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
        warn_if_taken(target, base_path, force)
        note(transfer_message(f"Would {verb}", source_path, target, base_path))
        return

    check_no_overwrite(target, base_path, force, fix=f"g.py cp {source} {destination} --force")
    ensure_directory(target.parent)

    if link:
        run_fs(f"link {source_path.name}", os.symlink, str(source_path), str(target))
        ok(transfer_message("Linked", source_path, target, base_path))
    elif source_path.is_dir():
        run_fs(f"copy {source_path.name}", shutil.copytree, str(source_path), str(target))
        ok(transfer_message("Copied", source_path, target, base_path))
    else:
        run_fs(f"copy {source_path.name}", shutil.copy2, str(source_path), str(target))
        ok(transfer_message("Copied", source_path, target, base_path))


@app.command("list")
def list_folders(
    category: Annotated[
        str,
        typer.Argument(
            callback=validate_category, metavar=CATEGORY_METAVAR, help="Category to list (P, R, M, A, O)"
        ),
    ],
    as_json: AsJsonTable = False,
    *,
    base_path: BasePath,
) -> None:
    """List folders in a category with their prefixes and descriptions."""
    category_name = CATEGORIES[category]
    category_path = base_path / category_name

    if not category_path.exists():
        # --json still exits 1, the way `find --json` does: adding --json must
        # not stop a script noticing that there is no archive here.
        if as_json:
            emit_json([])
            raise typer.Exit(1)
        raise die(f"No {category_name} directory at {base_path}", fix=f"g.py init --path {base_path}")

    rows: list[dict[str, object]] = []
    for _, item in iter_archive_folders(base_path, category_name):
        row: dict[str, object] = {
            "prefix": folder_prefix(item.name) or "",
            "name": folder_descriptor(item.name) or "",
            "path": display(item, base_path),
            "modified": datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d"),
        }
        if is_offloaded(item):
            row["offloaded"] = True
        rows.append(row)
    rows.sort(key=lambda r: (str(r["prefix"]), str(r["name"])))

    if as_json:
        emit_json(rows)
        return

    if not rows:
        note(f'No folders in {category_name} yet -- create one with:  g.py create {category} "Name"')
        return

    table = new_table(f"{category_name} Folders")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="green")
    table.add_column("Modified", style="yellow", no_wrap=True)
    # Status sits in its own column so tool state never mingles with a
    # user-chosen name like "Alpha [draft]".
    table.add_column("Status", style="yellow", no_wrap=True)
    for row in rows:
        status = Text("offloaded", style="dim yellow") if row.get("offloaded") else Text("")
        table.add_row(Text(str(row["prefix"])), Text(str(row["name"])), Text(str(row["modified"])), status)
    console.print(table)


@app.command("find")
def find_folders(
    pattern: Annotated[str, typer.Argument(help="Search pattern")],
    category: CategoryOpt = None,
    recursive: Annotated[bool, typer.Option("--recursive", "-r", help="Search inside folders too")] = False,
    depth: Annotated[
        int | None, typer.Option("--depth", min=1, help="Limit recursion depth (implies --recursive)")
    ] = None,
    exact: Annotated[
        bool, typer.Option("--exact", help="Match the whole name exactly, case-sensitively")
    ] = False,
    files: Annotated[bool, typer.Option("--files", help="Match files as well as directories")] = False,
    as_json: AsJsonTable = False,
    *,
    base_path: BasePath,
) -> None:
    """Find folders by name or number.

    Exits 1 when nothing matches, so it composes like grep.
    """
    require_archive(base_path)

    if depth is not None:
        recursive = True

    searched = [CATEGORIES[category]] if category else list(CATEGORIES.values())

    hits: list[tuple[str, Path]] = []
    for cat in searched:
        cat_path = base_path / cat
        if not cat_path.exists():
            continue
        for item in sorted(cat_path.rglob("*"), key=lambda p: str(p)):
            if item.is_dir() is False and not files:
                continue
            relative_depth = len(item.relative_to(cat_path).parts)
            if not recursive and relative_depth > 1:
                continue
            if depth is not None and relative_depth > depth:
                continue
            if matches_pattern(item.name, pattern, exact):
                hits.append((cat, item))

    # A folder and its own children matching the same word is one finding, not
    # several. Keep the outermost and count the rest.
    kept: list[tuple[str, Path]] = []
    collapsed = 0
    matched_paths = {item for _, item in hits}
    for cat, item in hits:
        if any(parent in matched_paths for parent in item.parents):
            collapsed += 1
            continue
        kept.append((cat, item))

    kept.sort(key=lambda pair: (pair[0], folder_prefix(pair[1].name) or "", str(pair[1])))

    if as_json:
        emit_json(
            [
                {
                    "category": cat,
                    "prefix": folder_prefix(item.name),
                    "name": item.name,
                    "path": display(item, base_path),
                    "modified": datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d"),
                }
                for cat, item in kept
            ]
        )
        if not kept:
            raise typer.Exit(1)
        return

    if not kept:
        warn(f"No matches for: {pattern}")
        raise typer.Exit(1)

    table = new_table(Text(f"Search Results for {pattern!r}"))
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Category", style="cyan", no_wrap=True)
    table.add_column("Path", style="green")
    table.add_column("Modified", style="yellow", no_wrap=True)

    for cat, item in kept:
        cell = Text(display(item, base_path))
        cell.highlight_regex(re.escape(pattern), style=f"bold {STYLES['accent']}")
        modified = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d")
        table.add_row(Text(folder_prefix(item.name) or "-"), Text(cat), cell, Text(modified))

    console.print(table)
    summary = f"{len(kept)} match{'' if len(kept) == 1 else 'es'}"
    if collapsed:
        summary += f" ({collapsed} nested match{'' if collapsed == 1 else 'es'} collapsed)"
    caption(summary)


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
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise die(f"Invalid date format: {date}. Use YYYY-MM-DD.", code=2) from exc
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
        target, base_path, force=False, fix=f"g.py oldify {source} --date {date}   # a different date"
    )
    ensure_directory(target.parent)
    run_fs(f"move {source_path.name}", shutil.move, str(source_path), str(target))
    ok(transfer_message("Retired", source_path, target, base_path))


# --- Remote sync commands ------------------------------------------------------


@app.command()
def push(
    selector: Selector,
    remote: PushRemotes = None,
    no_compress: NoCompressPush = False,
    keep: Keep = None,
    dry_run: DryRun = False,
    as_json: AsJsonSync = False,
    *,
    base_path: BasePath,
) -> None:
    """Push folder(s) to remote storage without deleting local copies."""
    remotes = get_remotes(remote, "push")
    targets = resolve_sync_targets(selector, base_path, "push")
    # `codec` doubles as the compress flag: pick_codec never returns "".
    codec = tarball.pick_codec() if compress_default() and not no_compress else ""
    retain = keep_default(keep)

    results: list[dict[str, object]] = []

    for cat_name, folder in targets:
        if is_offloaded(folder):
            # A tombstone has no local bytes to back up. Pushing it would also
            # overwrite the recorded size/file_count with zeros.
            if not as_json:
                caption(f"Skipped {display(folder, base_path)} -- offloaded, already on the remote")
            results.append(
                {
                    "folder": display(folder, base_path),
                    "prefix": folder_prefix(folder.name),
                    "category": cat_name,
                    "skipped": "offloaded",
                    "dry_run": dry_run,
                }
            )
            continue

        pfx = folder_prefix(folder.name) or ""
        desc = folder_descriptor(folder.name) or ""
        size, file_count = compute_folder_stats(folder)
        existing_meta = read_tombstone(folder) or {}
        was_loose = not isinstance(existing_meta.get("archive"), dict)

        prev_remotes = get_recorded_remotes(existing_meta)
        all_remotes = carry_remotes(prev_remotes, folder, base_path)

        meta: dict[str, object] = {
            **existing_meta,
            "prefix": pfx,
            "descriptor": desc,
            "category": cat_name,
            "remotes": all_remotes,
            "size": size,
            "file_count": file_count,
            "last_pushed_at": clock.now_stamp(),
            "tool_version": __version__,
        }
        # A --no-compress push must clear any archive block: leave it and the
        # next restore fetches an object this push never refreshed, silently
        # restoring stale content.
        if not codec:
            meta.pop("archive", None)

        dests = []
        pruned = 0

        with tempfile.TemporaryDirectory(prefix="gwarchive-") as tmp:
            staged: Path | None = None
            if codec and not dry_run:
                staged, entry = stage_archive(folder, Path(tmp), codec, as_json=as_json)
                meta["archive"] = record_version(existing_meta, entry, codec, retain)

            for rem in remotes:
                dest = remote_folder_target(rem, cat_name, folder.name)
                dests.append(dest)
                if not dry_run:
                    if staged is not None:
                        args = ["copyto", str(staged), remote_archive_object(dest, staged.name)]
                    else:
                        args = ["copy", str(folder), dest, *RCLONE_EXCLUDES]
                    rclone_or_die(
                        args,
                        f"push {folder.name} to {dest}",
                        status=None if as_json else f"Pushing {folder.name} {symbol('transfer')} {rem}",
                        fix=f"rclone lsd {rem}",
                    )
                if dest not in all_remotes:
                    all_remotes.append(dest)
                if not dry_run:
                    # Record each successful copy immediately so a later
                    # remote's failure doesn't lose the record of this one.
                    # Reassigned rather than relied on: `meta["remotes"]` and
                    # `all_remotes` were the same list object, so this write
                    # only persisted the append by accident.
                    meta["remotes"] = all_remotes
                    write_tombstone(folder, meta)

        if dry_run:
            pruned = would_prune(existing_meta, retain) if codec else 0
        else:
            # Pruned once every remote holds the new object: a failure above
            # raises before this line, so nothing old is removed until the new
            # copy exists everywhere it was asked to go.
            arch = meta.get("archive")
            if codec and isinstance(arch, dict):
                pruned = prune_versions(arch, all_remotes, retain)

        if dry_run:
            if not as_json:
                note(remote_message("Would push", display(folder, base_path), dests))
                if codec:
                    caption(f"Would compress {file_count} file(s) into one {codec} object")
                if pruned:
                    caption(f"Would remove {plural(pruned, 'older copy', 'older copies')}")
        else:
            write_tombstone(folder, meta)
            if not as_json:
                ok(remote_message("Pushed", display(folder, base_path), dests))
                if codec and was_loose and prev_remotes:
                    caption(
                        f"The pre-archive per-file copy is still on the remote under "
                        f"{cat_name}/{folder.name}/ -- rclone purge it to reclaim the space"
                    )
                if pruned:
                    caption(f"Removed {plural(pruned, 'older copy', 'older copies')}")

        results.append(
            {
                "folder": display(folder, base_path),
                "prefix": pfx,
                "category": cat_name,
                "size": size,
                "file_count": file_count,
                "remotes": all_remotes,
                "archive": meta.get("archive"),
                "pruned": pruned,
                "dry_run": dry_run,
            }
        )

    if as_json:
        emit_json(results)


@app.command()
def offload(
    selector: Selector,
    remote: PushRemotes = None,
    no_compress: NoCompressPush = False,
    keep: Keep = None,
    yes: Yes = False,
    dry_run: DryRun = False,
    as_json: AsJsonSync = False,
    *,
    base_path: BasePath,
) -> None:
    """Offload folder(s) to remote storage, deleting local files and leaving a tombstone."""
    remotes = get_remotes(remote, "offload")
    targets = resolve_sync_targets(selector, base_path, "offload")
    # `codec` doubles as the compress flag: pick_codec never returns "".
    codec = tarball.pick_codec() if compress_default() and not no_compress else ""
    retain = keep_default(keep)

    # A whole-category offload skips folders that are already parked, so an
    # interrupted batch can be re-run. Naming one folder explicitly still fails
    # loudly -- the user asked for something that cannot happen.
    already = [(cat, folder) for cat, folder in targets if is_offloaded(folder)]
    if already:
        if len(targets) == 1 and not dry_run:
            raise die(
                f"{display(targets[0][1], base_path)} is already offloaded.",
                fix=f"g.py restore {folder_prefix(targets[0][1].name)}",
            )
        for _, folder in already:
            if not as_json:
                caption(f"Skipped {display(folder, base_path)} -- already offloaded")
        targets = [(cat, folder) for cat, folder in targets if not is_offloaded(folder)]
        if not targets:
            if as_json:
                emit_json([])
            else:
                ok("Nothing to offload -- every folder is already offloaded.")
            return

    # Measured once: the prompt needs the sizes to say how much disk this
    # returns, and the loop needs them for the tombstone.
    measured: list[tuple[str, Path, int, int]] = []
    for cat_name, folder in targets:
        size, file_count = compute_folder_stats(folder)
        measured.append((cat_name, folder, size, file_count))

    if codec and not dry_run:
        # Folders are staged one at a time, so the largest of them is the
        # requirement. Checked before the prompt: the scratch cost is part of
        # what the user is agreeing to.
        ensure_scratch_space(
            max((size for _, _, size, _ in measured), default=0), Path(tempfile.gettempdir())
        )

    if not dry_run and not confirm_destructive(
        offload_summary(measured, remotes, base_path, codec), "Proceed with offload?", yes=yes
    ):
        raise die("Offload cancelled")

    results: list[dict[str, object]] = []

    for cat_name, folder, size, file_count in measured:
        pfx = folder_prefix(folder.name) or ""
        desc = folder_descriptor(folder.name) or ""
        existing_meta = read_tombstone(folder) or {}

        prev_remotes = get_recorded_remotes(existing_meta)
        all_remotes = carry_remotes(prev_remotes, folder, base_path)

        meta: dict[str, object] = {
            **existing_meta,
            "prefix": pfx,
            "descriptor": desc,
            "category": cat_name,
            "remotes": all_remotes,
            "size": size,
            "file_count": file_count,
            "tool_version": __version__,
        }
        if not codec:
            meta.pop("archive", None)

        dests = []
        pruned = 0

        with tempfile.TemporaryDirectory(prefix="gwarchive-") as tmp:
            staged: Path | None = None
            if codec and not dry_run:
                staged, entry = stage_archive(folder, Path(tmp), codec, as_json=as_json)
                # Read the archive back before a single local byte is deleted.
                # This is the one command that destroys the originals, so a
                # silently truncated archive would be data loss; push keeps the
                # local copy and skips the pass.
                verify_archive(staged, codec, int(str(entry["member_count"])))
                meta["archive"] = record_version(existing_meta, entry, codec, retain)

            for rem in remotes:
                dest = remote_folder_target(rem, cat_name, folder.name)
                dests.append(dest)
                if not dry_run:
                    if staged is not None:
                        args = ["copyto", str(staged), remote_archive_object(dest, staged.name)]
                    else:
                        args = ["copy", str(folder), dest, *RCLONE_EXCLUDES]
                    rclone_or_die(
                        args,
                        f"offload {folder.name} to {dest}",
                        status=None if as_json else f"Offloading {folder.name} {symbol('transfer')} {rem}",
                        fix=f"rclone lsd {rem}",
                    )
                if dest not in all_remotes:
                    all_remotes.append(dest)
                if not dry_run:
                    # Record each successful copy immediately: if a later remote
                    # fails, the tombstone still knows where the data already is.
                    meta["remotes"] = all_remotes
                    write_tombstone(folder, meta)

            if dry_run:
                pruned = would_prune(existing_meta, retain) if codec else 0
            else:
                # Pruned once every remote holds the new object, and before the
                # local files go: a failure above raises first, so nothing old
                # is removed until the new copy exists everywhere.
                arch = meta.get("archive")
                if codec and isinstance(arch, dict):
                    pruned = prune_versions(arch, all_remotes, retain)

            if dry_run:
                if not as_json:
                    note(
                        remote_message(
                            "Would offload (and delete local files)", display(folder, base_path), dests
                        )
                    )
                    if codec:
                        caption(f"Would compress {file_count} file(s) into one {codec} object")
                    if pruned:
                        caption(f"Would remove {plural(pruned, 'older copy', 'older copies')}")
            else:
                # Mark offloaded *before* deleting: if deletion is interrupted,
                # the folder reads as offloaded-with-leftovers (a verify notice,
                # and restore still works) rather than as a silently emptied live
                # folder that nothing knows is safe on the remote.
                meta["offloaded_at"] = clock.now_stamp()
                write_tombstone(folder, meta)

                for child in folder.iterdir():
                    # The same rule that decided what was packed decides what
                    # may go. Anything reserved was never in the archive, so
                    # deleting it here would be deleting the only copy.
                    if _reserved_member(child.name):
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()

                if not as_json:
                    ok(remote_message("Offloaded (local files deleted)", display(folder, base_path), dests))
                    if pruned:
                        caption(f"Removed {plural(pruned, 'older copy', 'older copies')}")

        results.append(
            {
                "folder": display(folder, base_path),
                "prefix": pfx,
                "category": cat_name,
                "size": size,
                "file_count": file_count,
                "remotes": all_remotes,
                "archive": meta.get("archive"),
                "pruned": pruned,
                "offloaded": not dry_run,
                "dry_run": dry_run,
            }
        )

    if as_json:
        emit_json(results)


def fetch_folders(
    selector: str,
    base_path: Path,
    *,
    command: str,
    gerund: str,
    past: str,
    remote: str | None,
    version: str | None,
    no_compress: bool,
    dry_run: bool,
    as_json: bool,
    clear_offloaded: bool = False,
) -> None:
    """The body of both pull and restore.

    They differ in three strings and one flag. Four things only one of them
    did, and in three cases it was the wrong one -- see AGENTS.md, "Commands".
    """
    targets = resolve_sync_targets(selector, base_path, command)
    if remote:
        remote = validate_remote_target(remote, command=command)

    results: list[dict[str, object]] = []

    for cat_name, folder in targets:
        shown = display(folder, base_path)
        existing_meta = read_tombstone(folder) or {}
        offloaded = bool(existing_meta.get("offloaded_at"))
        prev_remotes = get_recorded_remotes(existing_meta)
        arch = None if no_compress else archive_meta_of(existing_meta)

        if version and not arch:
            # Silently ignoring it meant `--no-compress --version 3` did a
            # plain mirror copy and exited 0 reporting success.
            raise die(
                f"--version needs a recorded archive; {shown} has none.",
                code=2,
                fix=f"g.py {command} {folder_prefix(folder.name) or selector}",
            )

        if clear_offloaded and not offloaded:
            # A category restore only touches parked folders; copying remote
            # content over a live one is additive but surprising. Naming a
            # single folder explicitly still runs.
            if len(targets) > 1:
                if not as_json:
                    caption(f"Skipped {shown} -- not offloaded")
                results.append({"folder": shown, "category": cat_name, "skipped": "not-offloaded"})
                continue
            if not prev_remotes and not remote:
                warn(f"{shown} does not appear to be offloaded.")

        src = resolve_fetch_source(remote, prev_remotes, cat_name, folder.name, command)
        # Named before the branch so --json reports it on a dry run too.
        wanted = select_version(arch, version).get("name") if arch else None

        fetched: str | None = None
        if dry_run:
            if not as_json:
                note(remote_message(f"Would {command}", src, [shown]))
                if wanted:
                    caption(f"Would unpack {wanted}")
        else:
            if arch:
                # Extraction rewrites every member it carries, where rclone copy
                # skips files it considers unchanged. So a fetch over local
                # edits clobbers all of them, not just the ones the remote
                # calls newer. Self-guarding, so it is a no-op on a tombstone.
                if not offloaded and compute_folder_stats(folder)[1]:
                    warn(f"{shown} still holds local files; unpacking overwrites any the archive contains")
                fetched = pull_archive(
                    src, folder, arch, version, verb=command, gerund=gerund, as_json=as_json
                )
            else:
                rclone_or_die(
                    ["copy", src, str(folder), *RCLONE_EXCLUDES],
                    f"{command} {folder.name} from {src}",
                    status=None if as_json else f"{gerund} {folder.name} {symbol('transfer')} local",
                    fix=f"rclone lsd {remote_root_of(src)}",
                )
            if clear_offloaded and offloaded:
                meta = dict(existing_meta)
                meta.pop("offloaded_at", None)
                meta["restored_at"] = clock.now_stamp()
                write_tombstone(folder, meta)
            if not as_json:
                ok(remote_message(past, f"{src} ({fetched})" if fetched else src, [shown]))

        entry: dict[str, object] = {
            "folder": shown,
            "prefix": folder_prefix(folder.name),
            "category": cat_name,
            "source": src,
            "archive": fetched or wanted if dry_run else fetched,
            "dry_run": dry_run,
        }
        if clear_offloaded:
            entry["restored"] = not dry_run
        results.append(entry)

    if as_json:
        emit_json(results)


@app.command()
def pull(
    selector: Selector,
    remote: PullRemote = None,
    version: ArchiveVersion = None,
    no_compress: NoCompressPull = False,
    dry_run: DryRun = False,
    as_json: AsJsonSync = False,
    *,
    base_path: BasePath,
) -> None:
    """Pull folder(s) from remote storage back to local (without clearing tombstone status)."""
    fetch_folders(
        selector,
        base_path,
        command="pull",
        gerund="Pulling",
        past="Pulled",
        remote=remote,
        version=version,
        no_compress=no_compress,
        dry_run=dry_run,
        as_json=as_json,
    )


@app.command()
def restore(
    selector: Selector,
    remote: PullRemote = None,
    version: ArchiveVersion = None,
    no_compress: NoCompressPull = False,
    dry_run: DryRun = False,
    as_json: AsJsonSync = False,
    *,
    base_path: BasePath,
) -> None:
    """Restore an offloaded folder from remote storage, clearing tombstone status."""
    fetch_folders(
        selector,
        base_path,
        command="restore",
        gerund="Restoring",
        past="Restored",
        remote=remote,
        version=version,
        no_compress=no_compress,
        dry_run=dry_run,
        as_json=as_json,
        clear_offloaded=True,
    )


@app.command()
def stats(
    detailed: Annotated[bool, typer.Option("--detail", help="Show recent and largest folders")] = False,
    recent: Annotated[int, typer.Option("--recent", help="How many rows each --detail list shows")] = 5,
    category: CategoryOpt = None,
    as_json: AsJsonTable = False,
    *,
    base_path: BasePath,
) -> None:
    """Show statistics about the GWArchive system."""
    require_archive(base_path)

    analyzed = [CATEGORIES[category]] if category else list(CATEGORIES.values())

    # "offloaded" is initialised here, not filled in inside the loop: it used
    # to be absent from --json entirely whenever a category directory was
    # missing, so a fixed-shape payload had an optional key.
    totals = {"folders": 0, "files": 0, "size": 0, "offloaded": 0}
    per_category: dict[str, dict[str, int]] = {}
    per_folder: list[tuple[int, str, str]] = []
    recent_items: list[tuple[float, str, str]] = []
    unreadable = 0

    with spinner("Walking the archive...", as_json=as_json) as status:
        for cat in analyzed:
            cat_path = base_path / cat
            if status:
                status.update(Text(f"Walking {cat}..."))

            # One pass. This used to be three -- a recursive glob for the
            # sizes, then iterdir for the mtimes, then iterdir again for the
            # offloaded count, each rediscovering what the first already knew.
            # rglob on a missing directory yields nothing, so the category that
            # does not exist needs no branch of its own.
            folders = files = size = offloaded = 0
            folder_sizes: dict[str, int] = {}
            for item in cat_path.rglob("*"):
                parts = item.relative_to(cat_path).parts
                if any(_reserved_member(part) for part in parts):
                    continue
                if item.is_dir():
                    folders += 1
                    if len(parts) == 1:
                        if folder_prefix(item.name):
                            recent_items.append((item.stat().st_mtime, cat, item.name))
                        if is_offloaded(item):
                            offloaded += 1
                    continue
                try:
                    item_size = item.stat().st_size
                except OSError:
                    unreadable += 1
                    continue
                files += 1
                size += item_size
                folder_sizes[parts[0]] = folder_sizes.get(parts[0], 0) + item_size

            per_category[cat] = {"folders": folders, "files": files, "size": size}
            per_folder.extend((folder_size, cat, name) for name, folder_size in folder_sizes.items())
            totals["folders"] += folders
            totals["files"] += files
            totals["size"] += size
            totals["offloaded"] += offloaded

    if as_json:
        emit_json(
            {
                "categories": {
                    cat: {**values, "size_human": decimal(values["size"])}
                    for cat, values in per_category.items()
                },
                "total": {**totals, "size_human": decimal(totals["size"])},
            }
        )
        return

    table = new_table("GWArchive Statistics")
    table.add_column("Category", style="cyan")
    table.add_column("Folders", style="green", justify="right")
    table.add_column("Files", style="yellow", justify="right")
    table.add_column("Size", style=STYLES["accent"], justify="right")

    for cat, values in per_category.items():
        # An empty category shouldn't carry the same weight as a full one.
        table.add_row(
            Text(cat),
            Text(str(values["folders"])),
            Text(str(values["files"])),
            Text(decimal(values["size"])),
            style=None if values["folders"] or values["files"] else "dim",
        )
    table.add_row(
        Text("TOTAL"),
        Text(str(totals["folders"])),
        Text(str(totals["files"])),
        Text(decimal(totals["size"])),
        style="bold",
    )
    console.print(table)
    if totals["offloaded"]:
        caption(f"{plural(totals['offloaded'], 'folder')} offloaded to remote storage")
    if unreadable:
        # A silently short total is worse than an ugly one.
        warn(f"{plural(unreadable, 'file')} could not be read and {pl(unreadable, 'is', 'are')} not counted")

    if not detailed:
        return

    if recent_items:
        console.print()
        console.print(Text("Recently modified", style="bold"))
        for modified, cat, name in sorted(recent_items, reverse=True)[:recent]:
            stamp = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
            line = Text(f"{INDENT}{stamp}  ")
            line.append(f"{cat}/", style="cyan")
            line.append(name, style="green")
            console.print(line, soft_wrap=True)

    if per_folder:
        console.print()
        console.print(Text("Largest folders", style="bold"))
        for folder_size, cat, name in sorted(per_folder, reverse=True)[:recent]:
            line = Text(f"{INDENT}{decimal(folder_size):>10}  ")
            line.append(f"{cat}/", style="cyan")
            line.append(name, style="green")
            console.print(line, soft_wrap=True)


# --- verify --------------------------------------------------------------------

ERROR = "error"
NOTICE = "notice"

# The remedy a finding names when the command can apply it itself.
FIX_HINT = "fixable with --fix"
QUARANTINE_HINT = "movable with --quarantine"


@dataclass
class Issue:
    """One verification finding.

    ``severity`` defaults to ERROR because nearly every finding is one, and the
    two notices say so explicitly. That default is what lets a collector yield
    a finding on one line instead of the seven the formatter needed when every
    call site had to spell ERROR out.

    ``target`` and ``action`` are how a finding tells --fix and --quarantine
    what to operate on, rather than having the path recovered by string-parsing
    ``message``. See AGENTS.md, "verify".
    """

    group: str
    message: str
    fix_hint: str = ""
    severity: str = ERROR
    resolution: str = ""
    target: Path | None = None
    action: str = ""

    def as_dict(self) -> dict[str, str]:
        """The --json and --report view: the text fields, never the Path.

        Spelled out rather than ``dataclasses.asdict``, which would put a
        PosixPath into ``emit_json`` and make json.dumps raise.
        """
        return {
            "severity": self.severity,
            "group": self.group,
            "message": self.message,
            "fix_hint": self.fix_hint,
            "resolution": self.resolution,
        }

    def line(self) -> Text:
        """One finding, symbol first, with its resolution or its remedy dimmed."""
        kind = "ok" if self.resolution else ("error" if self.severity == ERROR else "warn")
        body = Text(self.message)
        if self.resolution:
            body.append(f"  [{self.resolution}]", style=STYLES["caption"])
        elif self.fix_hint:
            body.append(f"  ({self.fix_hint})", style=STYLES["caption"])
        return _decorate(kind, body, indent=INDENT)


def collect_structure_issues(base_path: Path) -> Iterator[Issue]:
    """The archive root and its five category directories.

    No early return when the base is missing -- the categories are missing too,
    and reporting only the base made ``--fix`` exit 0 having created one
    directory. See AGENTS.md, "verify".
    """
    if not base_path.exists():
        yield Issue(
            "Structure",
            f"Base directory does not exist: {base_path}",
            FIX_HINT,
            target=base_path,
            action="create",
        )
    for category in CATEGORIES.values():
        cat_path = base_path / category
        if not cat_path.exists():
            yield Issue(
                "Structure",
                f"Missing category directory: {category}",
                FIX_HINT,
                target=cat_path,
                action="create",
            )


def collect_root_issues(base_path: Path) -> Iterator[Issue]:
    """Unrecognized entries at the archive root; the path rides on ``target``."""
    if not base_path.exists():
        return

    allowed_dirs = CATEGORY_NAMES | {"BACKUP"}
    allowed_files = {".gitignore", "README.md"}

    for item in sorted(base_path.iterdir(), key=lambda p: p.name):
        if item.name.startswith("."):
            continue
        is_dir = item.is_dir()
        if not (is_dir or item.is_file()):
            continue  # a broken symlink or a FIFO: skipped before, skipped now
        if item.name in (allowed_dirs if is_dir else allowed_files):
            continue
        yield Issue(
            "Root contents",
            f"Unrecognized {'directory' if is_dir else 'file'} in root: {item.name}",
            QUARANTINE_HINT,
            target=item,
            action="quarantine",
        )


def collect_naming_issues(base_path: Path) -> Iterator[Issue]:
    """Top-level folder names, per category.

    Deliberately not built on ``iter_archive_folders``: that helper keeps only
    folders that already have a valid prefix, which is exactly what this
    collector exists to report the absence of. It needs the category letter too.
    """
    for letter, category in CATEGORIES.items():
        cat_path = base_path / category
        if not cat_path.exists():
            continue
        for item in sorted(cat_path.iterdir(), key=lambda p: p.name):
            if not item.is_dir() or item.name == "BACKUP":
                continue
            if letter == "O":
                if not OLD_RE.match(item.name):
                    yield Issue("Naming", f"Old/{item.name} is not named YYYY-MM-DD-P0000-Descriptor")
                continue
            if not FOLDER_RE.match(item.name):
                yield Issue("Naming", f"{category}/{item.name} has no valid prefix")
                continue
            prefix = folder_prefix(item.name) or ""
            if prefix[0] != letter:
                # Legal: prefixes are permanent and survive a category move.
                # Worth surfacing so a misfile is still visible.
                yield Issue(
                    "Naming",
                    f"{category}/{item.name} was created in {CATEGORIES[prefix[0]]}",
                    severity=NOTICE,
                )


def _subfolder_issues(parent: Path, parent_prefix: str) -> Iterator[Issue]:
    """The numbered children of one folder. Unnumbered ones are allowed."""
    seen: dict[str, str] = {}
    for child in sorted(parent.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or "." not in child.name:
            continue
        match = SUBFOLDER_RE.match(child.name)
        if not match:
            if FOLDER_RE.match(child.name.split(".")[0] + " x"):
                yield Issue("Subfolders", f"{parent.name}/{child.name} is not named {parent_prefix}.NN Name")
            continue
        if match.group(1) != parent_prefix:
            yield Issue("Subfolders", f"{parent.name}/{child.name} carries another folder's prefix")
            continue
        number = match.group(2)
        if number in seen:
            yield Issue(
                "Subfolders",
                f"{parent.name}/ has two .{number} subfolders: {seen[number]} and {child.name}",
            )
        seen[number] = child.name


def collect_subfolder_issues(base_path: Path) -> Iterator[Issue]:
    """The P0001.01 convention that mksub enforces but verify never did."""
    for category, parent in iter_archive_folders(base_path):
        if category == CATEGORIES["O"]:
            continue
        yield from _subfolder_issues(parent, folder_prefix(parent.name) or "")


def collect_uniqueness_issues(base_path: Path) -> Iterator[Issue]:
    """A prefix is a permanent identifier, so exactly one folder may hold it."""
    by_prefix: dict[str, list[str]] = {}
    for _, item in iter_archive_folders(base_path):
        by_prefix.setdefault(folder_prefix(item.name) or "", []).append(display(item, base_path))

    for prefix, holders in sorted(by_prefix.items()):
        if len(holders) > 1:
            yield Issue(
                "Uniqueness",
                f"{prefix} is claimed by {len(holders)} folders: " + ", ".join(sorted(holders)),
                "not autofixable -- decide which one keeps the number",
            )


def collect_tombstone_issues(base_path: Path) -> Iterator[Issue]:
    for _, cat_path in category_dirs(base_path):
        for folder in sorted(cat_path.iterdir(), key=lambda p: p.name):
            if not folder.is_dir():
                continue
            state, data = read_tombstone_state(folder)
            if state == "absent":
                continue
            shown = display(folder, base_path)
            if state == "corrupt":
                yield Issue("Offload", f"Corrupted offload metadata in {shown}: {TOMBSTONE_NAME}")
                continue
            if data is None:
                yield Issue("Offload", f"Invalid metadata format in {shown}: expected JSON object")
                continue

            arch = data.get("archive")
            if arch is not None and not isinstance(arch, dict):
                yield Issue("Offload", f"Invalid archive block in {shown}: expected JSON object")
            elif isinstance(arch, dict):
                if arch.get("format") not in ARCHIVE_FORMATS:
                    # Reported here rather than left for restore, which would
                    # only discover it at extraction time with a worse message.
                    yield Issue(
                        "Offload",
                        f"{shown} records an unreadable archive format: {arch.get('format')!r}",
                    )
                if not isinstance(arch.get("versions"), list):
                    # The next push would rebuild `versions` from scratch, and
                    # pruning only ever deletes recorded names -- so every
                    # object the old list named becomes unreclaimable.
                    yield Issue("Offload", f"{shown} records an archive with no readable version list")

            if data.get("offloaded_at"):
                left = [p for p in folder.iterdir() if not _reserved_member(p.name)]
                if left:
                    yield Issue(
                        "Offload",
                        f"{shown} is marked offloaded but has {plural(len(left), 'file')}"
                        " (offload interrupted, or contents pulled back for local reading)",
                        severity=NOTICE,
                    )


def split_issues(issues: Sequence[Issue]) -> tuple[list[Issue], list[Issue]]:
    """Unresolved errors and unresolved notices. A resolved finding is neither."""
    live = [issue for issue in issues if not issue.resolution]
    return (
        [issue for issue in live if issue.severity == ERROR],
        [issue for issue in live if issue.severity == NOTICE],
    )


@app.command()
def verify(
    fix: Annotated[
        bool, typer.Option("--fix", help="Create missing category directories (safe, idempotent)")
    ] = False,
    quarantine: Annotated[
        bool, typer.Option("--quarantine", help="Also MOVE unrecognized root entries into BACKUP/<date>/")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt for --quarantine")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what --fix/--quarantine would do")] = False,
    as_json: AsJsonReport = False,
    report: Annotated[Path | None, typer.Option("--report", help="Save report to file")] = None,
    *,
    base_path: BasePath,
) -> None:
    """Verify that the structure follows GWArchive standards.

    Exits 1 when errors remain, so it can gate a script. Notices do not fail.

    --fix only creates missing category directories. Relocating unrecognized
    files is a separate, confirmed opt-in via --quarantine.
    """

    issues: list[Issue] = [
        *collect_structure_issues(base_path),
        *collect_root_issues(base_path),
        *collect_naming_issues(base_path),
        *collect_subfolder_issues(base_path),
        *collect_uniqueness_issues(base_path),
        *collect_tombstone_issues(base_path),
    ]

    if fix:
        for issue in issues:
            if issue.action != "create" or issue.target is None:
                continue
            if not dry_run:
                ensure_directory(issue.target)
            issue.resolution = "would create" if dry_run else "created"

    strays = [(i, i.target) for i in issues if i.action == "quarantine" and i.target is not None]
    if quarantine and strays:
        listing = Text(
            f"Move {plural(len(strays), 'unrecognized entry', 'unrecognized entries')} into BACKUP/\n\n",
            style="bold",
        )
        for _, item in strays:
            listing.append(f"{item.name}\n")
        if dry_run or confirm_destructive(listing, "Move them?", yes=yes):
            # Timestamped, so repeated runs never nest one quarantine inside
            # another.
            backup_dir = base_path / "BACKUP" / clock.today()
            for issue, item in strays:
                if dry_run:
                    issue.resolution = f"would move to BACKUP/{backup_dir.name}/{item.name}"
                    continue
                ensure_directory(backup_dir)
                run_fs(f"quarantine {item.name}", shutil.move, str(item), str(backup_dir / item.name))
                issue.resolution = f"moved to BACKUP/{backup_dir.name}/{item.name}"

    errors, notices = split_issues(issues)

    if as_json:
        emit_json(
            {
                "ok": not errors,
                "errors": len(errors),
                "notices": len(notices),
                "issues": [issue.as_dict() for issue in issues],
            }
        )
    else:
        render_verify_report(issues)

    if report:
        write_verify_report(report, base_path, issues)
        if not as_json:
            # Stdout is carrying a JSON document; a receipt would break it.
            note(f"Report saved to {report}")

    if errors:
        raise typer.Exit(1)


def render_verify_report(issues: Sequence[Issue]) -> None:
    """The human report, on stdout.

    These findings are the command's output the way ``list``'s table is, and
    the exit code carries pass/fail -- so they belong on stdout, and under
    --quiet the exit code is the whole report. They used to go to stderr while
    the clean-archive line went to stdout, which meant the primary output
    changed stream depending on the result, and a fully --fixed run wrote a
    green success summary to stderr.
    """
    if output.QUIET:
        return
    if not issues:
        ok("Verification passed. Every prefix unique, every folder where it says it is.")
        return

    errors, notices = split_issues(issues)
    grouped: dict[str, list[Issue]] = {}
    for issue in issues:
        grouped.setdefault(issue.group, []).append(issue)

    for group, group_issues in grouped.items():
        unresolved = sum(1 for i in group_issues if i.severity == ERROR and not i.resolution)
        heading = Text(f"{group}  ", style="bold")
        tail = f", {unresolved} unresolved" if unresolved else ""
        heading.append(plural(len(group_issues), "finding") + tail, style=STYLES["caption"])
        console.print(heading)
        for issue in group_issues:
            console.print(issue.line(), soft_wrap=True)

    summary = Text()
    summary.append(plural(len(errors), "error"), style="red" if errors else "green")
    summary.append(", ")
    summary.append(plural(len(notices), "notice"), style="yellow" if notices else "dim")
    resolved = len(issues) - len(errors) - len(notices)
    if resolved:
        summary.append(f", {resolved} resolved", style="green")
    console.print(_decorate("error" if errors else "ok", summary))


def write_verify_report(report: Path, base_path: Path, issues: Sequence[Issue]) -> None:
    """The --report file. Derives its own counts, so they cannot disagree."""
    errors, notices = split_issues(issues)
    lines = [
        f"GWArchive Verification Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Archive: {base_path}",
        "",
    ]
    if not issues:
        lines.append("Verification passed. The GWArchive structure is valid.")
    else:
        lines.append(f"{len(errors)} errors, {len(notices)} notices, {len(issues)} findings total")
        lines.append("")
        for issue in issues:
            resolution = f"  -> {issue.resolution}" if issue.resolution else ""
            lines.append(f"[{issue.severity}] {issue.group}: {issue.message}{resolution}")
    report.write_text("\n".join(lines) + "\n")


# --- Shell integration ---------------------------------------------------------


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

        eval "$(python3 g.py shell-init)"

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
    """Emit shell functions for bash/zsh. Use with: eval "$(python3 g.py shell-init)"

    Defines 'gcd' and 'ggd' (same body, two names) and 'clears', which clears
    the screen and shows a pokemon. With --greet, 'clears' also runs once as
    the eval happens -- the pokemon-at-shell-startup greeting.
    """
    interpreter = shlex.quote(sys.executable)
    script = shlex.quote(str(Path(__file__).resolve()))
    base_arg = f" --path {shlex.quote(str(path))}" if path else ""
    greet_line = "\nclears" if greet else ""

    print(
        f"""_gwarchive_cd() {{
    local target
    target="$({interpreter} {script} cd "$@"{base_arg})" || return 1
    if [ -z "$target" ]; then
        echo "gwarchive: no folder matching '$*'" >&2
        return 1
    fi
    cd "$target" || return 1
}}
gcd() {{ _gwarchive_cd "$@"; }}
ggd() {{ _gwarchive_cd "$@"; }}
clears() {{ {interpreter} {script} clears "$@"; }}{greet_line}"""
    )


if __name__ == "__main__":
    app()
