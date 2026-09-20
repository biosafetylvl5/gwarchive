"""Commands that read the tree: list, find.

find exits 1 on no match so it composes like grep. Both assert through --json
in the suite rather than against rendered box drawing."""

import re
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from gwarchive.naming import (
    CATEGORIES,
    CATEGORY_METAVAR,
    folder_descriptor,
    folder_prefix,
    matches_pattern,
)
from gwarchive.options import (
    AsJsonTable,
    BasePath,
    CategoryOpt,
    app,
    validate_category,
)
from gwarchive.output import (
    STYLES,
    caption,
    console,
    die,
    emit_json,
    new_table,
    note,
    program,
    warn,
)
from gwarchive.paths import (
    display,
    iter_archive_folders,
    require_archive,
)
from gwarchive.tombstone import (
    is_offloaded,
)


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
        raise die(f"No {category_name} directory at {base_path}", fix=f"{program()} init --path {base_path}")

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
        note(f'No folders in {category_name} yet -- create one with:  {program()} create {category} "Name"')
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
