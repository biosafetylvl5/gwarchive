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

import contextlib
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
from gwarchive.output import die, program, warn
from gwarchive.tombstone import _mstr, get_recorded_remotes

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
        if info.islnk():
            # gettarinfo recorded a repeat inode as a headers-only link member
            # with size 0 -- tarfile's own round trip for it. add() re-checks
            # isreg() on what this filter returns, and then reads the file
            # again by ITS OWN path (not the first occurrence's), so turning
            # the type back to a regular file is what makes the data follow.
            info.type = tarfile.REGTYPE
            info.linkname = ""
            with contextlib.suppress(OSError):
                info.size = (folder / info.name).stat().st_size
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


def unsupported_members(folder: Path) -> list[Path]:
    """Non-reserved entries that cannot round-trip through this format.

    Hard links are handled by ``create_archive``'s filter, but a symlink, a
    fifo, a socket or a device node has no regular-file content for tar to
    carry -- real-world cases are ``.venv/bin/python*``, bundled frameworks
    and ``cp -al`` trees. ``followlinks=False`` so a symlinked directory is
    reported once, as itself, rather than walked into.
    """
    found: list[Path] = []
    for root, dirs, files in os.walk(folder, followlinks=False):
        root_path = Path(root)
        kept_dirs = []
        for name in dirs:
            candidate = root_path / name
            if _reserved_member(name):
                continue
            if candidate.is_symlink():
                found.append(candidate)
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            if _reserved_member(name):
                continue
            candidate = root_path / name
            if candidate.is_symlink() or not candidate.is_file():
                found.append(candidate)
    return found


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
            fix=f"{program()} verify   # then repair or remove the archive block",
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
            fix=f"{program()} push P1",
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
        fix=f"{program()} restore P1 --version 1",
    )


def _version_remotes(entry: dict[str, object], recorded_dirs: Sequence[str]) -> list[str]:
    """Where this one version actually landed.

    A version recorded before this distinction existed carries no ``remotes``
    key of its own; its location is assumed to be the folder's whole
    historical union, which is what pruning already assumed before there was
    a per-version record. A version this run created carries only the
    remotes it was actually copied to -- not every remote the folder has ever
    used, which is what let a push to a *second* remote prune the object off
    the *first*, one it had never touched.
    """
    if "remotes" in entry:
        return get_recorded_remotes(entry)
    return list(recorded_dirs)


def _already_deleted(tail: str) -> bool:
    """True when a failed ``deletefile`` means the object was already gone.

    Covers rclone's "object not found" and "directory not found" wording
    across backends -- a surplus version pruning never pushed to is not a
    failure to clean up, it is already clean.
    """
    return "not found" in tail.lower()


def _remote_parent(recorded_dir: str) -> str:
    """The directory a sibling object actually lands in, for "same remote" checks.

    ``remote_archive_object`` writes beside the folder's mirror path, not
    inside it, so two recorded dirs that differ only in their folder-name
    tail -- the before and after of a ``rename`` -- still put every version's
    object in one physical directory. Comparing the recorded dirs themselves
    would see a rename as a move to a remote the newer version never used,
    and leave the older version's entry stuck forever.
    """
    base = recorded_dir.rstrip("/")
    head, sep, _ = base.rpartition("/")
    return head if sep else base


def prune_versions(arch: dict[str, object], recorded_dirs: Sequence[str], keep: int) -> int:
    """Drop all but the newest ``keep`` objects. Returns how many went.

    Only names already recorded in ``versions`` are ever deleted, and a failed
    delete warns and keeps its entry for the next push -- see AGENTS.md,
    "Retention".

    A surplus version is deleted only from the remote dirs where the newest
    *surviving* version is also recorded -- dirs this run never touched for
    that version are left alone, and the version stays indexed under whatever
    locations still hold it. It drops out of ``versions`` entirely only once
    none remain.

    ``versions`` arrives newest-first from ``record_version`` and stays that
    way: survivors keep their order, and a surplus entry that is not fully
    removed goes back on the end. **Do not sort it by object name** --
    ``archive_object_name`` puts the *folder* name first, so one ``rename``
    inverts the index. See AGENTS.md, "Retention".
    """
    if keep <= 0:
        return 0
    versions = archive_versions(arch)
    surplus = versions[keep:]
    if not surplus:
        return 0

    survivors = list(versions[:keep])
    newest_remotes = _version_remotes(survivors[0], recorded_dirs)
    removed = 0
    held_back: list[dict[str, object]] = []
    for entry in surplus:
        name = _mstr(entry, "name")
        if not name:
            continue
        entry_remotes = _version_remotes(entry, recorded_dirs)
        newest_parents = {_remote_parent(d) for d in newest_remotes}
        # Deduplicated: several recorded dirs can resolve to the same sibling
        # object (a rename leaves both the old and new path on record).
        attempt = [d for d in dict.fromkeys(entry_remotes) if _remote_parent(d) in newest_parents]
        deleted: list[str] = []
        for target_dir in attempt:
            target = remote_archive_object(target_dir, name)
            proc = external.run_rclone(["deletefile", target], capture_output=True)
            if proc.returncode == 0:
                deleted.append(target_dir)
                continue
            tail = rclone_tail(proc)
            if _already_deleted(tail):
                deleted.append(target_dir)
                continue
            warn(f"Could not remove {target}:\n{tail}")
        remaining = [d for d in entry_remotes if d not in deleted]
        if remaining:
            entry["remotes"] = remaining
            held_back.append(entry)
        else:
            removed += 1
    arch["versions"] = [*survivors, *held_back]
    return removed


def would_prune(
    existing: dict[str, object], dests: Sequence[str], recorded_dirs: Sequence[str], keep: int
) -> int:
    """How many copies a real push would remove, counted from the dry-run side.

    Pruning happens *after* the new version is recorded, and now only removes
    a surplus version from dirs the newest version actually reached, so the
    projection has to simulate that newest version too -- ``dests`` is where
    *this* run's copies would land, known before any of them actually happen.
    """
    if keep <= 0:
        return 0
    prior = existing.get("archive")
    versions = archive_versions(prior if isinstance(prior, dict) else None)
    incoming: dict[str, object] = {"remotes": list(dests)}
    surplus = [incoming, *versions][keep:]
    if not surplus:
        return 0
    newest_remotes = _version_remotes(incoming, recorded_dirs)
    removed = 0
    for entry in surplus:
        entry_remotes = _version_remotes(entry, recorded_dirs)
        newest_parents = {_remote_parent(d) for d in newest_remotes}
        if entry_remotes and all(_remote_parent(d) in newest_parents for d in entry_remotes):
            removed += 1
    return removed


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
