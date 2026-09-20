"""The relocation verbs: mv, rename, cp, oldify -- and prefix permanence.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

from pathlib import Path

import pytest
from conftest import run, runner

from gwarchive import clock, naming, options


def test_mv_into_a_missing_category_does_not_narrate(tmp_path: Path) -> None:
    """1.3: 'Created directory: .../Old' used to leak from the helper."""
    base = tmp_path / "partial"
    (base / "Project" / "P0001 Alpha").mkdir(parents=True)
    result = run("mv", "P0001", "Old", "--path", base)
    assert result.exit_code == 0
    assert "Created directory" not in result.stdout


def test_errors_go_to_stderr_and_stdout_stays_clean(archive: Path) -> None:
    """1.4: diagnostics used to land on stdout."""
    result = run("mv", "P9", "Archive", "--path", archive)
    assert result.exit_code == 1
    assert result.stdout.strip() == ""
    assert "Source not found" in result.stderr


def test_no_traceback_when_copying_onto_an_existing_folder(archive: Path) -> None:
    """1.5: this used to raise a raw FileExistsError."""
    run("create", "M", "Assets", "--path", archive)
    run("create", "P", "Alpha", "--path", archive)
    result = run("cp", "M0001", "P0001", "--path", archive)
    assert result.exit_code == 0
    again = run("cp", "M0001", "P0001", "--path", archive)
    assert again.exit_code == 1
    assert "Traceback" not in (again.stderr + again.stdout)
    assert "already exists" in again.stderr


def test_rename_command_exists_and_keeps_the_prefix(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("rename", "P1", "Renamed", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Project" / "P0001 Renamed").is_dir()


def test_mv_refuses_to_overwrite(archive: Path) -> None:
    # Unprefixed, deliberately: two folders sharing a prefix is unconstructible
    # under the permanent-identifier rule, so the prefix guard would fire first.
    (archive / "stray.txt").write_text("new")
    (archive / "Archive" / "stray.txt").write_text("old")
    result = run("mv", archive / "stray.txt", "Archive", "--path", archive)
    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert (archive / "Archive" / "stray.txt").read_text() == "old"
    assert (archive / "stray.txt").is_file()


def test_mv_force_overwrites(archive: Path) -> None:
    (archive / "stray.txt").write_text("new")
    (archive / "Archive" / "stray.txt").write_text("old")
    result = run("mv", archive / "stray.txt", "Archive", "--force", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Archive" / "stray.txt").read_text() == "new"
    assert not (archive / "stray.txt").exists()


def test_mv_refuses_a_move_that_would_duplicate_a_prefix(archive: Path) -> None:
    """6.5: the collision is refused up front, not resolved by overwriting."""
    run("create", "P", "Alpha", "--path", archive)
    (archive / "Archive" / "P0001 Impostor").mkdir()
    result = run("mv", archive / "Project" / "P0001 Alpha", "Archive", "--path", archive)
    assert result.exit_code == 1
    assert (archive / "Project" / "P0001 Alpha").is_dir()


def test_mv_dry_run_leaves_the_filesystem_alone(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    before = sorted(p.name for p in archive.rglob("*"))
    result = run("mv", "P1", "Archive", "--dry-run", "--path", archive)
    assert "Would move" in result.stdout
    assert sorted(p.name for p in archive.rglob("*")) == before


def test_mv_refuses_to_move_a_folder_into_itself(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("mv", "P1", "P1", "--path", archive)
    assert result.exit_code == 1


def test_quiet_still_reports_errors(archive: Path) -> None:
    result = runner.invoke(options.app, ["--quiet", "mv", "P9", "Archive", "--path", str(archive)])
    assert result.exit_code == 1
    assert "Source not found" in result.stderr


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("rename", "P1", "Renamed"), "Would rename"),
        (("cp", "P1", "A"), "Would copy"),
        (("oldify", "P1"), "Would move"),
    ],
)
def test_dry_run_output_keeps_the_shape_of_the_real_thing(
    archive: Path, folder: Path, args: tuple[str, ...], expected: str
) -> None:
    """AGENTS.md promises the same shape, differing only in the verb."""
    before = sorted(p.name for p in (archive / "Project").iterdir())
    result = run(*args, "--dry-run", "--path", archive)
    assert result.exit_code == 0
    assert expected in result.stdout
    assert "from" in result.stdout and "to" in result.stdout
    assert sorted(p.name for p in (archive / "Project").iterdir()) == before


def test_oldify_honors_the_date_flag(archive: Path) -> None:
    """6.1: --date was validated and then ignored."""
    run("create", "P", "Beta", "--path", archive)
    result = run("oldify", "P1", "--date", "2020-01-01", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Old" / "2020-01-01-P0001-Beta").is_dir()


def test_oldify_rejects_a_bad_date(archive: Path) -> None:
    run("create", "P", "Beta", "--path", archive)
    result = run("oldify", "P1", "--date", "01/01/2020", "--path", archive)
    assert result.exit_code == 2
    assert "Invalid date format" in result.stderr


def test_cp_into_a_prefix_destination(archive: Path, folder: Path) -> None:
    """6.2: 'cp foo P0001' used to create a literal ./P0001 directory."""
    run("create", "M", "Assets", "--path", archive)
    result = run("cp", "M1", "P1", "--path", archive)
    assert result.exit_code == 0
    assert (folder / "M0001 Assets").is_dir()
    assert not (archive / "P0001").exists()


def test_a_trailing_slash_destination_stays_inside_the_archive(
    archive: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mv P1 Archive/` is what tab-completion produces, and it used to escape.

    The literal-path form resolved relative paths against the shell's cwd, so
    the folder was renamed to ./Archive: outside the archive, prefix and
    descriptor stripped from the name, invisible to every lookup, and its
    number free to be reissued. It reported a green "Moved".
    """
    run("create", "P", "Alpha", "--path", archive)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run("mv", "P1", "Archive/", "--path", archive).exit_code == 0
    assert list(elsewhere.iterdir()) == []
    assert (archive / "Archive" / "P0001 Alpha").is_dir()


