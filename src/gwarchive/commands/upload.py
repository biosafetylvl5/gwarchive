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

import shutil
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
    _reserved_member,
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
)
from gwarchive.paths import (
    compute_folder_stats,
    display,
)
from gwarchive.sync import (
    carry_remotes,
    compress_default,
    ensure_scratch_space,
    keep_default,
    offload_summary,
    resolve_sync_targets,
    stage_archive,
)
from gwarchive.tarball import (
    prune_versions,
    record_version,
    remote_archive_object,
    verify_archive,
    would_prune,
)
from gwarchive.tombstone import (
    get_recorded_remotes,
    is_offloaded,
    read_tombstone,
    write_tombstone,
)


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
    codec = tarball.pick_codec() if compress_default() and not no_compress else ""
    retain = keep_default(keep)

    # A whole-category offload skips folders that are already parked, so an
    # interrupted batch can be re-run. Naming one folder explicitly still fails
    # loudly -- the user asked for something that cannot happen.
    already = [(cat, folder) for cat, folder in targets if is_offloaded(folder)]
    if already:
        if len(targets) == 1 and not dry_run:
            raise die(
                f"{display(targets[0][1], base_path)} is already offloaded.",
                fix=f"{program()} restore {folder_prefix(targets[0][1].name)}",
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
