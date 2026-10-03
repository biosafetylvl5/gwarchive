"""Commands that make things: init, create, mksub.

None of these takes --dry-run. They allocate by scanning the tree rather than
from a counter, so a dry run would report a prediction, not a reservation."""

from typing import Annotated

import typer
from rich.text import Text

from gwarchive import output
from gwarchive.destination import (
    check_name,
)
from gwarchive.naming import (
    CATEGORIES,
    folder_prefix,
)
from gwarchive.options import (
    BasePath,
    CategoryArg,
    app,
)
from gwarchive.output import (
    STYLES,
    console,
    detail,
    die,
    note,
    ok,
    panel,
)
from gwarchive.paths import (
    allocate_number,
    allocate_subnumber,
    display,
    ensure_directory,
    require_archive,
    resolve_prefix,
)


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
    check_name(name)
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
    check_name(name)

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
