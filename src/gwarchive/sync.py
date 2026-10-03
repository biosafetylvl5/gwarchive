"""Transfer policy: staging, scratch space, retention defaults, target resolution.

ensure_scratch_space sizes the need as payload bytes PLUS 512 per member,
because tar headers dominate a tree of tiny files: 100,000 empty files measured
as need=0, so the guard passed with one byte free and the write then died on
ENOSPC.

offload writes its tombstone BEFORE deleting local files, so an interrupted
offload reads as offloaded-with-leftovers rather than a silently emptied
folder. The ordering is the safety property; it is not incidental.
"""

import contextlib
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from rich.filesize import decimal
from rich.text import Text

from gwarchive import clock, tarball
from gwarchive.destination import locate_source
from gwarchive.external import (
    get_remotes,
    is_remote_target,
    rclone_or_die,
    remote_folder_target,
    remote_root_of,
    validate_remote_target,
)
from gwarchive.naming import (
    CATEGORIES,
    CATEGORY_NAMES,
    SUBFOLDER_RE,
    TOMBSTONE_NAME,
    _reserved_path,
    category_letter_for,
    folder_prefix,
)
from gwarchive.output import (
    LABEL_WIDTH,
    STYLES,
    die,
    plural,
    program,
    spinner,
    symbol,
    warn,
)
from gwarchive.paths import display, iter_archive_folders
from gwarchive.tarball import (
    ARCHIVE_FORMATS,
    archive_object_name,
    archive_versions,
    file_sha256,
    remote_archive_object,
    select_version,
)
from gwarchive.tombstone import (
    _mint,
    _mstr,
    read_tombstone_state,
)


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
            fix=f"TMPDIR=/some/larger/volume {program()} offload P1",
        )


def compress_default() -> bool:
    """On by default: one object beats thousands.

    ``$GWARCHIVE_COMPRESS=0`` per shell, ``--no-compress`` per command.
    """
    return os.environ.get("GWARCHIVE_COMPRESS", "").strip().lower() not in ("0", "false", "no", "off")


def keep_default(keep: int | None, prior_archive: dict[str, object] | None = None) -> int:
    """--keep, else $GWARCHIVE_KEEP, else this folder's own recorded keep, else 1.

    Falling back past a value the folder already has on record is how a plain
    push after ``push P1 --keep 5`` used to silently re-prune to 1: nothing
    ever read ``archive["keep"]`` back. ``prior_archive`` is that folder's
    existing archive block, not the whole tombstone -- callers already have it
    on hand from the metadata they just read.
    """
    if keep is not None:
        return max(keep, 0)
    raw = os.environ.get("GWARCHIVE_KEEP", "").strip()
    if raw:
        if not (raw.isascii() and raw.isdecimal()):
            # Falling back to 1 here means "delete all but one copy" -- the
            # opposite of what somebody raising the retention meant to ask for.
            raise die(
                f"$GWARCHIVE_KEEP is not a whole number: {raw!r}",
                code=2,
                fix=f"GWARCHIVE_KEEP=2 {program()} push P1",
            )
        return int(raw)
    recorded = _mint(prior_archive, "keep") if prior_archive is not None else None
    if recorded is not None and recorded >= 0:
        return recorded
    return 1


