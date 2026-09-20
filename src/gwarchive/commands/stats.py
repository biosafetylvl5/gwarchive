"""stats: one single-pass walk over the tree."""

from datetime import datetime
from typing import Annotated

import typer
from rich.filesize import decimal
from rich.text import Text

from gwarchive.naming import (
    CATEGORIES,
    _reserved_member,
    folder_prefix,
)
from gwarchive.options import (
    AsJsonTable,
    BasePath,
    CategoryOpt,
    app,
)
from gwarchive.output import (
    INDENT,
    STYLES,
    caption,
    console,
    emit_json,
    new_table,
    pl,
    plural,
    spinner,
    warn,
)
from gwarchive.paths import (
    require_archive,
)
from gwarchive.tombstone import (
    is_offloaded,
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
