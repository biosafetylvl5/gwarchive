"""push and offload -- deliberately NOT merged.

They are ~74% identical by line (86 of 116/139 code lines match in order,
ratio 0.67 by difflib; the AGENTS.md note saying ~33 lines predates the
compressed-transfer work). Merging puts the local-delete loop inside a
function push also calls, gated on a flag -- and a flag is a thing that can be
wrong. Today push CANNOT delete local files, because push's source contains no
delete. That structural impossibility is worth more than the duplication, and
keeping both bodies in one file is what lets a reader diff them by eye.

offload writes its tombstone BEFORE deleting anything, so an interrupted
offload reads as offloaded-with-leftovers rather than a silently emptied
folder."""

import tempfile
from pathlib import Path

from gwarchive import __version__, clock, tarball
from gwarchive.external import (
    RCLONE_EXCLUDES,
    get_remotes,
    rclone_or_die,
    remote_folder_target,
)
from gwarchive.naming import (
    folder_descriptor,
    folder_prefix,
)
from gwarchive.options import (
    AsJsonSync,
    BasePath,
    DryRun,
    Keep,
    NoCompressPush,
    PushRemotes,
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
    carry_remotes,
    compress_default,
    delete_archived_contents,
    ensure_scratch_space,
    keep_default,
    offload_summary,
    require_tombstone,
    resolve_sync_targets,
    snapshot_manifest,
    stage_archive,
)
from gwarchive.tarball import (
    prune_versions,
    record_version,
    remote_archive_object,
    unsupported_members,
    verify_archive,
    would_prune,
)
from gwarchive.tombstone import (
    get_recorded_remotes,
    write_tombstone,
)


