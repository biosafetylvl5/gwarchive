"""Where things are: the archive root, walking it, resolving prefixes, allocating numbers.

`resolve_prefix` is the single resolution entry point, and it treats ambiguity
as a failure rather than returning the first match. There is no index or
database anywhere in this tool -- the filesystem is the source of truth, so
allocation works by scanning every category including Old/.

`transfer_message` lives here rather than in output because it needs
`display()`. Moving it up to output would make output reach into layer 2, and
rewriting its signature to take pre-rendered strings would scatter twenty
display() calls across its ten call sites in place of the one it makes now.
One layer down was the cheaper answer than either.
"""

import os
from pathlib import Path

from rich.text import Text

from gwarchive.naming import (
    CATEGORIES,
    MAX_NUMBER,
    MAX_SUBNUMBER,
    SUBFOLDER_RE,
    _reserved_path,
    folder_prefix,
    normalize_prefix,
)
from gwarchive.output import die, error, hint, labelled_message, program


def get_base_path() -> Path:
    base_path = os.environ.get("GWARCHIVE_BASE")
    if base_path:
        return Path(base_path)
    return Path.home() / "gwarchive"


def ensure_directory(path: Path) -> bool:
    """True if it created the directory.

    Silent on success by design -- commands own output, so one action prints
    one line -- but not on failure: this was the one filesystem call that
    raised straight through, so a read-only parent gave a traceback.
    ``is_dir`` rather than ``exists``, so a *file* at the path is a reported
    failure and not a silent "already there".
    """
    if path.is_dir():
        return False
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise die(f"Could not create {path}: {exc.strerror or exc}") from exc
    return True


def rel(path: Path, base: Path) -> str:
    try:
        return os.path.relpath(str(path), str(base))
    except ValueError:  # different drive on Windows
        return str(path)


def display(path: Path, base: Path) -> str:
    """Shortest unambiguous rendering of a path for human output."""
    relative = rel(path, base)
    return str(path) if relative.startswith("..") else relative


def category_dirs(base_path: Path, only: str | None = None) -> list[tuple[str, Path]]:
    names = [only] if only else list(CATEGORIES.values())
    return [(name, base_path / name) for name in names if (base_path / name).exists()]


def iter_archive_folders(base_path: Path, only: str | None = None) -> list[tuple[str, Path]]:
    """Every prefixed top-level folder, as (category name, path).

    Categories in declaration order, folders sorted by name within each.
    """
    return [
        (category, item)
        for category, cat_path in category_dirs(base_path, only)
        for item in sorted(cat_path.iterdir(), key=lambda p: p.name)
        if item.is_dir() and folder_prefix(item.name)
    ]


def find_folders_by_prefix(prefix: str, base_path: Path) -> list[Path]:
    """Every folder carrying this prefix, in any category.

    Prefixes are permanent and travel with the folder, so the category letter
    does not tell you which directory the folder currently lives in. Scanning
    all of them is what makes a moved folder findable.
    """
    return [item for _, item in iter_archive_folders(base_path) if folder_prefix(item.name) == prefix]


def resolve_prefix(prefix: str, base_path: Path, quiet: bool = False) -> Path | None:
    """Resolve a prefix to exactly one folder, or None.

    Ambiguity is a failure, not a coin flip -- picking the first ``iterdir()``
    match silently navigates people to the wrong folder.
    """
    normalized = normalize_prefix(prefix)
    if not normalized:
        if not quiet:
            error(f"Not a valid prefix: {prefix}  (expected e.g. P1, P01 or P0001)")
        return None

    matches = find_folders_by_prefix(normalized, base_path)
    if not matches:
        if not quiet:
            error(f"No folder found with prefix {normalized}")
            hint(f"{program()} find {normalized}")
        return None
    if len(matches) > 1:
        if not quiet:
            listing = Text(f"Prefix {normalized} is ambiguous -- {len(matches)} folders share it:")
            for match in matches:
                listing.append(f"\n{display(match, base_path)}")
            error(listing)
            hint(f'{program()} rename {normalized} "New name"')
        return None
    return matches[0]


def allocate_number(category: str, base_path: Path) -> int:
    """Return the next unissued number for a category.

    Scans every category directory, not just this one, because prefixes are
    permanent: a ``P`` folder that now lives in Archive still holds its number,
    and so does a retired one in ``Old/`` under its date-stamped name.
    """
    max_num = 0
    for _, item in iter_archive_folders(base_path):
        prefix = folder_prefix(item.name)
        if prefix and prefix[0] == category:
            max_num = max(max_num, int(prefix[1:]))
    if max_num >= MAX_NUMBER:
        raise die(
            f"Category {category} is full -- {MAX_NUMBER} is the highest number a 4-digit prefix can hold."
        )
    return max_num + 1


def allocate_subnumber(parent_folder: Path, parent_prefix: str) -> int:
    max_num = 0
    for item in parent_folder.iterdir():
        if not item.is_dir():
            continue
        match = SUBFOLDER_RE.match(item.name)
        if match and match.group(1) == parent_prefix:
            max_num = max(max_num, int(match.group(2)))
    if max_num >= MAX_SUBNUMBER:
        raise die(
            f"{parent_prefix} is full -- {MAX_SUBNUMBER} is the highest number "
            f"a 2-digit subfolder suffix can hold."
        )
    return max_num + 1


def transfer_message(verb: str, source: Path, target: Path, base_path: Path) -> Text:
    """Two local paths, one per line."""
    return labelled_message(verb, [("from", display(source, base_path)), ("to", display(target, base_path))])


def compute_folder_stats(folder: Path) -> tuple[int, int]:
    """Return total bytes and file count of folder, excluding metadata files."""
    total_size = 0
    file_count = 0
    if not folder.is_dir():
        return 0, 0
    for item in folder.rglob("*"):
        if item.is_file():
            if _reserved_path(item, folder):
                continue
            try:
                total_size += item.stat().st_size
                file_count += 1
            except OSError:
                continue
    return total_size, file_count


def require_archive(base_path: Path) -> None:
    if not base_path.exists():
        raise die(f"No archive at {base_path}", fix=f"{program()} init --path {base_path}")