def stage_archive(folder: Path, tmpdir: Path, codec: str, *, as_json: bool) -> tuple[Path, dict[str, object]]:
    """Write the folder's archive into tmpdir; return it and its version record.

    The spinner is not decoration: compression is the slow step and it runs
    before any rclone call, so without one a speed-up reads as a freeze.
    """
    name = archive_object_name(folder.name, clock.archive_stamp(), codec)
    staged = tmpdir / name
    with spinner(f"Compressing {folder.name}", as_json=as_json):
        members, raw_size = tarball.create_archive(folder, staged, codec)
    entry: dict[str, object] = {
        "name": name,
        "format": codec,
        "pushed_at": clock.now_stamp(),
        "sha256": file_sha256(staged),
        "member_count": members,
        "uncompressed_size": raw_size,
        "compressed_size": staged.stat().st_size,
        # Populated as each remote copy actually succeeds, not with every
        # remote this push was asked for -- see prune_versions.
        "remotes": [],
    }
    return staged, entry


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
                        f"{program()} {verb} {prefix} --version {older}"
                        if older
                        else f"rclone lsl {remote_root_of(target)}   # this is the only recorded copy"
                    ),
                )
        written = tarball.extract_archive(staged, folder, codec)

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
            raise die(f"No {cat_name} directory at {base_path}", fix=f"{program()} init --path {base_path}")
        targets = iter_archive_folders(base_path, cat_name)
        if not targets:
            raise die(
                f"No folders found in category {cat_name}",
                fix=f'{program()} create {letter} "Name" --path {base_path}',
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
            fix=f"{program()} {command} {parent}",
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


def resolve_fetch_source(
    remote: str | None,
    prev_remotes: Sequence[str],
    category: str,
    folder_name: str,
    command: str,
    version_remotes: Sequence[str] | None = None,
) -> str:
    """Where a pull or a restore reads from.

    An explicit --remote wins. Otherwise the selected VERSION's own recorded
    remotes take priority over the folder's historical union: with per-version
    retention, an older kept copy can live only on a remote a later push never
    touched, and the folder-level list alone would send ``--version 2`` to a
    directory that never had it. The folder-level ``remotes`` list is a
    historical union that only ever grows, so the *last* usable entry there is
    the fallback. Entries a laxer version recorded can be bare local paths;
    only a list with nothing usable in it is fatal.
    """
    if remote:
        return remote_folder_target(remote, category, folder_name)
    if version_remotes:
        usable = [r for r in version_remotes if is_remote_target(r)]
        if usable:
            return usable[-1]
    if prev_remotes:
        usable = [r for r in prev_remotes if is_remote_target(r)]
        if usable:
            return usable[-1]
        return validate_remote_target(str(prev_remotes[0]), f"recorded in {TOMBSTONE_NAME}", command)
    return remote_folder_target(get_remotes(None, command)[0], category, folder_name)


def require_tombstone(folder: Path, base_path: Path) -> dict[str, object]:
    """The tombstone, refusing to treat corrupt as absent.

    ``read_tombstone_state``'s whole point is that distinction -- collapsing a
    corrupt or non-object tombstone back to "absent" here would re-introduce
    exactly what it exists to prevent: push, offload, pull and restore would
    all treat a damaged version index as a folder that was never backed up,
    and overwrite it.
    """
    state, data = read_tombstone_state(folder)
    if state in ("corrupt", "not-object"):
        shown = display(folder, base_path)
        # verify takes no folder argument; it reports every tombstone at once.
        raise die(
            f"{shown}'s {TOMBSTONE_NAME} is {state} and cannot be trusted as a version index.\n"
            f"Repair {shown}/{TOMBSTONE_NAME} by hand; nothing has been changed.",
            code=2,
            fix=f"{program()} verify",
        )
    return data or {}


def snapshot_manifest(folder: Path) -> dict[str, tuple[int, int]]:
    """relative path -> (size, mtime_ns) for every non-reserved file, via lstat.

    Taken before packing or copying, which can take a while, so a file created
    or changed during the transfer is never deleted sight-unseen afterwards --
    only a path whose size and mtime still match this snapshot is.
    """
    manifest: dict[str, tuple[int, int]] = {}
    for root, dirs, files in os.walk(folder, followlinks=False):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if not _reserved_path(root_path / d, folder)]
        for name in files:
            path = root_path / name
            if _reserved_path(path, folder):
                continue
            try:
                st = path.lstat()
            except OSError:
                continue
            manifest[str(path.relative_to(folder))] = (st.st_size, st.st_mtime_ns)
    return manifest


def delete_archived_contents(folder: Path, manifest: dict[str, tuple[int, int]]) -> list[str]:
    """Delete what the manifest says was captured and is still unchanged.

    Bottom-up and never ``rmtree``: a reserved path is skipped at ANY depth
    (the same rule ``create_archive`` prunes by), a changed or new file is
    left behind and reported rather than guessed about, and a directory is
    removed only once ``rmdir`` proves it is actually empty -- which it is not
    when something reserved or kept still lives under it.
    """
    kept: list[str] = []
    for root, dirs, files in os.walk(folder, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            path = root_path / name
            if _reserved_path(path, folder):
                continue
            rel = str(path.relative_to(folder))
            try:
                st = path.lstat()
            except OSError:
                continue
            if manifest.get(rel) == (st.st_size, st.st_mtime_ns):
                path.unlink()
            else:
                kept.append(rel)
        for name in dirs:
            path = root_path / name
            if _reserved_path(path, folder) or path.is_symlink():
                continue
            with contextlib.suppress(OSError):
                path.rmdir()  # not empty: something reserved or kept still lives under it
    return kept


def overwrite_summary(entries: Sequence[tuple[Path, int]], base_path: Path) -> Text:
    """Which folders hold local files a fetch is about to overwrite, and how many.

    One prompt for the whole batch rather than one per folder, so declining
    does not require sitting through every folder that came before the one you
    actually wanted to stop at.
    """
    width = max((len(display(folder, base_path)) for folder, _ in entries), default=0)
    summary = Text()
    summary.append(
        "Local files the remote also has will be overwritten\n\n",
        style="bold",
    )
    for folder, count in entries:
        summary.append(f"{display(folder, base_path):<{width}}  ")
        summary.append(f"{plural(count, 'local file')}\n", style=STYLES["label"])
    return summary