def _unsupported_member_report(folder: Path, base_path: Path, bad: list[Path]) -> str:
    """One line naming up to 10 symlinks/special files, for a warning or a refusal."""
    shown = display(folder, base_path)
    sample = ", ".join(str(p.relative_to(folder)) for p in bad[:10])
    more = f" (+{len(bad) - 10} more)" if len(bad) > 10 else ""
    return f"{shown}: {sample}{more}"


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
    codec = tarball.pick_codec() if compress_default() and not no_compress else ""

    results: list[dict[str, object]] = []

    for cat_name, folder in targets:
        existing_meta = require_tombstone(folder, base_path)
        offloaded = bool(existing_meta.get("offloaded_at"))
        size, file_count = compute_folder_stats(folder)

        if offloaded:
            shown = display(folder, base_path)
            if file_count:
                # "Already on the remote" would be false here: either this was
                # pulled and then edited, or an earlier offload's delete step
                # was interrupted and these are its leftovers. Either way the
                # remote does not have what is on disk right now. Fewer files
                # than were offloaded points at leftovers, where --keep-local
                # would make a partial copy the live one -- so the hint differs.
                recorded = existing_meta.get("file_count")
                partial = isinstance(recorded, int) and file_count < recorded
                ref = folder_prefix(folder.name) or shown
                raise die(
                    f"{shown} is offloaded but holds {plural(file_count, 'local file')} not reflected "
                    "on the remote.\n"
                    "Either it was pulled and then edited, or an earlier offload's delete step was "
                    "interrupted and these are its leftovers.",
                    fix=(
                        f"{program()} restore {ref}   # fewer files than were offloaded: fetch the archive"
                        if partial
                        else f"{program()} restore {ref} --keep-local   # keep these files as the live copy"
                    ),
                )
            # A clean tombstone has no local bytes to back up. Pushing it would
            # also overwrite the recorded size/file_count with zeros.
            if not as_json:
                caption(f"Skipped {shown} -- offloaded, already on the remote")
            results.append(
                {
                    "folder": shown,
                    "prefix": folder_prefix(folder.name),
                    "category": cat_name,
                    "skipped": "offloaded",
                    "dry_run": dry_run,
                }
            )
            continue

        bad_links = unsupported_members(folder)
        if bad_links:
            warn(f"won't round-trip through push: {_unsupported_member_report(folder, base_path, bad_links)}")

        pfx = folder_prefix(folder.name) or ""
        desc = folder_descriptor(folder.name) or ""
        was_loose = not isinstance(existing_meta.get("archive"), dict)
        prior_arch = existing_meta.get("archive")
        retain = keep_default(keep, prior_arch if isinstance(prior_arch, dict) else None)

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

        dests: list[str] = []
        pruned = 0

        with tempfile.TemporaryDirectory(prefix="gwarchive-") as tmp:
            staged: Path | None = None
            entry: dict[str, object] | None = None
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
                    if entry is not None:
                        # Only the remotes THIS version actually reached, not
                        # every remote the folder has ever used -- see
                        # tarball.prune_versions.
                        raw = entry.get("remotes")
                        version_remotes = raw if isinstance(raw, list) else []
                        if dest not in version_remotes:
                            version_remotes.append(dest)
                        entry["remotes"] = version_remotes
                    # Record each successful copy immediately so a later
                    # remote's failure doesn't lose the record of this one.
                    # Reassigned rather than relied on: `meta["remotes"]` and
                    # `all_remotes` were the same list object, so this write
                    # only persisted the append by accident.
                    meta["remotes"] = all_remotes
                    write_tombstone(folder, meta)

        if dry_run:
            pruned = would_prune(existing_meta, dests, all_remotes, retain) if codec else 0
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
    codec = tarball.pick_codec() if compress_default() and not no_compress else ""
    single_target = len(targets) == 1

    # Every tombstone is read once, up front: corrupt must stop the whole run
    # before the confirmation prompt, not partway through a batch that has
    # already deleted some folders' local files.
    states: list[tuple[str, Path, dict[str, object], bool, int, int]] = []
    for cat_name, folder in targets:
        existing_meta = require_tombstone(folder, base_path)
        offloaded = bool(existing_meta.get("offloaded_at"))
        size, file_count = compute_folder_stats(folder)
        states.append((cat_name, folder, existing_meta, offloaded, size, file_count))

    # A whole-category offload skips folders that are already parked, so an
    # interrupted batch can be re-run. Naming one folder explicitly still fails
    # loudly -- the user asked for something that cannot happen.
    already = [(cat, folder) for cat, folder, _meta, offloaded, _size, _count in states if offloaded]
    if already:
        if single_target and not dry_run:
            _, lone = already[0]
            raise die(
                f"{display(lone, base_path)} is already offloaded.",
                fix=f"{program()} restore {folder_prefix(lone.name)}",
            )
        for _, folder in already:
            if not as_json:
                caption(f"Skipped {display(folder, base_path)} -- already offloaded")
        states = [s for s in states if not s[3]]
        if not states:
            if as_json:
                emit_json([])
            else:
                ok("Nothing to offload -- every folder is already offloaded.")
            return

    # A folder holding nothing but reserved metadata has nothing to archive.
    empty = [(cat, folder) for cat, folder, _meta, _off, _size, count in states if count == 0]
    if empty:
        if single_target:
            lone_folder = empty[0][1]
            shown = display(lone_folder, base_path)
            raise die(
                f"{shown} holds no files to offload.",
                fix=f"{program()} find {folder_prefix(lone_folder.name) or shown}",
            )
        for _, folder in empty:
            if not as_json:
                caption(f"Skipped {display(folder, base_path)} -- nothing to offload")
        states = [s for s in states if s[5] != 0]
        if not states:
            if as_json:
                emit_json([])
            else:
                ok("Nothing to offload -- every folder is empty or already offloaded.")
            return

    # Symlinks, fifos, sockets and device nodes cannot round-trip through the
    # archive, or through a --no-compress rclone copy either. Refused before
    # any staging or prompt -- offload is the command that deletes the
    # originals, so this is the last chance to say so.
    offending = []
    for _cat, folder, _meta, _off, _size, _count in states:
        bad = unsupported_members(folder)
        if bad:
            offending.append(_unsupported_member_report(folder, base_path, bad))
    if offending:
        raise die(
            "Cannot offload -- symlink(s) or special file(s) would not round-trip through the "
            "archive:\n" + "\n".join(offending),
            fix="Replace them with real copies (cp -L) or remove them, then retry.",
        )

    measured: list[tuple[str, Path, int, int]] = [
        (cat, folder, size, count) for cat, folder, _meta, _off, size, count in states
    ]

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

    for cat_name, folder, existing_meta, _offloaded, size, file_count in states:
        pfx = folder_prefix(folder.name) or ""
        desc = folder_descriptor(folder.name) or ""
        prior_arch = existing_meta.get("archive")
        retain = keep_default(keep, prior_arch if isinstance(prior_arch, dict) else None)

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

        dests: list[str] = []
        pruned = 0
        # Taken before packing or copying -- which can take a while -- so
        # anything created or changed in between is never deleted sight
        # unseen. See sync.delete_archived_contents.
        manifest = snapshot_manifest(folder) if not dry_run else {}

        with tempfile.TemporaryDirectory(prefix="gwarchive-") as tmp:
            staged: Path | None = None
            entry: dict[str, object] | None = None
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
                    if entry is not None:
                        raw = entry.get("remotes")
                        version_remotes = raw if isinstance(raw, list) else []
                        if dest not in version_remotes:
                            version_remotes.append(dest)
                        entry["remotes"] = version_remotes
                    # Record each successful copy immediately: if a later remote
                    # fails, the tombstone still knows where the data already is.
                    meta["remotes"] = all_remotes
                    write_tombstone(folder, meta)

            if dry_run:
                pruned = would_prune(existing_meta, dests, all_remotes, retain) if codec else 0
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

                leftover = delete_archived_contents(folder, manifest)
                if leftover:
                    shown_names = ", ".join(leftover[:5])
                    more = f" (+{len(leftover) - 5} more)" if len(leftover) > 5 else ""
                    warn(
                        f"{display(folder, base_path)}: {plural(len(leftover), 'file')} changed or appeared "
                        f"after packing and stayed local, unuploaded: {shown_names}{more}"
                    )

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
