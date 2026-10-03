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
    RestoreKeepLocal,
    Selector,
    Yes,
    app,
)
from gwarchive.output import (
    caption,
    confirm_destructive,
    die,
    emit_json,
    note,
    ok,
    plural,
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
    overwrite_summary,
    pull_archive,
    require_tombstone,
    resolve_fetch_source,
    resolve_sync_targets,
)
from gwarchive.tarball import (
    archive_meta_of,
    select_version,
)
from gwarchive.tombstone import (
    _mint,
    get_recorded_remotes,
    write_tombstone,
)


def _keep_local(
    selector: str,
    base_path: Path,
    *,
    yes: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """restore --keep-local: clear offloaded_at using what is already on disk.

    Fetches nothing -- the point is to resolve an offloaded-with-local-files
    folder (pulled and edited, or an interrupted offload's leftovers) without
    choosing for the user which copy is right.

    Fewer local files than the tombstone recorded at offload time looks like
    leftovers rather than a full pull, and marking a partial folder live is
    how the next push (keep=1) would replace the only complete archive with
    it. That case needs --yes.
    """
    targets = resolve_sync_targets(selector, base_path, "restore")
    results: list[dict[str, object]] = []

    for cat_name, folder in targets:
        shown = display(folder, base_path)
        existing_meta = require_tombstone(folder, base_path)
        offloaded = bool(existing_meta.get("offloaded_at"))
        if not offloaded:
            if len(targets) > 1:
                if not as_json:
                    caption(f"Skipped {shown} -- not offloaded")
                results.append({"folder": shown, "category": cat_name, "skipped": "not-offloaded"})
                continue
            raise die(f"{shown} is not offloaded; --keep-local has nothing to clear.")

        local_count = compute_folder_stats(folder)[1]
        if not local_count:
            raise die(
                f"{shown} holds no local files; --keep-local would mark it restored with nothing there.",
                fix=f"{program()} restore {folder_prefix(folder.name) or shown}",
            )
        recorded_count = _mint(existing_meta, "file_count")
        if recorded_count is not None and local_count < recorded_count and not yes:
            raise die(
                f"{shown} holds {plural(local_count, 'local file')}, but {recorded_count} were offloaded.\n"
                "This looks like the leftovers of an interrupted offload, not a full copy. Marking it "
                "live and pushing it would replace the complete archive with a partial one.",
                fix=(
                    f"{program()} restore {folder_prefix(folder.name) or shown}"
                    "   # fetch the archive; add --keep-local --yes only if the partial copy is what you want"
                ),
            )

        if dry_run:
            if not as_json:
                note(f"Would clear offloaded status on {shown}, keeping its local files")
        else:
            meta = dict(existing_meta)
            meta.pop("offloaded_at", None)
            meta["restored_at"] = clock.now_stamp()
            write_tombstone(folder, meta)
            if not as_json:
                ok(f"{shown}: kept the local files, cleared offloaded status")

        results.append(
            {
                "folder": shown,
                "prefix": folder_prefix(folder.name),
                "category": cat_name,
                "restored": not dry_run,
                "dry_run": dry_run,
            }
        )

    if as_json:
        emit_json(results)


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
    keep_local: bool = False,
    yes: bool = False,
) -> None:
    """The body of both pull and restore.

    They differ in three strings and one flag. Four things only one of them
    did, and in three cases it was the wrong one -- see AGENTS.md, "Commands".
    """
    if keep_local:
        if remote or version or no_compress:
            # Each of these shapes a fetch, and --keep-local fetches nothing;
            # accepting them would report success for options that did nothing.
            raise die(
                "--keep-local fetches nothing, so --remote, --version and --no-compress do not apply.", code=2
            )
        _keep_local(selector, base_path, yes=yes, dry_run=dry_run, as_json=as_json)
        return

    targets = resolve_sync_targets(selector, base_path, command)
    if remote:
        remote = validate_remote_target(remote, command=command)

    # Read every target's state once, before anything is fetched: the
    # confirmation below needs to know the whole batch's local-file exposure
    # up front, not folder by folder as each one is about to be clobbered.
    Prepared = tuple[str, Path, dict[str, object], bool, list[str], "dict[str, object] | None", int, bool]
    prepared: list[Prepared] = []
    for cat_name, folder in targets:
        existing_meta = require_tombstone(folder, base_path)
        offloaded = bool(existing_meta.get("offloaded_at"))
        prev_remotes = get_recorded_remotes(existing_meta)
        arch = None if no_compress else archive_meta_of(existing_meta)
        if version and not arch:
            # Silently ignoring it meant `--no-compress --version 3` did a
            # plain mirror copy and exited 0 reporting success.
            shown = display(folder, base_path)
            raise die(
                f"--version needs a recorded archive; {shown} has none.",
                code=2,
                fix=f"{program()} {command} {folder_prefix(folder.name) or selector}",
            )
        _, file_count = compute_folder_stats(folder)
        will_skip = clear_offloaded and not offloaded and len(targets) > 1
        prepared.append(
            (cat_name, folder, existing_meta, offloaded, prev_remotes, arch, file_count, will_skip)
        )

    # Extraction rewrites every member it carries, and a --no-compress copy
    # replaces anything rclone considers changed -- neither path is the
    # gentler one, and both overwrite a local edit the remote does not know
    # about. One confirmation covers the whole batch rather than one per
    # folder, which used to warn after it had already extracted over some of
    # them.
    overwrite_targets: list[tuple[Path, int]] = [
        (folder, file_count)
        for _, folder, _, _, _, _, file_count, will_skip in prepared
        if file_count and not will_skip
    ]
    if overwrite_targets and not dry_run:
        if as_json:
            if not yes:
                raise die(
                    "Refusing to overwrite local files under --json without --yes.",
                    code=2,
                    fix=f"{program()} {command} {selector} --json --yes",
                )
        elif not confirm_destructive(
            overwrite_summary(overwrite_targets, base_path), f"Proceed with {command}?", yes=yes
        ):
            raise die(f"{command.capitalize()} cancelled")

    results: list[dict[str, object]] = []

    for cat_name, folder, existing_meta, offloaded, prev_remotes, arch, file_count, will_skip in prepared:
        shown = display(folder, base_path)

        if will_skip:
            # A category restore only touches parked folders; copying remote
            # content over a live one is additive but surprising. Naming a
            # single folder explicitly still runs.
            if not as_json:
                caption(f"Skipped {shown} -- not offloaded")
            results.append({"folder": shown, "category": cat_name, "skipped": "not-offloaded"})
            continue
        if clear_offloaded and not offloaded and not prev_remotes and not remote:
            warn(f"{shown} does not appear to be offloaded.")

        version_entry = select_version(arch, version) if arch else None
        version_remotes = get_recorded_remotes(version_entry) if version_entry else None
        src = resolve_fetch_source(remote, prev_remotes, cat_name, folder.name, command, version_remotes)
        # Named before the branch so --json reports it on a dry run too.
        wanted = version_entry.get("name") if version_entry else None
        would_overwrite = bool(file_count)

        fetched: str | None = None
        if dry_run:
            if not as_json:
                note(remote_message(f"Would {command}", src, [shown]))
                if wanted:
                    caption(f"Would unpack {wanted}")
                if would_overwrite:
                    warn(f"{shown} holds local files that would be overwritten")
        else:
            if arch:
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
            "would_overwrite_local": would_overwrite,
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
    yes: Yes = False,
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
        yes=yes,
    )


@app.command()
def restore(
    selector: Selector,
    remote: PullRemote = None,
    version: ArchiveVersion = None,
    no_compress: NoCompressPull = False,
    keep_local: RestoreKeepLocal = False,
    yes: Yes = False,
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
        keep_local=keep_local,
        yes=yes,
    )
