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

import contextlib
import json
import os
import tempfile
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
    # ValueError, not just json.JSONDecodeError (a subclass): a tombstone
    # that is not valid UTF-8 raises UnicodeDecodeError from read_text, also a
    # ValueError, and that must read as corrupt too rather than crash every
    # reader of this file.
    except (OSError, ValueError):
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
    """Write the tombstone atomically.

    ``write_text`` truncates before it writes, so a crash or ENOSPC mid-write
    left a 0-byte or half-written file that the next read called "corrupt" --
    discarding the recorded remotes and orphaning every object pruning could
    otherwise have reclaimed. The temp file is a sibling (same directory, same
    filesystem) so ``os.replace`` is atomic, and its name starts with
    ``.gwarchive-`` so it is itself skipped by every reserved-name check if a
    crash leaves it behind.
    """
    meta_file = folder / TOMBSTONE_NAME
    # mkstemp creates 0600; keep the mode the file had, or what write_text
    # gave it before (0644 under the usual umask), so a replace does not
    # quietly make the tombstone unreadable to other users of a shared archive.
    try:
        mode = meta_file.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=".gwarchive-offload.tmp-", dir=folder)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, meta_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def is_offloaded(folder: Path) -> bool:
    """True if folder carries metadata with a non-null offloaded_at timestamp."""
    meta = read_tombstone(folder)
    return bool(meta and meta.get("offloaded_at"))
