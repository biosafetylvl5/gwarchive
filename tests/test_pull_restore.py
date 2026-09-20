"""pull and restore -- the two wrappers over one fetch_folders body.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import run

from gwarchive import naming, tombstone


@pytest.mark.parametrize("command", ["pull", "restore"])
def test_pull_and_restore_refuse_a_colonless_remote(
    command: str,
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)

    res = run(command, "P1", "--remote", "unraid", "--path", archive)
    assert res.exit_code == 2
    assert calls == []


@pytest.mark.parametrize("command", ["pull", "restore"])
def test_pull_and_restore_refuse_a_colonless_remote_recorded_in_metadata(
    command: str,
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """Metadata written by a laxer version points at a stray local copy, not a remote."""
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    tombstone.write_tombstone(
        folder,
        {"prefix": "P0001", "offloaded_at": "2026-01-01T00:00:00", "remotes": ["unraid/Project/P0001 Alpha"]},
    )

    res = run(command, "P1", "--path", archive)
    assert res.exit_code == 2
    assert naming.TOMBSTONE_NAME in res.stderr
    assert calls == []


@pytest.mark.parametrize("command", ["pull", "restore"])
def test_pull_and_restore_skip_past_a_stray_recorded_remote(
    command: str,
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """One poisoned entry must not dead-end a folder that also has a real remote."""
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    tombstone.write_tombstone(
        folder,
        {
            "prefix": "P0001",
            "offloaded_at": "2026-01-01T00:00:00",
            "remotes": ["unraid/Project/P0001 Alpha", "nas:archive/Project/P0001 Alpha"],
        },
    )

    res = run(command, "P1", "--path", archive)
    assert res.exit_code == 0
    assert calls[0][1] == "nas:archive/Project/P0001 Alpha"


def test_pull_downloads_from_remote(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)

    res = run("pull", "P1", "--remote", "nas:archive", "--path", archive)
    assert res.exit_code == 0
    assert "Pulled" in res.stdout
    assert len(calls) == 1
    assert calls[0][0] == "copy"
    assert calls[0][1] == "nas:archive/Project/P0001 Alpha"
    assert calls[0][2] == str(archive / "Project" / "P0001 Alpha")
