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

import typer
from rich.filesize import decimal
from rich.text import Text

from gwarchive import clock, gitrepo, tarball
from gwarchive.destination import locate_source
from gwarchive.external import (
    get_remotes,
    is_remote_target,
    rclone_or_die,
    remote_folder_target,
    remote_root_of,
    validate_remote_target,
)
from gwarchive.forge import GitConfig, load_config, url_host
from gwarchive.gitrepo import FolderGit, GitRun, Repo
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
    caption,
    confirm_destructive,
    detail,
    die,
    error,
    hint,
    interactive,
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
    *,
    scratch: int | None = None,
    git_lines: Sequence[str] = (),
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
        if scratch is None:
            scratch = max((size for _, _, size, _ in measured), default=0)
        summary.append(f"{decimal(scratch)} of scratch in $TMPDIR")
    summary.append(f"\n{'to':<{LABEL_WIDTH}}", style=STYLES["label"])
    summary.append(", ".join(remotes))
    for i, line in enumerate(git_lines):
        summary.append(f"\n{'git' if i == 0 else '':<{LABEL_WIDTH}}", style=STYLES["label"])
        summary.append(line)
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


# --- the git server --------------------------------------------------------------
#
# push and offload call these in the same order: git_setup before anything,
# git_preflight before any prompt or transfer, git_step per folder BEFORE
# offload's snapshot_manifest, then the receipt row and the repository lines.
# The rules behind that order are in gitrepo's docstring.


def git_setup(*, no_git: bool, force_git: bool, yes: bool, as_json: bool, retry: str) -> GitConfig | None:
    """Every setup check, before any transfer. None: no git step this run.

    ``--force-git --json`` without ``--yes`` is refused whether or not anything
    would be refused: checked only on a refusal, a cron line would work every
    night until the first divergence, then lose that night's backup.
    """
    if no_git:
        return None
    if force_git and as_json and not yes:
        raise die(
            "--force-git asks before overwriting, and a --json run cannot answer.",
            code=2,
            fix=f"{retry} --force-git --json --yes",
        )
    return load_config()


def git_preflight(
    cfg: GitConfig,
    folders: list[tuple[str, Path]],
    *,
    wip: bool,
    force_git: bool,
    as_json: bool,
    offload: bool,
) -> GitRun:
    """The read-only half, for the whole batch. A run-wide failure is reported here, once."""
    host = url_host(cfg.url)
    with spinner(f"Checking repositories on {host}", as_json=as_json):
        run = gitrepo.preflight(
            cfg,
            folders,
            wip=wip,
            force_git=force_git,
            unattended=not interactive(as_json),
            offload=offload,
        )
    if run.error and not offload:
        warn(f"git server {host}: {run.error}. The rclone copy goes ahead; no repository is pushed.")
    return run


def _short(ref: str) -> str:
    if ref.startswith("refs/tags/"):
        return "tag " + ref.removeprefix("refs/tags/")
    return ref.removeprefix("refs/heads/")


def _label(fg: FolderGit, repo: Repo) -> str:
    prefix = folder_prefix(fg.folder.name) or fg.folder.name
    return prefix if repo.rel == "." else f"{prefix} {repo.rel}"


def _refused_text(refs: Sequence[gitrepo.Ref]) -> str:
    """``diverged main, dev; unknown release``, grouped by state."""
    groups: dict[str, list[str]] = {}
    for ref in refs:
        groups.setdefault(ref.state, []).append(_short(ref.ref))
    text = "; ".join(f"{state} {', '.join(names)}" for state, names in groups.items())
    if "unknown" in groups:
        text += " (the server has commits this repository never fetched)"
    return text


def _forced(repo: Repo) -> list[gitrepo.Ref]:
    return [r for r in repo.refs if r.action == "forced"]


def _outcome(repo: Repo) -> str:
    if repo.skipped:
        return "skipped"
    if repo.error:
        return "error"
    if repo.refused():
        return "refused"
    if repo.wip or any(r.action in ("pushed", "forced") for r in repo.refs):
        return "pushed"
    return "up to date"


