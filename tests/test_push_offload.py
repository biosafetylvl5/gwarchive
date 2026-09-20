"""push and offload, including compressed transfer and retention.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

import json
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from conftest import run

from gwarchive import external, naming, paths, sync, tarball, tombstone


def test_keep_prunes_on_every_remote_it_recorded(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    stamps: Callable[[Sequence[str]], None],
    gzip_codec: None,
) -> None:
    """--keep had only ever been exercised against a single remote."""
    stamps(["20260101-000001", "20260101-000002"])
    one, two = tmp_path / "r1", tmp_path / "r2"

    for _ in range(2):
        assert (
            run("push", "P1", "--remote", one, "--remote", two, "--keep", 1, "--path", archive).exit_code == 0
        )

    for remote in (one, two):
        assert [p.name for p in (remote / "Project").iterdir()] == [
            "P0001 Alpha [draft].20260101-000002.tar.gz"
        ]
    deleted = sorted(Path(c[1]).parent.parent.name for c in rclone_local if c[0] == "deletefile")
    assert deleted == ["r1", "r2"]
    versions = (tombstone.read_tombstone(sample) or {})["archive"]["versions"]  # type: ignore[index]
    assert len(versions) == 1


@pytest.mark.parametrize("command", ["pull", "restore"])
def test_pull_and_restore_json_shape(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
    command: str,
) -> None:
    """Neither command's --json payload had ever been parsed by a test."""
    remote = tmp_path / "remote"
    assert run("offload", "P1", "--remote", remote, "--yes", "--path", archive).exit_code == 0

    result = run(command, "P1", "--json", "--path", archive)
    assert result.exit_code == 0
    (entry,) = json.loads(result.stdout)
    assert entry["prefix"] == "P0001"
    assert entry["category"] == "Project"
    assert entry["dry_run"] is False
    assert entry["archive"].endswith(".tar.gz")
    assert ("restored" in entry) is (command == "restore")


