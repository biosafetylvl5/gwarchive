""".gwarchive-offload.json: the file a folder carries once it has been pushed.

Its presence with a non-null `offloaded_at` is what makes a folder a tombstone
-- the name and prefix stay on disk, the bytes live on the remote. There is no
remote listing anywhere in this tool, so this file IS the version index.

`read_tombstone_state` distinguishes four states (absent / corrupt / not-object
/ ok) and that distinction is load-bearing: collapsing them to None would make
a corrupt tombstone read as NO tombstone, discarding the recorded remotes and
orphaning every remote object that pruning could have reclaimed.

The two readers below are tolerant on purpose. The file is untrusted input: a
JSON `null` is a PRESENT key, so a naive str() returned the string "None" --
truthy, and past every guard downstream. And JSON's `true` is an int to Python,
so the int reader excludes bools explicitly.
"""

import json
from pathlib import Path

from gwarchive.naming import TOMBSTONE_NAME


def _mstr(data: dict[str, object], key: str, default: str = "") -> str:
    """A string field out of tombstone JSON, whatever the file actually holds.

    Not ``str(data.get(key, ""))``: a JSON ``null`` is a *present* key, so that
    returned the string ``"None"`` -- truthy, and past every guard downstream.
    """
    value = data.get(key)
    return value if isinstance(value, str) else default


def _mint(data: dict[str, object], key: str) -> int | None:
    """An integer field, or None. JSON's ``true`` is an ``int``; exclude it."""
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_tombstone_state(folder: Path) -> tuple[str, dict[str, object] | None]:
    """The tombstone, and which of four states it is in.

    ``absent`` / ``corrupt`` / ``not-object`` / ``ok``. ``verify`` needs the
    distinction to report the right thing, and the sync commands need it
    because collapsing all four to None makes a *corrupt* tombstone read as
    *no* tombstone -- which discards the recorded remotes and the whole version
    index, orphaning every object pruning could otherwise have reclaimed.
    """
    meta_file = folder / TOMBSTONE_NAME
    if not meta_file.is_file():
        return "absent", None
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "corrupt", None
    return ("ok", data) if isinstance(data, dict) else ("not-object", None)


def read_tombstone(folder: Path) -> dict[str, object] | None:
    """The tombstone dict, or None when it is absent or unreadable."""
    return read_tombstone_state(folder)[1]


def get_recorded_remotes(meta: dict[str, object] | None) -> list[str]:
    if not meta:
        return []
    raw = meta.get("remotes")
    if isinstance(raw, list):
        return [str(r) for r in raw]
    return []


def write_tombstone(folder: Path, data: dict[str, object]) -> None:
    meta_file = folder / TOMBSTONE_NAME
    meta_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def is_offloaded(folder: Path) -> bool:
    """True if folder carries metadata with a non-null offloaded_at timestamp."""
    meta = read_tombstone(folder)
    return bool(meta and meta.get("offloaded_at"))
