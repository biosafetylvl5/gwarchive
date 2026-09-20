"""pull and restore -- two thin wrappers over one fetch_folders body.

These ARE merged, for the opposite reason to push/offload: they had drifted
byte-for-byte apart once, comment included, and three of the four things only
one of them did were bugs in the other. Here the shared body is the guarantee."""

from pathlib import Path

from gwarchive import clock
from gwarchive.external import (
    RCLONE_EXCLUDES,
    rclone_or_die,
    remote_root_of,
    validate_remote_target,
)
from gwarchive.naming import (
    folder_prefix,
)
from gwarchive.options import (
    ArchiveVersion,
    AsJsonSync,
    BasePath,
    DryRun,
    NoCompressPull,
    PullRemote,
    Selector,
    app,
)
from gwarchive.output import (
    caption,
    die,
    emit_json,
    note,
    ok,
    program,
    remote_message,
    symbol,
    warn,
)
from gwarchive.paths import (
    compute_folder_stats,
    display,
)
from gwarchive.sync import (
    pull_archive,
    resolve_fetch_source,
    resolve_sync_targets,
)
from gwarchive.tarball import (
    archive_meta_of,
    select_version,
)
from gwarchive.tombstone import (
    get_recorded_remotes,
    read_tombstone,
    write_tombstone,
)


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
                fix=f"{program()} {command} {folder_prefix(folder.name) or selector}",
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