def test_remote_resolution_cli_and_env(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GWARCHIVE_REMOTE", raising=False)
    run("create", "P", "Alpha", "--path", archive)

    # Missing remote exits 2
    res = run("push", "P1", "--no-compress", "--path", archive)
    assert res.exit_code == 2
    assert "No remote specified" in res.stderr

    # Env var works
    monkeypatch.setenv("GWARCHIVE_REMOTE", "nas:archive")
    calls = rclone()
    res = run("push", "P1", "--no-compress", "--path", archive)
    assert res.exit_code == 0
    assert len(calls) == 1
    assert calls[0][2] == "nas:archive/Project/P0001 Alpha"

    # CLI option overrides env
    calls.clear()
    res = run("push", "P1", "--remote", "proton:backups", "--no-compress", "--path", archive)
    assert res.exit_code == 0
    assert len(calls) == 1
    assert calls[0][2] == "proton:backups/Project/P0001 Alpha"


@pytest.mark.parametrize(
    "bad", ["unraid", "backup/nas", "~/backup", ".", " ", "backup/nas:", "./nas:", "~/nas:"]
)
def test_push_refuses_a_remote_without_a_colon(
    bad: str,
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """rclone reads a colonless argument as a local directory; refuse before copying."""
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)

    res = run("push", "P1", "--remote", bad, "--no-compress", "--path", archive)
    assert res.exit_code == 2
    assert "not a remote target" in res.stderr
    assert calls == []
    assert tombstone.read_tombstone(archive / "Project" / "P0001 Alpha") is None

    dry = run("push", "P1", "--remote", bad, "--dry-run", "--path", archive)
    assert dry.exit_code == 2
    assert "Would push" not in dry.stdout


def test_push_refuses_a_colonless_remote_from_the_environment(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = rclone()
    monkeypatch.setenv("GWARCHIVE_REMOTE", "unraid")
    run("create", "P", "Alpha", "--path", archive)

    res = run("push", "P1", "--path", archive)
    assert res.exit_code == 2
    assert "not a remote target" in res.stderr
    assert "$GWARCHIVE_REMOTE" in res.stderr
    assert calls == []


def test_push_suggests_the_missing_colon(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    rclone()
    run("create", "P", "Alpha", "--path", archive)

    res = run("push", "P1", "--remote", "unraid", "--path", archive)
    assert "--remote unraid:" in res.stderr


def test_offload_refuses_a_colonless_remote_before_deleting_anything(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """The destructive command must fail on the remote, not after clearing the folder."""
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    doc = archive / "Project" / "P0001 Alpha" / "notes.txt"
    doc.write_text("important data\n")

    res = run("offload", "P1", "--remote", "unraid", "--yes", "--path", archive)
    assert res.exit_code == 2
    assert "not a remote target" in res.stderr
    assert calls == []
    assert doc.exists()
    assert not tombstone.is_offloaded(archive / "Project" / "P0001 Alpha")


@pytest.mark.parametrize(
    "remote, expected",
    [
        ("nas:archive", "nas:archive/Project/P0001 Alpha"),
        ("nas:", "nas:Project/P0001 Alpha"),
        (":sftp,host=example.com:", ":sftp,host=example.com:Project/P0001 Alpha"),
        ("/Volumes/Backup", "/Volumes/Backup/Project/P0001 Alpha"),
    ],
)
def test_push_accepts_every_deliberate_remote_shape(
    remote: str,
    expected: str,
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """A bare ``nas:``, a connection string and an absolute path are all real targets."""
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)

    res = run("push", "P1", "--remote", remote, "--no-compress", "--path", archive)
    assert res.exit_code == 0
    assert calls[0][2] == expected


def test_push_refuses_a_bad_remote_even_when_it_is_not_the_first(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """Every remote is checked before the first copy, so a good one is not half-pushed."""
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)

    res = run(
        "push", "P1", "--remote", "nas:archive", "--remote", "unraid", "--no-compress", "--path", archive
    )
    assert res.exit_code == 2
    assert "not a remote target" in res.stderr
    assert calls == []


def test_push_drops_a_stray_recorded_remote_from_the_metadata(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """A push heals metadata a laxer version poisoned instead of carrying it forward."""
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    tombstone.write_tombstone(folder, {"prefix": "P0001", "remotes": ["unraid/Project/P0001 Alpha"]})

    res = run("push", "P1", "--remote", "nas:archive", "--path", archive)
    assert res.exit_code == 0
    meta = tombstone.read_tombstone(folder)
    assert meta is not None
    assert meta["remotes"] == ["nas:archive/Project/P0001 Alpha"]


def test_push_invokes_rclone_copy_and_writes_metadata(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    doc = archive / "Project" / "P0001 Alpha" / "notes.txt"
    doc.write_text("important data\n")

    res = run("push", "P1", "--remote", "nas:archive", "--no-compress", "--path", archive)
    assert res.exit_code == 0
    assert "Pushed" in res.stdout

    # Assert rclone was invoked with copy and --exclude
    assert len(calls) == 1
    folder_str = str(archive / "Project" / "P0001 Alpha")
    assert calls[0] == ["copy", folder_str, "nas:archive/Project/P0001 Alpha", *external.RCLONE_EXCLUDES]

    # Local file is still there (push is non-destructive)
    assert doc.exists()

    # Metadata file is created
    meta = tombstone.read_tombstone(archive / "Project" / "P0001 Alpha")
    assert meta is not None
    assert meta["prefix"] == "P0001"
    assert meta["remotes"] == ["nas:archive/Project/P0001 Alpha"]
    assert "last_pushed_at" in meta
    assert "offloaded_at" not in meta  # not offloaded!
    assert not tombstone.is_offloaded(archive / "Project" / "P0001 Alpha")


def test_push_category_pushes_all_folders(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("create", "P", "Beta", "--path", archive)

    res = run("push", "P", "--remote", "nas:archive", "--no-compress", "--path", archive)
    assert res.exit_code == 0
    assert len(calls) == 2


def test_push_repeatable_remote(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)

    res = run("push", "P1", "--remote", "nas:archive", "--remote", "proton:mirror", "--path", archive)
    assert res.exit_code == 0
    assert len(calls) == 2
    meta = tombstone.read_tombstone(archive / "Project" / "P0001 Alpha")
    assert meta is not None
    assert meta["remotes"] == ["nas:archive/Project/P0001 Alpha", "proton:mirror/Project/P0001 Alpha"]


def test_push_skips_tombstones_and_preserves_their_metadata(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """Pushing an offloaded folder must not overwrite its recorded size/count with zeros."""
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    (folder / "data.bin").write_bytes(b"x" * 1234)
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--path", archive)
    before = tombstone.read_tombstone(folder)
    assert before is not None and before["size"] == 1234

    calls.clear()
    res = run("push", "P", "--remote", "nas:archive", "--path", archive)
    assert res.exit_code == 0
    assert "Skipped" in res.stdout
    assert len(calls) == 0

    after = tombstone.read_tombstone(folder)
    assert after is not None
    assert after["size"] == 1234
    assert after["file_count"] == before["file_count"]
    assert "offloaded_at" in after


def test_offload_category_skips_already_offloaded_folders(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """A whole-category offload is re-runnable: parked folders are skipped, not fatal."""
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("create", "P", "Beta", "--path", archive)
    (archive / "Project" / "P0002 Beta" / "doc.txt").write_text("beta")
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--path", archive)

    calls.clear()
    res = run("offload", "P", "--remote", "nas:archive", "--yes", "--path", archive)
    assert res.exit_code == 0
    assert "Skipped" in res.stdout
    assert len(calls) == 1  # only Beta transferred
    assert tombstone.is_offloaded(archive / "Project" / "P0002 Beta")

    # And when everything is already parked, it succeeds with a clear message.
    calls.clear()
    res = run("offload", "P", "--remote", "nas:archive", "--yes", "--path", archive)
    assert res.exit_code == 0
    assert "Nothing to offload" in res.stdout
    assert len(calls) == 0


def test_offload_prompts_and_declines_without_yes(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    doc = archive / "Project" / "P0001 Alpha" / "data.bin"
    doc.write_bytes(b"12345")

    # User enters 'n'
    res = run("offload", "P1", "--remote", "nas:archive", "--path", archive, input="n\n")
    assert res.exit_code == 1
    assert "Offload cancelled" in res.stderr
    assert len(calls) == 0
    assert doc.exists()


def test_offload_deletes_local_contents_and_leaves_tombstone(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    doc = folder / "data.bin"
    doc.write_bytes(b"hello world")
    sub = folder / "P0001.01 Sub"
    sub.mkdir()
    (sub / "subfile.txt").write_text("sub")

    res = run("offload", "P1", "--remote", "nas:archive", "--yes", "--path", archive)
    assert res.exit_code == 0
    assert "Offloaded" in res.stdout
    assert len(calls) == 1

    # Local contents deleted
    assert not doc.exists()
    assert not sub.exists()

    # Tombstone file remains
    assert (folder / naming.TOMBSTONE_NAME).exists()
    assert tombstone.is_offloaded(folder)
    meta = tombstone.read_tombstone(folder)
    assert meta is not None
    assert "offloaded_at" in meta
    assert meta["prefix"] == "P0001"


def test_offload_refuses_already_offloaded(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--path", archive)

    # Second offload fails
    res = run("offload", "P1", "--remote", "nas:archive", "--yes", "--path", archive)
    assert res.exit_code == 1
    assert "already offloaded" in res.stderr


def test_offload_dry_run_leaves_files_intact(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    doc = archive / "Project" / "P0001 Alpha" / "doc.txt"
    doc.write_text("test")

    res = run("offload", "P1", "--remote", "nas:archive", "--dry-run", "--path", archive)
    assert res.exit_code == 0
    assert "Would offload" in res.stdout
    assert len(calls) == 0
    assert doc.exists()
    assert not tombstone.is_offloaded(archive / "Project" / "P0001 Alpha")


def test_restore_downloads_and_clears_offloaded_status(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--no-compress", "--path", archive)
    assert tombstone.is_offloaded(folder)

    calls.clear()
    res = run("restore", "P1", "--no-compress", "--path", archive)
    assert res.exit_code == 0
    assert "Restored" in res.stdout
    assert len(calls) == 1
    assert calls[0][1] == "nas:archive/Project/P0001 Alpha"

    # Status cleared
    assert not tombstone.is_offloaded(folder)
    meta = tombstone.read_tombstone(folder)
    assert meta is not None
    assert "offloaded_at" not in meta
    assert "restored_at" in meta


def test_list_marks_offloaded_folders(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("create", "P", "Beta", "--path", archive)
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--no-compress", "--path", archive)

    table_res = run("list", "P", "--path", archive)
    assert table_res.exit_code == 0
    assert "Status" in table_res.stdout
    assert "offloaded" in table_res.stdout

    json_res = run("list", "P", "--json", "--path", archive)
    assert json_res.exit_code == 0
    data = json.loads(json_res.stdout)
    p0001 = next(item for item in data if item["prefix"] == "P0001")
    p0002 = next(item for item in data if item["prefix"] == "P0002")
    assert p0001.get("offloaded") is True
    assert p0002.get("offloaded") is not True


def test_stats_excludes_tombstone_and_shows_offloaded(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--path", archive)

    res = run("stats", "--path", archive)
    assert res.exit_code == 0
    assert "1 folder offloaded" in res.stdout
    assert "0 bytes" in res.stdout


def test_sync_rejects_subfolders(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    """Sync works on top-level folders only; a subfolder would flatten on the remote."""
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("mksub", "P1", "Notes", "--path", archive)
    sub = archive / "Project" / "P0001 Alpha" / "P0001.01 Notes"

    res = run("push", sub, "--remote", "nas:archive", "--path", archive)
    assert res.exit_code == 1
    assert "top-level" in res.stderr
    assert "gwarchive push P0001" in res.stderr


def test_offload_dry_run_on_already_offloaded_folder_reports_instead_of_dying(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--path", archive)

    res = run("offload", "P1", "--remote", "nas:archive", "--dry-run", "--path", archive)
    assert res.exit_code == 0
    assert "already offloaded" in res.stdout


def test_offload_writes_tombstone_before_deleting(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If deletion is interrupted, the folder must already read as offloaded."""
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    (folder / "doc.txt").write_text("data")

    seen: list[bool] = []
    real_rmtree = shutil.rmtree

    def spy_unlink(self: Path, missing_ok: bool = False) -> None:
        seen.append(tombstone.is_offloaded(folder))
        real_unlink(self, missing_ok=missing_ok)

    real_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", spy_unlink)
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)

    res = run("offload", "P1", "--remote", "nas:archive", "--yes", "--path", archive)
    assert res.exit_code == 0
    # Every deletion happened after the tombstone was already in place.
    assert seen and all(seen)


def test_multi_remote_offload_records_successful_copy_before_failure(
    archive: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the second remote fails, the first successful copy is still recorded."""
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    doc = folder / "doc.txt"
    doc.write_text("data")

    def _fake_run(args: Sequence[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        rc = 1 if any("bad:" in arg for arg in args) else 0
        return subprocess.CompletedProcess(args=["rclone", *args], returncode=rc, stdout="", stderr="boom")

    monkeypatch.setattr(external, "run_rclone", _fake_run)

    res = run(
        "offload", "P1", "--remote", "good:archive", "--remote", "bad:archive", "--yes", "--path", archive
    )
    assert res.exit_code == 1
    # Local files preserved, not offloaded...
    assert doc.exists()
    assert not tombstone.is_offloaded(folder)
    # ...but the copy that did land is on record.
    meta = tombstone.read_tombstone(folder)
    assert meta is not None
    assert "good:archive/Project/P0001 Alpha" in tombstone.get_recorded_remotes(meta)


def test_restore_category_skips_live_folders(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("create", "P", "Beta", "--path", archive)
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--no-compress", "--path", archive)

    calls.clear()
    res = run("restore", "P", "--no-compress", "--path", archive)
    assert res.exit_code == 0
    # Only the offloaded folder was pulled; the live one was skipped.
    assert len(calls) == 1
    assert calls[0][1] == "nas:archive/Project/P0001 Alpha"
    assert "Skipped" in res.stdout and "Beta" in res.stdout


def test_rclone_failure_aborts_without_deleting_local_files(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    rclone(returncode=1, stderr="connection timed out")
    run("create", "P", "Alpha", "--path", archive)
    doc = archive / "Project" / "P0001 Alpha" / "keepme.txt"
    doc.write_text("don't delete me")

    res = run("offload", "P1", "--remote", "nas:archive", "--yes", "--no-compress", "--path", archive)
    assert res.exit_code == 1
    assert "Could not offload" in res.stderr
    assert "connection timed out" in res.stderr
    # Crucial safety invariant: local file was not deleted!
    assert doc.exists()
    assert not tombstone.is_offloaded(archive / "Project" / "P0001 Alpha")


def test_push_uploads_one_object_per_remote(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """The point of the whole feature: many files leave as a single object."""
    calls = rclone_local
    one, two = tmp_path / "r1", tmp_path / "r2"

    res = run("push", "P1", "--remote", one, "--remote", two, "--json", "--path", archive)
    assert res.exit_code == 0

    assert [call[0] for call in calls] == ["copyto", "copyto"]
    objects = sorted(p.name for p in (one / "Project").iterdir())
    assert len(objects) == 1
    assert objects[0].startswith("P0001 Alpha [draft].") and objects[0].endswith(".tar.gz")
    assert (two / "Project" / objects[0]).is_file()

    payload = json.loads(res.stdout)[0]
    arch = payload["archive"]
    assert arch["format"] == "tar.gz"
    assert arch["keep"] == 1
    assert len(arch["versions"]) == 1
    version = arch["versions"][0]
    assert version["name"] == objects[0]
    assert re.fullmatch(r"[0-9a-f]{64}", version["sha256"])
    assert version["member_count"] == 3  # notes.txt, sub/, sub/deep.bin -- and no cache
    assert version["compressed_size"] == (one / "Project" / objects[0]).stat().st_size


def test_archive_excludes_the_reserved_metadata_namespace(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """The tombstone and .gwarchive-* never travel to a remote."""
    remote = tmp_path / "remote"

    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 0

    obj = next((remote / "Project").iterdir())
    with tarfile.open(obj, "r:gz") as tar:
        names = sorted(tar.getnames())
    assert names == ["notes.txt", "sub", "sub/deep.bin"]
    assert not any(n.startswith(".gwarchive-") for n in names)


def test_offload_then_restore_round_trips_the_contents(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """The round trip that has to hold, byte for byte, through one object."""
    remote = tmp_path / "remote"
    folder = sample
    reference = tmp_path / "reference"
    shutil.copytree(folder, reference)

    assert run("offload", "P1", "--remote", remote, "--yes", "--path", archive).exit_code == 0
    assert tombstone.is_offloaded(folder)
    assert sorted(p.name for p in folder.iterdir()) == [".gwarchive-cache", naming.TOMBSTONE_NAME]

    assert run("restore", "P1", "--path", archive).exit_code == 0
    assert not tombstone.is_offloaded(folder)
    assert (folder / "notes.txt").read_text() == "top level\n"
    assert (folder / "sub" / "deep.bin").read_bytes() == (reference / "sub" / "deep.bin").read_bytes()
    # The excluded cache is local-only, so the round trip must leave it exactly
    # where it was -- never travelling is not a licence to delete it.
    assert (folder / ".gwarchive-cache" / "junk").exists()


def test_restore_refuses_an_object_that_fails_its_checksum(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """A corrupt object must leave an offloaded folder a clean tombstone."""
    remote = tmp_path / "remote"
    folder = sample
    assert run("offload", "P1", "--remote", remote, "--yes", "--path", archive).exit_code == 0

    obj = next((remote / "Project").iterdir())
    with obj.open("ab") as handle:
        handle.write(b"corruption")

    res = run("restore", "P1", "--path", archive)
    assert res.exit_code == 1
    assert "checksum" in res.stderr
    # Only copy recorded, so the hint must not offer a --version 2 that is not there.
    assert "--version" not in res.stderr
    assert sorted(p.name for p in folder.iterdir()) == [".gwarchive-cache", naming.TOMBSTONE_NAME]
    assert tombstone.is_offloaded(folder)

    # With a spare copy retained, the same failure points at it.
    meta = tombstone.read_tombstone(folder) or {}
    versions = meta["archive"]["versions"]  # type: ignore[index]
    versions.append({**versions[0], "name": "P0001 Alpha [draft].20250101-000000.tar.gz"})
    tombstone.write_tombstone(folder, meta)
    res = run("restore", "P1", "--path", archive)
    assert res.exit_code == 1
    assert "--version 2" in res.stderr


def test_restore_warns_when_a_version_has_no_recorded_checksum(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """A hand-edited tombstone still restores, but says it could not verify."""
    remote = tmp_path / "remote"
    folder = sample
    assert run("offload", "P1", "--remote", remote, "--yes", "--path", archive).exit_code == 0

    meta = tombstone.read_tombstone(folder) or {}
    meta["archive"]["versions"][0].pop("sha256")  # type: ignore[index]
    tombstone.write_tombstone(folder, meta)

    res = run("restore", "P1", "--path", archive)
    assert res.exit_code == 0
    assert "no recorded checksum" in res.stderr
    assert (folder / "notes.txt").read_text() == "top level\n"


def test_offload_keeps_what_the_archive_never_carried(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """The rule that decides what is packed must decide what may be deleted.

    create_archive prunes the .gwarchive-* namespace by path component, but the
    delete loop skipped only the tombstone -- so a file under .gwarchive-cache/
    was excluded from the archive and then deleted locally, leaving no copy
    anywhere. verify_archive could not notice: expected and found both came
    from the same pruned walk.
    """
    remote = tmp_path / "remote"
    folder = sample
    (folder / ".gwarchive-cache" / "junk").write_text("the only copy")

    assert run("offload", "P1", "--remote", remote, "--yes", "--path", archive).exit_code == 0
    assert (folder / ".gwarchive-cache" / "junk").read_text() == "the only copy"
    # What *was* packed is gone, so the offload still did its job.
    assert not (folder / "notes.txt").exists()
    assert not (folder / "sub").exists()


def test_the_mirror_excludes_the_same_namespace_the_archive_does(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """--no-compress used to upload the .gwarchive-* namespace the tar filter drops."""
    calls = rclone_local
    remote = tmp_path / "remote"

    assert run("push", "P1", "--no-compress", "--remote", remote, "--path", archive).exit_code == 0
    assert calls[0][0] == "copy"
    assert calls[0][3:] == external.RCLONE_EXCLUDES


def test_no_compress_copies_files_and_records_no_archive(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """The escape hatch keeps the old per-file mirror, tombstone included."""
    calls = rclone_local
    remote = tmp_path / "remote"
    folder = sample

    assert run("push", "P1", "--remote", remote, "--no-compress", "--path", archive).exit_code == 0
    assert calls[0][0] == "copy"
    assert (remote / "Project" / folder.name / "sub" / "deep.bin").is_file()
    assert "archive" not in (tombstone.read_tombstone(folder) or {})


def test_a_no_compress_push_clears_a_stale_archive_block(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """Leaving it would let restore fetch an object this push never refreshed."""
    remote = tmp_path / "remote"
    folder = sample

    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 0
    assert "archive" in (tombstone.read_tombstone(folder) or {})

    assert run("push", "P1", "--remote", remote, "--no-compress", "--path", archive).exit_code == 0
    assert "archive" not in (tombstone.read_tombstone(folder) or {})


def test_keep_retains_the_newest_copies_and_removes_the_rest(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    stamps: Callable[[Sequence[str]], None],
    gzip_codec: None,
) -> None:
    """--keep 2 leaves two objects on the remote and two recorded versions."""
    calls = rclone_local
    stamps(["20260101-000001", "20260101-000002", "20260101-000003"])
    remote = tmp_path / "remote"
    folder = sample

    for _ in range(3):
        assert run("push", "P1", "--remote", remote, "--keep", 2, "--path", archive).exit_code == 0

    objects = sorted(p.name for p in (remote / "Project").iterdir())
    assert objects == [
        "P0001 Alpha [draft].20260101-000002.tar.gz",
        "P0001 Alpha [draft].20260101-000003.tar.gz",
    ]
    versions = (tombstone.read_tombstone(folder) or {})["archive"]["versions"]  # type: ignore[index]
    assert [v["name"] for v in versions] == list(reversed(objects))

    deleted = [call[1] for call in calls if call[0] == "deletefile"]
    assert deleted == [str(remote / "Project" / "P0001 Alpha [draft].20260101-000001.tar.gz")]


def test_a_rename_between_pushes_does_not_invert_the_version_index(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    stamps: Callable[[Sequence[str]], None],
    gzip_codec: None,
) -> None:
    """Object names start with the folder name, so name order is not time order.

    prune_versions used to re-sort survivors by object name. One rename to a
    lexicographically earlier descriptor then put the *older* copy at index 0,
    so a default restore served stale content for good and the next prune
    deleted the second-newest object instead of the oldest.
    """
    calls = rclone_local
    stamps(["20260101-000001", "20260101-000002", "20260101-000003"])
    remote = tmp_path / "remote"

    for _ in range(2):
        assert run("push", "P1", "--remote", remote, "--keep", 2, "--path", archive).exit_code == 0
    # "Aardvark" sorts before "Alpha", which is the whole point.
    assert run("rename", "P1", "Aardvark", "--path", archive).exit_code == 0
    assert run("push", "P1", "--remote", remote, "--keep", 2, "--path", archive).exit_code == 0

    folder = archive / "Project" / "P0001 Aardvark"
    versions = (tombstone.read_tombstone(folder) or {})["archive"]["versions"]  # type: ignore[index]
    assert [v["name"] for v in versions] == [
        "P0001 Aardvark.20260101-000003.tar.gz",
        "P0001 Alpha [draft].20260101-000002.tar.gz",
    ]
    # The oldest went, not the second-newest.
    deleted = [Path(call[1]).name for call in calls if call[0] == "deletefile"]
    assert deleted == ["P0001 Alpha [draft].20260101-000001.tar.gz"]
    # And the newest is what a bare restore reaches for.
    arch = (tombstone.read_tombstone(folder) or {})["archive"]
    assert tarball.select_version(arch, None)["name"] == "P0001 Aardvark.20260101-000003.tar.gz"  # type: ignore[arg-type]


def test_prune_does_not_retry_a_delete_it_already_made(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    stamps: Callable[[Sequence[str]], None],
    gzip_codec: None,
) -> None:
    """Recorded remotes are a historical union; several can name one object.

    After a rename the tombstone lists both directory paths, and both map to
    the same sibling object -- so the second deletefile used to fail on the
    file the first had just removed, marking a successful prune as failed and
    keeping the entry for a retry that could never succeed.
    """
    stamps(["20260101-000001", "20260101-000002"])
    remote = tmp_path / "remote"

    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 0
    assert run("rename", "P1", "Beta", "--path", archive).exit_code == 0
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 0
    assert "Could not remove" not in res.stderr

    folder = archive / "Project" / "P0001 Beta"
    meta = tombstone.read_tombstone(folder) or {}
    assert len(meta["archive"]["versions"]) == 1  # type: ignore[index]
    assert "Removed 1 older copy" in res.stdout


def test_keep_zero_never_removes_a_copy(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    stamps: Callable[[Sequence[str]], None],
    gzip_codec: None,
) -> None:
    """0 means keep everything -- retention off, not retention of nothing."""
    calls = rclone_local
    stamps(["20260101-000001", "20260101-000002"])
    remote = tmp_path / "remote"

    for _ in range(2):
        assert run("push", "P1", "--remote", remote, "--keep", 0, "--path", archive).exit_code == 0

    assert len(list((remote / "Project").iterdir())) == 2
    assert not [call for call in calls if call[0] == "deletefile"]


def test_a_failed_prune_warns_and_keeps_the_entry_for_next_time(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    stamps: Callable[[Sequence[str]], None],
    gzip_codec: None,
) -> None:
    """Cleanup failing is a storage cost, not a reason to fail the transfer."""
    stamps(["20260101-000001", "20260101-000002"])
    remote = tmp_path / "remote"
    folder = sample

    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 0
    # Remove the object behind rclone's back, so deletefile fails.
    (remote / "Project" / "P0001 Alpha [draft].20260101-000001.tar.gz").unlink()

    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 0
    assert "Could not remove" in res.stderr
    versions = (tombstone.read_tombstone(folder) or {})["archive"]["versions"]  # type: ignore[index]
    assert len(versions) == 2


def test_version_selects_an_older_copy_and_rejects_an_unknown_one(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    stamps: Callable[[Sequence[str]], None],
    gzip_codec: None,
) -> None:
    """Retention without a way to read it back would just be storage."""
    stamps(["20260101-000001", "20260101-000002"])
    remote = tmp_path / "remote"
    folder = sample

    assert run("push", "P1", "--remote", remote, "--keep", 0, "--path", archive).exit_code == 0
    (folder / "notes.txt").write_text("second push\n")
    assert run("push", "P1", "--remote", remote, "--keep", 0, "--path", archive).exit_code == 0

    assert run("pull", "P1", "--version", 2, "--path", archive).exit_code == 0
    assert (folder / "notes.txt").read_text() == "top level\n"

    assert run("pull", "P1", "--version", 1, "--path", archive).exit_code == 0
    assert (folder / "notes.txt").read_text() == "second push\n"

    res = run("pull", "P1", "--version", 9, "--path", archive)
    assert res.exit_code == 2
    assert "No such version" in res.stderr
    assert "20260101-000001" in res.stderr


def test_push_dry_run_creates_no_archive_and_calls_no_rclone(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """Dry-run names the codec it would really use and touches nothing."""
    calls = rclone_local
    remote = tmp_path / "remote"
    folder = sample

    res = run("push", "P1", "--remote", remote, "--dry-run", "--path", archive)
    assert res.exit_code == 0
    assert "Would push" in res.stdout
    assert "tar.gz" in res.stdout
    assert calls == []
    assert not remote.exists()
    assert not (folder / naming.TOMBSTONE_NAME).exists()


def test_offload_refuses_when_scratch_space_is_short(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive is staged before anything is deleted, so it needs the room."""
    calls = rclone_local
    folder = sample

    class NoRoom:
        free = 1

    monkeypatch.setattr(shutil, "disk_usage", lambda _path: NoRoom())

    res = run("offload", "P1", "--remote", tmp_path / "remote", "--yes", "--path", archive)
    assert res.exit_code == 1
    assert "scratch space" in res.stderr
    assert calls == []
    assert (folder / "notes.txt").is_file()


def test_offload_aborts_before_deleting_when_the_archive_is_short(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated archive must cost an error, never the local files."""
    calls = rclone_local
    folder = sample

    real_create = tarball.create_archive

    def short_count(source: Path, out: Path, codec: str) -> tuple[int, int]:
        members, total = real_create(source, out, codec)
        return members + 1, total

    monkeypatch.setattr(tarball, "create_archive", short_count)

    res = run("offload", "P1", "--remote", tmp_path / "remote", "--yes", "--path", archive)
    assert res.exit_code == 1
    assert "expected" in res.stderr
    assert calls == []
    assert (folder / "notes.txt").read_text() == "top level\n"
    assert not tombstone.is_offloaded(folder)


def test_pull_over_a_live_folder_warns_before_overwriting(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """Extraction rewrites every member, where rclone copy skipped unchanged ones."""
    remote = tmp_path / "remote"
    folder = sample
    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 0

    (folder / "notes.txt").write_text("local edit\n")
    (folder / "untouched.txt").write_text("not in the archive\n")

    res = run("pull", "P1", "--path", archive)
    assert res.exit_code == 0
    assert "still holds local files" in res.stderr
    assert (folder / "notes.txt").read_text() == "top level\n"
    assert (folder / "untouched.txt").read_text() == "not in the archive\n"


def test_dry_run_counts_the_copy_the_real_push_would_remove(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    stamps: Callable[[Sequence[str]], None],
    gzip_codec: None,
) -> None:
    """Pruning happens after the new version is recorded, so the projection
    has to include it -- a dry-run that understates deletions is worse than
    silence."""
    calls = rclone_local
    stamps(["20260101-000001", "20260101-000002"])
    remote = tmp_path / "remote"

    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 0
    calls.clear()

    res = run("push", "P1", "--remote", remote, "--dry-run", "--path", archive)
    assert res.exit_code == 0
    assert "Would remove 1 older copy" in res.stdout
    assert calls == []

    # And the real run removes exactly what the dry-run promised.
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert "Removed 1 older copy" in res.stdout
    assert len(list((remote / "Project").iterdir())) == 1


def test_offload_prompt_states_the_scratch_it_needs(
    archive: Path,
    tmp_path: Path,
    sample: Path,
    rclone_local: list[list[str]],
    gzip_codec: None,
) -> None:
    """The archive is staged before deletion, so its cost belongs in the prompt."""

    res = run("offload", "P1", "--remote", str(tmp_path / "remote"), "--path", archive, input="n\n")
    assert res.exit_code == 1
    assert "Offload cancelled" in res.stderr

    # Asserted against the summary itself rather than scraped out of the
    # rendered panel: the panel goes to stderr so it cannot share stdout with a
    # --json document, and its wrapping depends on the terminal width.
    folder = archive / "Project" / "P0001 Alpha [draft]"
    size, count = paths.compute_folder_stats(folder)
    summary = sync.offload_summary(
        [("Project", folder, size, count)], ["nas:archive"], archive, "tar.gz"
    ).plain
    assert "packs" in summary and "one tar.gz object per folder" in summary
    assert "needs" in summary and "$TMPDIR" in summary
    assert "frees" in summary
