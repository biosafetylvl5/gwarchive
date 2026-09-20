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

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.filesize import decimal
from rich.text import Text

# gwarchive.output is imported as a MODULE as well as by name: QUIET is read
# through it so the read happens at call time, not at import time.
from gwarchive import clock, output
from gwarchive.destination import (
    check_no_overwrite,
    check_not_nested,
    check_prefix_available,
    locate_source,
    resolve_destination,
    run_fs,
    warn_if_taken,
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
    category_letter_for,
    folder_descriptor,
    folder_prefix,
    matches_pattern,
    rename_preserving_prefix,
)
from gwarchive.output import (
    INDENT,
    LABEL_WIDTH,
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
    get_base_path,
    iter_archive_folders,
    require_archive,
    resolve_prefix,
    transfer_message,
)

__version__ = "0.3.0"

app = typer.Typer(
    help="GWArchive CLI tool for file organization",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


# The archive formats push and offload can write. The extension is deliberately
# the codec's own: lose the tombstone entirely and
# ``zstd -d < x.tar.zst | tar -tvf -`` is still a complete recovery path, which
# would not be true if the codec lived only in JSON.
ARCHIVE_FORMATS = ("tar.zst", "tar.gz")

# What the per-file rclone mirror must leave behind, so it carries the same set
# as the tar member filter. Without the second pattern a --no-compress push
# uploaded the .gwarchive-* namespace that the compressed path never packs.
RCLONE_EXCLUDES = ["--exclude", TOMBSTONE_NAME, "--exclude", ".gwarchive-*"]

# Read size for the object hash. Big enough that hashing a multi-gigabyte
# archive is one sequential pass, small enough to stay off the heap.
HASH_CHUNK = 1 << 20

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


def is_remote_target(value: str) -> bool:
    """True when rclone reads this as remote storage rather than as a directory.

    rclone honours a colon only in the first path segment: ``nas:archive``, a
    bare ``nas:`` and the ``:sftp,host=x:`` connection-string form are remotes,
    while ``backup/nas:`` and ``~/nas:`` are ordinary directory names -- rclone
    does not expand ``~`` either. An absolute path is a remote target for our
    purposes, because ``/Volumes/Backup`` is a local target somebody meant.
    """
    stripped = value.strip()
    head, colon, _ = stripped.partition(":")
    return stripped.startswith("/") or bool(colon and "/" not in head)


def validate_remote_target(target: str, source: str = "--remote", command: str = "push") -> str:
    """Refuse a remote that rclone would quietly read as a local directory.

    Without a colon in its first segment ``unraid`` is an ordinary relative
    directory name, so a push lands in ./unraid/ beside wherever g was run and
    still reports success. That has happened; hence this guard.
    """
    value = target.strip()
    if is_remote_target(value):
        return value
    suggestion = f"g.py {command} P1 --remote {value}:" if value and "/" not in value else None
    raise die(
        f"{value or '(empty)'} is not a remote target ({source}).\n"
        "rclone only reads an argument as a remote when a colon comes before the first slash,"
        " so this would read or write a local directory instead of remote storage.",
        code=2,
        fix=suggestion or f"g.py {command} P1 --remote nas:archive",
    )


def get_remotes(remotes: Sequence[str] | None = None, command: str = "push") -> list[str]:
    """Resolve remote target(s) from CLI options or $GWARCHIVE_REMOTE."""
    if remotes:
        return [validate_remote_target(r, command=command) for r in remotes]
    env_remote = os.environ.get("GWARCHIVE_REMOTE")
    if env_remote and env_remote.strip():
        return [validate_remote_target(env_remote, "$GWARCHIVE_REMOTE", command)]
    raise die(
        "No remote specified.\nPass --remote, or set $GWARCHIVE_REMOTE once in your shell profile.",
        code=2,
        fix=f"g.py {command} P1 --remote nas:archive",
    )


def remote_root_of(target: str) -> str:
    """The rclone remote name a full target path lives on, e.g. ``nas:``."""
    return target.split(":", 1)[0] + ":" if ":" in target else target


def remote_folder_target(remote_root: str, category: str, folder_name: str) -> str:
    clean_root = remote_root.rstrip("/")
    sep = "" if clean_root.endswith(":") else "/"
    return f"{clean_root}{sep}{category}/{folder_name}"


# --- Destination resolution -- shared by mv, cp, rename and oldify -------------


# --- Remote sync helpers -------------------------------------------------------


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


def run_rclone(
    args: Sequence[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """The single boundary between g and rclone.

    One monkeypatchable function per external tool, so tests can assert args
    without the binary; ``run_zstd`` and ``run_pokeget`` are the other two.
    """
    cmd = ["rclone", *args]
    try:
        return subprocess.run(
            cmd,
            text=True,
            capture_output=capture_output,
            check=False,
        )
    except FileNotFoundError as exc:
        raise die(
            "rclone is not on your PATH.",
            fix="brew install rclone   # or see https://rclone.org/install/",
        ) from exc


def rclone_tail(proc: subprocess.CompletedProcess[str], limit: int = 4) -> str:
    """The last few things rclone actually said, or failing that its exit code."""
    raw = (proc.stderr or proc.stdout or "").strip()
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return f"rclone exited with code {proc.returncode}"
    return "\n".join(lines[-limit:])


def rclone_or_die(
    args: Sequence[str],
    description: str,
    *,
    capture_output: bool = False,
    status: str | None = None,
    fix: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run rclone and fail with an informative message if nonzero.

    ``status`` draws a spinner for the duration, but only on a real terminal --
    piped output and --json stay byte-identical to a run without one.
    """
    with spinner(status):
        proc = run_rclone(args, capture_output=capture_output)
    if proc.returncode != 0:
        raise die(f"Could not {description}:\n{rclone_tail(proc)}", fix=fix)
    return proc


def run_zstd(
    args: Sequence[str],
    *,
    stdin: int | None = None,
    stdout: int | None = None,
) -> subprocess.Popen[bytes]:
    """The boundary to zstd. A ``Popen``, because the archive is piped
    through it and never buffered whole."""
    cmd = ["zstd", *args]
    try:
        return subprocess.Popen(cmd, stdin=stdin, stdout=stdout)
    except FileNotFoundError as exc:
        raise die(
            "zstd is not on your PATH.",
            fix="brew install zstd   # or GWARCHIVE_CODEC=gzip to use the stdlib codec",
        ) from exc


# --- Archive layer -------------------------------------------------------------


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
        proc = run_zstd(zstd_argv(path), stdin=subprocess.PIPE)
        stream = proc.stdin
    else:
        proc = run_zstd(["-d", "-c", "-q", str(path)], stdout=subprocess.PIPE)
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
            proc = run_rclone(["deletefile", target], capture_output=True)
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


def ensure_scratch_space(need: int, where: Path) -> None:
    """Refuse to start an offload that cannot stage its own archive.

    The archive is written in full before anything local is deleted -- that
    ordering is what keeps a failed transfer from losing data -- so offloading
    a large folder briefly needs room for a second copy, on whatever volume
    $TMPDIR points at. Fatal here, rather than a half-written archive later.
    """
    try:
        free = shutil.disk_usage(where).free
    except OSError:
        return
    if free < need:
        raise die(
            f"Not enough scratch space in {where}: {decimal(free)} free, "
            f"about {decimal(need)} needed to stage the archive.",
            fix="TMPDIR=/some/larger/volume g.py offload P1",
        )


def run_pokeget(args: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """The boundary to pokeget.

    None when it is not installed: the greeting runs at shell startup, so a
    missing binary has to degrade silently rather than die.
    """
    try:
        return subprocess.run(["pokeget", *args], text=True, check=False)
    except FileNotFoundError:
        return None


def compress_default() -> bool:
    """On by default: one object beats thousands.

    ``$GWARCHIVE_COMPRESS=0`` per shell, ``--no-compress`` per command.
    """
    return os.environ.get("GWARCHIVE_COMPRESS", "").strip().lower() not in ("0", "false", "no", "off")


def keep_default(keep: int | None) -> int:
    """--keep, else $GWARCHIVE_KEEP, else 1.

    One matches the uncompressed path, so retention grows only when asked.
    """
    if keep is not None:
        return max(keep, 0)
    raw = os.environ.get("GWARCHIVE_KEEP", "").strip()
    if not raw:
        return 1
    if not (raw.isascii() and raw.isdecimal()):
        # Falling back to 1 here means "delete all but one copy" -- the
        # opposite of what somebody raising the retention meant to ask for.
        raise die(
            f"$GWARCHIVE_KEEP is not a whole number: {raw!r}",
            code=2,
            fix="GWARCHIVE_KEEP=2 g.py push P1",
        )
    return int(raw)


def stage_archive(folder: Path, tmpdir: Path, codec: str, *, as_json: bool) -> tuple[Path, dict[str, object]]:
    """Write the folder's archive into tmpdir; return it and its version record.

    The spinner is not decoration: compression is the slow step and it runs
    before any rclone call, so without one a speed-up reads as a freeze.
    """
    name = archive_object_name(folder.name, clock.archive_stamp(), codec)
    staged = tmpdir / name
    with spinner(f"Compressing {folder.name}", as_json=as_json):
        members, raw_size = create_archive(folder, staged, codec)
    entry: dict[str, object] = {
        "name": name,
        "format": codec,
        "pushed_at": clock.now_stamp(),
        "sha256": file_sha256(staged),
        "member_count": members,
        "uncompressed_size": raw_size,
        "compressed_size": staged.stat().st_size,
    }
    return staged, entry


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


def pull_archive(
    src_dir: str,
    folder: Path,
    arch: dict[str, object],
    spec: str | None,
    *,
    verb: str,
    gerund: str,
    as_json: bool,
) -> str:
    """Fetch one recorded object and unpack it into folder. Returns its name.

    The checksum is verified before anything touches the folder, so a corrupt
    or truncated object leaves an offloaded folder as a clean tombstone rather
    than half-overwritten -- and the remote copy is still there to retry, or to
    fall back from with --version 2.
    """
    versions = archive_versions(arch)
    entry = select_version(arch, spec)
    name = _mstr(entry, "name")
    # The copy behind this one, if --keep left one, is what a bad object falls
    # back to. Only worth suggesting when it actually exists.
    older = versions.index(entry) + 2 if versions.index(entry) + 1 < len(versions) else None
    codec = _mstr(entry, "format") or _mstr(arch, "format")
    if codec not in ARCHIVE_FORMATS:
        raise die(f"{name} names an archive format this build cannot read: {codec!r}", code=2)
    target = remote_archive_object(src_dir, name)

    with tempfile.TemporaryDirectory(prefix="gwarchive-") as tmp:
        staged = Path(tmp) / name
        rclone_or_die(
            ["copyto", target, str(staged)],
            f"{verb} {folder.name} from {target}",
            status=None if as_json else f"{gerund} {folder.name} {symbol('transfer')} local",
            fix=f"rclone lsd {remote_root_of(target)}",
        )
        recorded = _mstr(entry, "sha256")
        if not recorded:
            warn(f"{name} carries no recorded checksum; extracting it unverified")
        else:
            actual = file_sha256(staged)
            if actual != recorded:
                prefix = folder_prefix(folder.name) or folder.name
                raise die(
                    f"{name} does not match the checksum recorded for it.\n"
                    f"expected {recorded}\ngot      {actual}",
                    fix=(
                        f"g.py {verb} {prefix} --version {older}"
                        if older
                        else f"rclone lsl {remote_root_of(target)}   # this is the only recorded copy"
                    ),
                )
        written = extract_archive(staged, folder, codec)

    expected = _mint(entry, "member_count")
    if expected is not None and written != expected:
        warn(f"{folder.name}: extracted {written} member(s) but {name} records {expected}")
    return name


def determine_folder_category(folder: Path, base_path: Path) -> str:
    try:
        rel_parts = folder.relative_to(base_path).parts
        if rel_parts and rel_parts[0] in CATEGORY_NAMES:
            return rel_parts[0]
    except ValueError:
        pass
    pfx = folder_prefix(folder.name)
    if pfx and pfx[0] in CATEGORIES:
        return CATEGORIES[pfx[0]]
    return "Archive"


def resolve_sync_targets(selector: str, base_path: Path, command: str = "push") -> list[tuple[str, Path]]:
    """Resolve a sync selector (category letter/name or prefix/path) to folders."""
    letter = category_letter_for(selector)
    if letter is not None:
        cat_name = CATEGORIES[letter]
        cat_dir = base_path / cat_name
        if not cat_dir.exists():
            raise die(f"No {cat_name} directory at {base_path}", fix=f"g.py init --path {base_path}")
        targets = iter_archive_folders(base_path, cat_name)
        if not targets:
            raise die(
                f"No folders found in category {cat_name}",
                fix=f'g.py create {letter} "Name" --path {base_path}',
            )
        return targets

    source_path = locate_source(selector, base_path)
    if not source_path.is_dir():
        raise die(f"Target is not a directory: {source_path}")

    # Sync operates on whole top-level folders only. A subfolder pushed on its
    # own would land flattened at <remote>/<Category>/<name>, and list/stats/
    # verify never look below the top level, so its tombstone would be
    # invisible to every scanner.
    try:
        parts = source_path.resolve().relative_to(base_path.resolve()).parts
    except ValueError:
        parts = ()
    if len(parts) != 2 or parts[0] not in CATEGORY_NAMES:
        sub_match = SUBFOLDER_RE.match(source_path.name)
        parent = sub_match.group(1) if sub_match else selector
        raise die(
            f"Sync works on top-level archive folders only; {source_path.name} is not one.",
            fix=f"g.py {command} {parent}",
        )

    cat_name = determine_folder_category(source_path, base_path)
    return [(cat_name, source_path)]


def carry_remotes(prev: Sequence[str], folder: Path, base_path: Path) -> list[str]:
    """The recorded remotes worth carrying into the next transfer.

    Metadata written by a laxer version can name a stray local directory; drop
    those, so one bad entry does not outlive the transfer that replaces it. A
    fresh list -- the per-remote loop appends to it as each copy lands.
    """
    kept = [r for r in prev if is_remote_target(r)]
    if len(kept) != len(prev):
        dropped = len(prev) - len(kept)
        shown = display(folder, base_path)
        warn(f"{shown}: ignoring {plural(dropped, 'recorded remote')} rclone would read as local")
    return kept


def offload_summary(
    measured: Sequence[tuple[str, Path, int, int]],
    remotes: Sequence[str],
    base_path: Path,
    codec: str = "",
) -> Text:
    """What is about to be deleted locally, and how much disk that returns.

    The freed figure is the number the decision actually turns on, so it gets
    its own line rather than being left for the user to add up. When the
    transfer is compressed, so does the scratch space it needs first -- that is
    a cost the user should see before answering, not discover mid-run.
    """
    count = len(measured)
    freed = sum(size for _, _, size, _ in measured)
    width = max((len(display(folder, base_path)) for _, folder, _, _ in measured), default=0)

    summary = Text()
    summary.append(
        f"Offload {count} folder{'' if count == 1 else 's'} and DELETE the local files\n\n",
        style="bold",
    )
    for _, folder, size, _ in measured:
        summary.append(f"{display(folder, base_path):<{width}}  ")
        summary.append(f"{decimal(size):>9}\n", style=STYLES["label"])
    summary.append(f"\n{'frees':<{LABEL_WIDTH}}", style=STYLES["label"])
    summary.append(decimal(freed))
    if codec:
        # The scratch requirement is the largest single folder: they are staged
        # one at a time. Shown as its own line because it is a real cost, and
        # the volume it lands on ($TMPDIR) may not be the one being freed.
        summary.append(f"\n{'packs':<{LABEL_WIDTH}}", style=STYLES["label"])
        summary.append(f"one {codec} object per folder")
        summary.append(f"\n{'needs':<{LABEL_WIDTH}}", style=STYLES["label"])
        summary.append(
            f"{decimal(max((size for _, _, size, _ in measured), default=0))} of scratch in $TMPDIR"
        )
    summary.append(f"\n{'to':<{LABEL_WIDTH}}", style=STYLES["label"])
    summary.append(", ".join(remotes))
    return summary


# --- Parameter validation ------------------------------------------------------


def validate_category(value: str | None) -> str | None:
    """Accept a category letter in any case; reject anything else loudly.

    An invalid category must never fall through to "search everything" -- a
    silent wrong answer is worse than an error.
    """
    if value is None:
        return None
    candidate = value.strip().upper()
    if candidate not in CATEGORIES:
        raise typer.BadParameter(
            f"{value!r} is not a category. Choose one of: "
            + ", ".join(f"{k} ({v})" for k, v in CATEGORIES.items())
        )
    return candidate


def version_callback(value: bool) -> None:
    if value:
        print(f"gwarchive {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=version_callback, is_eager=True, help="Show the version and exit"
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress success output; errors still go to stderr"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show extra detail"),
) -> None:
    """GWArchive CLI tool for file organization."""
    output.set_verbosity(quiet, verbose)


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

BasePath = Annotated[
    Path,
    typer.Option("--path", default_factory=get_base_path, show_default=False, help="Base path for GWArchive"),
]
DryRun = Annotated[bool, typer.Option("--dry-run", help="Show what would happen without making changes")]
# Three --json wordings, because there are three output shapes to replace.
AsJsonTable = Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table")]
AsJsonReport = Annotated[bool, typer.Option("--json", help="Emit JSON instead of a report")]
AsJsonSync = Annotated[bool, typer.Option("--json", help="Emit JSON output")]
Force = Annotated[bool, typer.Option("--force", help="Replace the destination if it already exists")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")]
CategoryArg = Annotated[
    str,
    typer.Argument(callback=validate_category, metavar=CATEGORY_METAVAR, help="Category (P, R, M, A, O)"),
]
CategoryOpt = Annotated[
    str | None,
    typer.Option(
        "--category",
        "-c",
        callback=validate_category,
        metavar=CATEGORY_METAVAR,
        help="Limit to one category (P, R, M, A, O)",
    ),
]
SourceArg = Annotated[str, typer.Argument(help="Source prefix or path")]
Selector = Annotated[str, typer.Argument(help="Category (P, R, M, A, O) or folder prefix/path")]
PushRemotes = Annotated[
    list[str] | None,
    typer.Option(
        "--remote", help="Remote destination, e.g. nas:archive (default $GWARCHIVE_REMOTE). Can be repeated."
    ),
]
PullRemote = Annotated[
    str | None,
    typer.Option("--remote", help="Remote source (defaults to recorded remote or $GWARCHIVE_REMOTE)"),
]
ArchiveVersion = Annotated[
    str | None,
    typer.Option("--version", help="Archived copy to fetch: 1 is newest (default), or an object name"),
]
Keep = Annotated[
    int | None,
    typer.Option("--keep", help="Archived copies to retain per remote (default 1; 0 keeps every copy)"),
]
NoCompressPush = Annotated[
    bool, typer.Option("--no-compress", help="Copy files individually instead of one compressed archive")
]
NoCompressPull = Annotated[
    bool, typer.Option("--no-compress", help="Copy files individually, ignoring any recorded archive")
]


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
    codec = pick_codec() if compress_default() and not no_compress else ""
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
    codec = pick_codec() if compress_default() and not no_compress else ""
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


def resolve_fetch_source(
    remote: str | None,
    prev_remotes: Sequence[str],
    category: str,
    folder_name: str,
    command: str,
) -> str:
    """Where a pull or a restore reads from.

    An explicit --remote wins. Otherwise the tombstone's ``remotes`` list is a
    historical union that only ever grows, so the *last* usable entry is the
    current one -- see AGENTS.md, "Retention". Entries a laxer version recorded
    can be bare local paths; only a list with nothing usable in it is fatal.
    """
    if remote:
        return remote_folder_target(remote, category, folder_name)
    if prev_remotes:
        usable = [r for r in prev_remotes if is_remote_target(r)]
        if usable:
            return usable[-1]
        return validate_remote_target(str(prev_remotes[0]), f"recorded in {TOMBSTONE_NAME}", command)
    return remote_folder_target(get_remotes(None, command)[0], category, folder_name)


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
    if run_pokeget([chosen, "--hide-name"]) is None:
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
