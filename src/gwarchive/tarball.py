"""The archive format: codec choice, tar streaming, hashing, and the version index.

Named tarball rather than archive because "archive" already means three other
things here -- the `A` category, the base directory (require_archive,
iter_archive_folders), and the pytest fixture for a populated tree. The module
claims only the fourth.

Writer and reader stay together: _vet_member is the path-traversal guard for
extraction and _tar_stream is the codec context manager both directions share.
Splitting them would split the two halves of one format contract.

Extraction vets every member on every Python rather than trusting tarfile's
filter="data": that landed in 3.12 and was backported to 3.11.4, while
requires-python admits 3.11.0 -- and CI resolves '3.11' to the newest patch, so
the vulnerable range is exactly the range nothing exercises.

The object extension is the codec's own on purpose. Lose the tombstone entirely
and `zstd -d < x.tar.zst | tar -tvf -` is still a complete recovery path, which
would not be true if the codec lived only in JSON.

This is a SEAM module (with `clock` and `external`): the suite patches
pick_codec and create_archive, so callers import the MODULE and write
tarball.pick_codec().
"""

import hashlib
import os
import shutil
import subprocess
import tarfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from gwarchive import external
from gwarchive.external import rclone_tail
from gwarchive.naming import TOMBSTONE_NAME, _reserved_member
from gwarchive.output import die, warn
from gwarchive.tombstone import _mstr

# The archive formats push and offload can write. The extension is deliberately
# the codec's own: lose the tombstone entirely and
# ``zstd -d < x.tar.zst | tar -tvf -`` is still a complete recovery path, which
# would not be true if the codec lived only in JSON.
ARCHIVE_FORMATS = ("tar.zst", "tar.gz")


# Read size for the object hash. Big enough that hashing a multi-gigabyte
# archive is one sequential pass, small enough to stay off the heap.
HASH_CHUNK = 1 << 20


def pick_codec() -> str:
    """The archive format to write: zstd when its binary is there, gzip otherwise.

    ``$GWARCHIVE_CODEC`` forces one. This function is the seam tests
    monkeypatch, because forcing gzip runs the whole create/verify/extract path
    with no external binary at all -- and CI has neither zstd nor rclone.
    """
    forced = os.environ.get("GWARCHIVE_CODEC", "").strip().lower()
    if forced in ("zstd", "zst", "tar.zst"):
        return "tar.zst"
    if forced in ("gzip", "gz", "tar.gz"):
        return "tar.gz"
    return "tar.zst" if shutil.which("zstd") else "tar.gz"


def zstd_argv(out: Path) -> list[str]:
    """Compression flags for a streamed write. Pure, so tests need no binary.

    ``-f`` because zstd otherwise refuses an existing output and prompts on a
    TTY, ``-q`` because its progress meter would fight the Rich spinner, and
    ``-3`` spelled out because that speed/ratio point is a decision here: the
    network is the bottleneck this feature exists to relieve, not the CPU.
    """
    return ["-T0", "-3", "-q", "-f", "-o", str(out)]


def archive_object_name(folder_name: str, stamp: str, codec: str) -> str:
    """``P0001 Alpha.20260909-101530.tar.zst``."""
    return f"{folder_name}.{stamp}.{codec}"


def remote_archive_object(recorded_dir: str, object_name: str) -> str:
    """The object path *beside* a folder's remote directory, not inside it.

    See AGENTS.md, "Remote layout", for the layout this produces and why
    sibling placement is what keeps a later --no-compress push from dragging a
    stale tarball down into the live folder.
    """
    base = recorded_dir.rstrip("/")
    head, sep, _ = base.rpartition("/")
    return f"{head}{sep}{object_name}" if sep else object_name