def test_a_descriptor_with_a_separator_stays_inside_the_archive(
    archive: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run("create", "P", "Alpha", "--path", archive)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run("mv", "P1", "Q1/Q2 Report", "--path", archive).exit_code == 0
    assert list(elsewhere.iterdir()) == []
    assert (archive / "Q1" / "Q2 Report").is_dir()


def test_an_unexpandable_home_is_a_message_not_a_traceback(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("mv", "P1", "~nosuchuser/x", "--path", archive)
    assert result.exit_code == 2
    assert "Traceback" not in result.stderr
    assert "no such user" in result.stderr


@pytest.mark.parametrize("destination", ["archive", "Archive", "a", "A"])
def test_a_category_destination_is_case_insensitive(destination: str, archive: Path) -> None:
    """`mv P1 archive` used to rename the folder to "archive", in place.

    The letter branch upper-cased its argument and the name branch compared
    exactly, so a lowercase category *name* fell past both and landed in the
    descriptor form -- reporting a move that never happened.
    """
    run("create", "P", "Alpha", "--path", archive)
    assert run("mv", "P1", destination, "--path", archive).exit_code == 0
    assert (archive / "Archive" / "P0001 Alpha").is_dir()
    assert not (archive / "Project" / "P0001 archive").exists()


def test_a_lowercase_old_still_retires(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    assert run("mv", "P1", "old", "--path", archive).exit_code == 0
    assert (archive / "Old" / f"{clock.today()}-P0001-Alpha").is_dir()
    assert not (archive / "Project" / "P0001 old").exists()


def test_mv_across_categories_keeps_the_prefix(archive: Path) -> None:
    """The headline bug: this used to produce two A0001 folders."""
    run("create", "A", "Existing Archive Item", "--path", archive)
    run("create", "P", "Alpha", "--path", archive)

    result = run("mv", "P1", "Archive", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Archive" / "P0001 Alpha").is_dir()

    prefixes = sorted(naming.folder_prefix(p.name) or "" for p in (archive / "Archive").iterdir())
    assert prefixes == ["A0001", "P0001"]


def test_cd_follows_a_folder_across_a_category_move(archive: Path) -> None:
    run("create", "A", "Existing Archive Item", "--path", archive)
    run("create", "P", "Alpha", "--path", archive)
    run("mv", "P1", "Archive", "--path", archive)

    moved = run("cd", "P1", "--path", archive)
    assert moved.stdout.strip().endswith("Archive/P0001 Alpha")

    original = run("cd", "A1", "--path", archive)
    assert original.stdout.strip().endswith("Archive/A0001 Existing Archive Item")


def test_create_does_not_reissue_a_retired_number(archive: Path) -> None:
    """6.5: create ignored Old/, so a retired number came back."""
    run("create", "P", "Alpha", "--path", archive)
    run("create", "P", "Beta", "--path", archive)
    run("oldify", "P2", "--date", "2020-01-01", "--path", archive)
    assert (archive / "Old" / "2020-01-01-P0002-Beta").is_dir()

    result = run("create", "P", "Gamma", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Project" / "P0003 Gamma").is_dir()
    assert not (archive / "Project" / "P0002 Gamma").exists()


def test_create_does_not_reissue_a_number_that_moved_away(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    run("mv", "P1", "Archive", "--path", archive)
    run("create", "P", "Beta", "--path", archive)
    assert (archive / "Project" / "P0002 Beta").is_dir()