def _counts(repos: Sequence[Repo], dry_run: bool) -> str:
    tally: dict[str, int] = {}
    for repo in repos:
        tally[_outcome(repo)] = tally.get(_outcome(repo), 0) + 1
    created = sum(1 for r in repos if r.created and not r.error)
    dirty = sum(1 for r in repos if r.dirty and not r.wip and not r.skipped)
    parts = [
        f"{tally['pushed']} {'to push' if dry_run else 'pushed'}" if tally.get("pushed") else "",
        f"{tally['up to date']} up to date" if tally.get("up to date") else "",
        f"{created} {'to create' if dry_run else 'created'}" if created else "",
        f"{tally['refused']} refused" if tally.get("refused") else "",
        f"{plural(tally['error'], 'error')}" if tally.get("error") else "",
        f"{dirty} dirty" if dirty else "",
        f"{tally['skipped']} skipped" if tally.get("skipped") else "",
    ]
    return ", ".join(p for p in parts if p)


def git_row(run: GitRun | None, folder: Path, *, dry_run: bool) -> list[tuple[str, str]]:
    """The receipt's ``git`` row, or no row when there is no git step."""
    if run is None:
        return []
    where = f"{url_host(run.cfg.url)}/{run.cfg.owner}"
    if run.error:
        return [("git", f"{where}  failed: see above")]
    fg = run.folders.get(folder)
    if fg is None or not fg.repos:
        return [("git", f"{where}  no repositories")]
    text = f"{where}  {plural(len(fg.repos), 'repository', 'repositories')}: {_counts(fg.repos, dry_run)}"
    if any(r.dirty and not r.wip and not r.skipped for r in fg.repos):
        text += "  (add --wip)"
    if fg.failed():
        text += "; see below"
    return [("git", text)]


def git_repo_lines(run: GitRun | None, folder: Path, *, retry: str, dry_run: bool, as_json: bool) -> None:
    """The lines after a folder's receipt, only for states that need the user.

    Refusals and errors go to stderr through warn, so they survive --quiet and
    --json; everything else is stdout chrome, which --json owns.
    """
    if run is None or run.error:
        return
    fg = run.folders.get(folder)
    if fg is None:
        return
    owner = run.cfg.owner
    for repo in fg.repos:
        label = _label(fg, repo)
        if repo.skipped:
            if not as_json:
                detail(f"{label}: skipped -- {repo.skipped}")
            continue
        if repo.error:
            warn(f"{label}: {repo.error}")
        elif repo.refused():
            warn(f"{label}: {_refused_text(repo.refused())}")
        if repo.error or repo.refused():
            for line in gitrepo.hint_lines(repo, run, retry):
                hint(line)
        if repo.lfs == "missing":
            warn(
                f"{label}: uses Git LFS, but git-lfs is not installed; "
                "its LFS objects are in the rclone copy only"
            )
        if repo.error or as_json:
            continue
        if repo.created:
            caption(f"{label}: {'would create' if dry_run else 'created'} {owner}/{repo.repo}")
        if _forced(repo):
            verb = "would overwrite" if dry_run else "overwrote"
            caption(f"{label}: {verb} {', '.join(_short(r.ref) for r in _forced(repo))} on the server")
        behind = [_short(r.ref) for r in repo.refs if r.state == "behind"]
        if behind:
            caption(f"{label}: the server is ahead on {', '.join(behind)}; nothing to push there")
        if repo.archive_note:
            caption(f"{label}: {repo.archive_note} {owner}/{repo.repo}")
        if repo.wip and repo.untracked:
            shown = ", ".join(repo.untracked[:10])
            more = f" (+{len(repo.untracked) - 10} more)" if len(repo.untracked) > 10 else ""
            caption(f"{label}: {repo.wip} includes untracked {shown}{more}")
        if not (repo.created or behind or repo.archive_note or repo.refused()):
            detail(f"{label}: {_outcome(repo)} as {owner}/{repo.repo}")


def git_batch_summary(run: GitRun | None, *, dry_run: bool) -> None:
    """One line across a batch of more than one folder."""
    if run is None or run.error or len(run.folders) < 2:
        return
    repos = [r for fg in run.folders.values() for r in fg.repos]
    if repos:
        caption(
            f"git: {plural(len(repos), 'repository', 'repositories')} in {len(run.folders)} folders: "
            f"{_counts(repos, dry_run)}"
        )


def _overwrite_lines(run: GitRun) -> list[str]:
    lines = []
    for fg in run.folders.values():
        for repo in fg.repos:
            forced = _forced(repo)
            if forced:
                names = ", ".join(
                    _short(r.ref) + (" (unknown: never fetched)" if r.state == "unknown" else "")
                    for r in forced
                )
                lines.append(f"overwrites {run.cfg.owner}/{repo.repo}: {names}")
    return lines