def file_sha256(path: Path) -> str:
    """SHA-256 of a file, streamed a chunk at a time."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _tar_stream(path: Path, codec: str, *, write: bool) -> Iterator[tarfile.TarFile]:
    """A sequential tar over either codec, in either direction.

    A zstd stream lives on a pipe, so the archive is non-seekable: ``r|`` and
    ``w|`` only, and no ``getmembers()`` up front. gzip goes through the same
    shape so neither the add loop nor the vetting loop is written twice.

    Two orderings here are load-bearing and both are recorded in AGENTS.md,
    "Subprocess boundaries": ``tarfile`` closes *inside* the try, before the
    pipe, and the gzip write stays ``w:gz`` rather than ``w|gz``.
    """
    if codec != "tar.zst":
        with tarfile.open(path, "w:gz" if write else "r|gz") as archive:
            yield archive
        return

    doing = "writing" if write else "reading"
    if write:
        proc = external.run_zstd(zstd_argv(path), stdin=subprocess.PIPE)
        stream = proc.stdin
    else:
        proc = external.run_zstd(["-d", "-c", "-q", str(path)], stdout=subprocess.PIPE)
        stream = proc.stdout
    if stream is None:
        raise die(f"zstd gave us no stream for {doing} {path.name}")
    try:
        with tarfile.open(fileobj=stream, mode="w|" if write else "r|") as archive:
            yield archive
    finally:
        # Closing the pipe is what tells zstd to finish; waiting is what stops
        # a tarfile error from leaking a process on it.
        stream.close()
        proc.wait()
    if proc.returncode != 0:
        raise die(f"zstd exited with code {proc.returncode} {doing} {path.name}")


def create_archive(folder: Path, out: Path, codec: str) -> tuple[int, int]:
    """Pack ``folder`` into ``out``. Returns (member count, uncompressed bytes).

    Members are relative to the folder -- ``sub/f.txt``, never
    ``P0001 Alpha/sub/f.txt``. Rooting them at the folder name would bake the
    name into the archive, so ``rename`` would break the round trip, and would
    force extraction one level up into the category directory. Relative members
    mean extraction targets the folder itself and can never write above it.

    The counts come from this walk, the one that writes the archive, rather
    than from a second ``compute_folder_stats`` pass: two walks can disagree
    when a sync daemon touches the tree mid-flight, and ``stat`` counts a
    symlink as its target's bytes where tar stores a 0-byte link member.
    """
    members = 0
    total = 0

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        nonlocal members, total
        # Returning None prunes a directory subtree whole, so a .gwarchive-
        # cache directory is skipped along with everything under it.
        if any(_reserved_member(part) for part in info.name.split("/")):
            return None
        members += 1
        total += info.size
        return info

    def _add_children(archive: tarfile.TarFile, items: Sequence[Path]) -> None:
        # A sync daemon touching the tree mid-walk -- the exact case this
        # function's counting is written for -- raises out of tarfile.add. Every
        # other failure in this layer is a message with a way out; this one was
        # a traceback.
        try:
            for item in items:
                archive.add(item, arcname=item.name, filter=keep)
        except OSError as exc:
            raise die(
                f"Could not pack {folder.name}: {exc.strerror or exc}",
                fix=f'chmod -R +r "{folder}"   # then retry; nothing has been deleted',
            ) from exc

    with _tar_stream(out, codec, write=True) as archive:
        _add_children(archive, sorted(folder.iterdir(), key=lambda item: item.name))

    return members, total


def verify_archive(path: Path, codec: str, expected_members: int) -> None:
    """Read the finished archive back and confirm it holds what we packed.

    Only ``offload`` calls this. It is a full re-read, and offload is the one
    command that deletes the originals, so a silently truncated archive would
    be data loss rather than an inconvenience. ``push`` keeps the local copy
    and skips the pass.
    """
    found = 0
    with _tar_stream(path, codec, write=False) as archive:
        for _ in archive:
            found += 1
    if found != expected_members:
        raise die(
            f"{path.name} holds {found} member(s), expected {expected_members}.",
            fix="Retry the offload -- nothing local has been deleted.",
        )


def _vet_member(info: tarfile.TarInfo, folder: Path) -> bool:
    """True when a member is safe to write under ``folder``.

    Run on every member on every Python, not just where ``filter="data"`` is
    missing. ``tarfile``'s own filter landed in 3.12 and was backported to
    3.11.4, while the PEP 723 pin admits 3.11.0 -- and CI resolves '3.11' to
    the newest patch release, so the vulnerable range is exactly the range
    nothing exercises.

    Symlinks and hardlinks are refused rather than followed. That makes
    push -> pull not round-trip faithful for links, which matches today:
    ``rclone copy`` skips them too unless given ``-L``.
    """
    name = info.name
    if not name or name.startswith("/"):
        return False
    if not (info.isfile() or info.isdir()):
        return False
    parts = name.split("/")
    if any(part == ".." for part in parts):
        return False
    if any(_reserved_member(part) for part in parts):
        return False
    root = folder.resolve()
    dest = (root / name).resolve()
    return dest == root or root in dest.parents


def extract_archive(path: Path, folder: Path, codec: str) -> int:
    """Unpack ``path`` into ``folder``. Returns the number of members written.

    Vetted and extracted one member at a time, because the stream is not
    seekable. An unsafe or reserved member is dropped with a warning rather
    than aborting a restore that is otherwise sound.
    """
    written = 0
    refused = 0
    with _tar_stream(path, codec, write=False) as archive:
        for info in archive:
            if not _vet_member(info, folder):
                refused += 1
                continue
            if hasattr(tarfile, "data_filter"):
                archive.extract(info, path=str(folder), filter="data")
            else:  # pragma: no cover -- Python 3.11.0 to 3.11.3
                archive.extract(info, path=str(folder))
            written += 1
    if refused:
        noun = "member" if refused == 1 else "members"
        warn(f"Skipped {refused} unsafe or reserved {noun} in {path.name}")
    return written


def archive_meta_of(meta: dict[str, object] | None) -> dict[str, object] | None:
    """The archive block from a tombstone, or None for a per-file remote.

    Absence of the key is the only archived-or-not signal in the tool: there is
    no remote listing anywhere, so the tombstone *is* the version index.
    """
    if not meta:
        return None
    raw = meta.get("archive")
    if not isinstance(raw, dict):
        return None
    fmt = raw.get("format")
    if fmt not in ARCHIVE_FORMATS:
        raise die(
            f"{TOMBSTONE_NAME} names an archive format this build cannot read: {fmt!r}",
            code=2,
            fix="g.py verify   # then repair or remove the archive block",
        )
    return raw


def archive_versions(arch: dict[str, object] | None) -> list[dict[str, object]]:
    """The recorded objects for an archived folder, newest first."""
    if not arch:
        return []
    raw = arch.get("versions")
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def select_version(arch: dict[str, object], spec: str | None) -> dict[str, object]:
    """Pick a recorded version: the newest by default, else an index or a name.

    Without this, --keep would be storage with no way to read it back.
    """
    versions = archive_versions(arch)
    if not versions:
        raise die(
            f"{TOMBSTONE_NAME} records an archive but no versions of it.",
            fix="g.py push P1",
        )
    if spec is None:
        return versions[0]
    wanted = spec.strip()
    if wanted.isdecimal():
        index = int(wanted)
        if 1 <= index <= len(versions):
            return versions[index - 1]
    for entry in versions:
        if _mstr(entry, "name") == wanted:
            return entry
    listing = "\n".join(f"  {i}  {entry.get('name')}" for i, entry in enumerate(versions, 1))
    raise die(
        f"No such version: {wanted}\nRecorded versions, newest first:\n{listing}",
        code=2,
        fix="g.py restore P1 --version 1",
    )


def prune_versions(arch: dict[str, object], recorded_dirs: Sequence[str], keep: int) -> int:
    """Drop all but the newest ``keep`` objects. Returns how many went.

    Only names already recorded in ``versions`` are ever deleted, and a failed
    delete warns and keeps its entry for the next push -- see AGENTS.md,
    "Retention".

    ``versions`` arrives newest-first from ``record_version`` and stays that
    way: survivors keep their order, and a failed delete goes back on the end.
    **Do not sort it by object name** -- ``archive_object_name`` puts the
    *folder* name first, so one ``rename`` inverts the index. See AGENTS.md,
    "Retention".
    """
    if keep <= 0:
        return 0
    versions = archive_versions(arch)
    surplus = versions[keep:]
    if not surplus:
        return 0

    survivors = list(versions[:keep])
    removed = 0
    for entry in surplus:
        name = _mstr(entry, "name")
        if not name:
            continue
        # Deduplicated: `recorded_dirs` is a historical union, and entries
        # that differ can still resolve to the same sibling object.
        failed = False
        for target in dict.fromkeys(remote_archive_object(d, name) for d in recorded_dirs):
            proc = external.run_rclone(["deletefile", target], capture_output=True)
            if proc.returncode != 0:
                warn(f"Could not remove {target}:\n{rclone_tail(proc)}")
                failed = True
        if failed:
            survivors.append(entry)
        else:
            removed += 1
    arch["versions"] = survivors
    return removed


def would_prune(existing: dict[str, object], keep: int) -> int:
    """How many copies a real push would remove, counted from the dry-run side.

    Pruning happens *after* the new version is recorded, so a dry-run that
    measured the current list would report one too few -- and a --dry-run that
    understates what the real run deletes is worse than no output at all.
    """
    if keep <= 0:
        return 0
    prior = existing.get("archive")
    incoming = len(archive_versions(prior if isinstance(prior, dict) else None)) + 1
    return max(incoming - keep, 0)


def record_version(
    existing: dict[str, object], entry: dict[str, object], codec: str, keep: int
) -> dict[str, object]:
    """The archive block for a tombstone, with this push's version on the front.

    See AGENTS.md, "Tombstones", for the schema and why ``format`` is carried
    per version as well as on the block.
    """
    prior = existing.get("archive")
    name = _mstr(entry, "name")
    # The stamp has second resolution, so two pushes inside one second write the
    # same object name -- the second overwrote the first, so it gets one entry,
    # not two that a prune would read as a spare copy to delete.
    versions = [
        v for v in archive_versions(prior if isinstance(prior, dict) else None) if _mstr(v, "name") != name
    ]
    return {"format": codec, "keep": keep, "versions": [entry, *versions]}
