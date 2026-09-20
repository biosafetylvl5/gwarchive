"""What things are called. The taxonomy, the four regexes, and name parsing.

`CATEGORIES` is the letter->directory map and everything below it derives from
that dict -- change it and the rest follows. That property only holds while the
derivations sit next to it, which is why they are all here and not spread
across the modules that use them.

Matching names case-sensitively while matching letters case-insensitively is
what once made `mv P1 archive` rename a folder in place and report a green
"Moved", so CATEGORY_BY_NAME is upper-cased on purpose.

Layer 0: stdlib only. Nothing here may import another gwarchive module.
"""

import re
from pathlib import Path

# Constants
CATEGORIES = {
    "P": "Project",
    "R": "Recurring",
    "M": "Material",
    "A": "Archive",
    "O": "Old",
}


CATEGORY_LETTERS = "".join(CATEGORIES)
# The directory names, as a set: four places test membership against them.
CATEGORY_NAMES = frozenset(CATEGORIES.values())
# Upper-cased, so a category name resolves back to its letter in any casing.
CATEGORY_BY_NAME = {name.upper(): letter for letter, name in CATEGORIES.items()}
CATEGORY_METAVAR = "[" + "|".join(CATEGORIES) + "]"


MAX_NUMBER = 9999
MAX_SUBNUMBER = 99


# The bookkeeping file a folder carries once it has been pushed or offloaded to
# a remote. Its presence with a non-null ``offloaded_at`` makes the folder a
# tombstone: the name and prefix stay, the bytes live on the remote.
TOMBSTONE_NAME = ".gwarchive-offload.json"


# A prefix on its own, in any width it is accepted in: P1, p01, P0001
PREFIX_RE = re.compile(rf"^([{CATEGORY_LETTERS}])(\d{{1,4}})$")
# A live folder name: P0001 Some Name
FOLDER_RE = re.compile(rf"^([{CATEGORY_LETTERS}])(\d{{4}})(?:\s+(.*))?$")
# A retired folder name in Old/: 2026-09-04-P0001-Some Name
OLD_RE = re.compile(rf"^(\d{{4}}-\d{{2}}-\d{{2}})-([{CATEGORY_LETTERS}])(\d{{4}})-(.*)$")
# A subfolder name: P0001.01 Some Name
SUBFOLDER_RE = re.compile(rf"^([{CATEGORY_LETTERS}]\d{{4}})\.(\d{{2}})(?:\s+(.*))?$")


def normalize_prefix(prefix: str) -> str | None:
    """``P1``, ``p01``, ``P001``, ``P0001`` -> ``P0001``. Anything else, None."""
    candidate = prefix.strip().upper()
    match = PREFIX_RE.match(candidate)
    if not match:
        return None
    letter, number = match.groups()
    return f"{letter}{int(number):04d}"


def name_parts(name: str) -> tuple[str, str] | None:
    """(prefix, descriptor) for a folder name, live or retired, else None.

    One home for the group numbering of both name regexes, which used to be
    spelled out twice with a separate test for each half.
    """
    match = FOLDER_RE.match(name)
    if match:
        return f"{match.group(1)}{match.group(2)}", (match.group(3) or "").strip()
    match = OLD_RE.match(name)
    if match:
        return f"{match.group(2)}{match.group(3)}", (match.group(4) or "").strip()
    return None


def folder_prefix(name: str) -> str | None:
    """The permanent identifier out of a folder name, live or retired."""
    return parts[0] if (parts := name_parts(name)) else None


def folder_descriptor(name: str) -> str | None:
    """The human-readable part of a folder name, live or retired."""
    return (parts[1] or None) if (parts := name_parts(name)) else None


def rename_preserving_prefix(name: str, new_descriptor: str) -> str:
    match = OLD_RE.match(name)
    if match:
        return f"{match.group(1)}-{match.group(2)}{match.group(3)}-{new_descriptor}"
    prefix = folder_prefix(name)
    return f"{prefix} {new_descriptor}" if prefix else new_descriptor


def category_letter_for(destination: str) -> str | None:
    """Map a category name ('Old', 'old') or a letter ('O', 'o') to a letter.

    Case-insensitive on both, which it was not: the letter branch upper-cased
    and the name branch compared exactly, so ``mv P1 archive`` fell past this
    form and two others into "rename in place" -- reporting a green ``Moved``
    for a folder that never left Project/. ``mv P1 old`` quietly declined to
    retire anything.
    """
    candidate = destination.upper()
    if candidate in CATEGORY_BY_NAME:
        return CATEGORY_BY_NAME[candidate]
    return candidate if len(candidate) == 1 and candidate in CATEGORIES else None


def _reserved_member(name: str) -> bool:
    """True for one path *component* in the metadata namespace.

    The one definition of the rule, and per-component rather than per-leaf on
    purpose -- ``.gwarchive-cache/junk`` has an innocent leaf name. See
    AGENTS.md, "Compression". Applied on the way in *and* out: an archive may
    not be one we wrote. ``_reserved_path`` takes a path.
    """
    return name == TOMBSTONE_NAME or name.startswith(".gwarchive-")


def _reserved_path(path: Path, root: Path) -> bool:
    """True when any component of ``path`` below ``root`` is reserved."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = (path.name,)
    return any(_reserved_member(part) for part in parts)


def matches_pattern(name: str, pattern: str, exact: bool) -> bool:
    """--exact means equality. Without it, a case-insensitive substring."""
    return name == pattern if exact else pattern.lower() in name.lower()