def _dirty_lines(run: GitRun) -> list[str]:
    return [
        f"{_label(fg, repo)} has uncommitted changes. They will be in the rclone copy only; "
        "add --wip to also keep them on the server."
        for fg in run.folders.values()
        for repo in fg.active()
        if repo.dirty and not repo.wip
    ]


def confirm_force_git(run: GitRun | None, *, yes: bool, dry_run: bool) -> None:
    """push's one prompt: only when --force-git has something to overwrite.

    Declined, the refs go back to refused and the run carries on.
    """
    if run is None or dry_run:
        return
    lines = _overwrite_lines(run)
    if not lines:
        return
    summary = Text()
    summary.append("Overwrite refs on the git server\n\n", style="bold")
    summary.append("\n".join(lines))
    if yes:
        for line in lines:
            warn(line)
    if not confirm_destructive(summary, "Overwrite them?", yes=yes):
        gitrepo.declined(run)


def git_summary_lines(run: GitRun | None) -> list[str]:
    """offload's confirmation additions. Printed through warn as well under --yes."""
    if run is None:
        return []
    repos = [r for fg in run.folders.values() for r in fg.repos]
    lines = [f"{url_host(run.cfg.url)}/{run.cfg.owner}  {plural(len(repos), 'repository', 'repositories')}"]
    there = sum(1 for r in repos if r.skipped and r.skipped.startswith("already there"))
    if there:
        lines.append(
            f"{plural(there, 'repository', 'repositories')} already there, not checked against the server: "
            "commits not yet on it will be in the rclone copy only"
        )
    return lines + _overwrite_lines(run) + _dirty_lines(run)


def warn_unattended(run: GitRun | None, *, yes: bool) -> None:
    """--yes renders no summary, so the lines a scheduled run's log needs go to stderr."""
    if run is None or not yes:
        return
    for line in _overwrite_lines(run) + _dirty_lines(run):
        warn(line)


def wip_bytes(run: GitRun | None, folder: Path) -> int:
    """What --wip will write into the folder's .git: an upper bound, before compression."""
    fg = run.folders.get(folder) if run else None
    return sum(r.changed_bytes for r in fg.active() if r.wip) if fg else 0


def git_refusal(run: GitRun, retry: str) -> typer.Exit:
    """offload's whole-batch refusal: every affected repository, then the ways forward."""
    error("Cannot offload -- the git server would not hold every repository, so nothing was transferred.")
    if run.error:
        warn(f"git server {url_host(run.cfg.url)}: {run.error}")
        hint(f"{retry} --no-git   # rclone copy only; the server keeps what it has")
        return typer.Exit(1)
    fetches: list[str] = []
    fixes: list[str] = []
    refused = unknown = False
    for fg in run.folders.values():
        for repo in fg.repos:
            if repo.error:
                warn(f"{_label(fg, repo)}: {repo.error}")
                fixes.extend(repo.fixes)
            elif repo.refused():
                warn(f"{_label(fg, repo)}: {_refused_text(repo.refused())}")
                refused = True
                unknown = unknown or any(r.state == "unknown" for r in repo.refused())
                fetches.extend(line for line in gitrepo.hint_lines(repo, run, retry) if " fetch " in line)
    for line in fetches:
        hint(f"{line}   # unknown refs: see what the server has first")
    hint(f"{retry} --no-git   # rclone copy only; the server keeps what it has")
    if refused:
        note = ", including commits never fetched" if unknown else ""
        hint(f"{retry} --force-git   # overwrite the refused server refs{note}")
    for line in fixes:
        hint(line)
    return typer.Exit(1)


def git_step(
    run: GitRun | None, folder: Path, existing_meta: dict[str, object], *, as_json: bool
) -> dict[str, object] | None:
    """The git writes for one folder, and the tombstone's new ``git`` record.

    None means keep the previous record untouched: no server, --no-git, a
    run-wide failure, or a folder that has never had a repository.
    """
    if run is None or run.error or run.forge is None:
        return None
    fg = run.folders.get(folder)
    if fg is None:
        return None
    raw = existing_meta.get("git")
    prev = raw if isinstance(raw, dict) else None
    if not fg.active() and prev is None:
        return None
    label = (
        None
        if as_json or not fg.active()
        else f"Pushing repositories of {folder.name} {symbol('transfer')} {url_host(run.cfg.url)}"
    )
    with spinner(label, as_json=as_json):
        gitrepo.apply(run, fg, prev)
    return gitrepo.record(run, fg, prev)
