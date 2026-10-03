"""The commands that make things: init, create, mksub.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

from pathlib import Path

import pytest
from conftest import run, runner

from gwarchive import options


def test_bracketed_names_survive_the_create_message(archive: Path) -> None:
    result = run("create", "P", "[WIP] Thing", "--path", archive)
    assert "[WIP] Thing" in result.stdout


def test_create_emits_exactly_one_line(archive: Path) -> None:
    """1.3: ensure_directory used to print alongside the command itself."""
    result = run("create", "P", "Alpha", "--path", archive)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "Created" in lines[0]


def test_paths_are_shown_relative_to_the_archive_root(archive: Path) -> None:
    result = run("create", "P", "Alpha", "--path", archive)
    assert "Project/P0001 Alpha" in result.stdout
    assert str(archive) not in result.stdout


def test_ambiguous_prefix_fails_instead_of_guessing(archive: Path, duplicates: None) -> None:
    """4.4: the first iterdir() match used to win, silently."""
    result = run("mksub", "A1", "Notes", "--path", archive)
    assert result.exit_code == 1
    assert "ambiguous" in result.stderr


def test_quiet_suppresses_success_output(archive: Path) -> None:
    result = runner.invoke(options.app, ["--quiet", "create", "P", "Alpha", "--path", str(archive)])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""
    assert (archive / "Project" / "P0001 Alpha").is_dir()


def test_verbose_surfaces_detail_output(archive: Path) -> None:
    """detail() is invisible without --verbose, which nothing exercised."""
    quiet_run = run("init", "--path", archive)
    assert "exists" not in quiet_run.stdout
    loud = run("--verbose", "init", "--path", archive)
    assert "exists" in loud.stdout


def test_a_file_where_a_directory_belongs_is_a_message(archive: Path) -> None:
    """ensure_directory tested exists(), so a file there read as success."""
    (archive / "Project" / "P0001 Alpha").write_text("not a directory")
    result = run("mksub", "P1", "Notes", "--path", archive)
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr


def test_allocate_number_refuses_to_overflow(archive: Path) -> None:
    (archive / "Project" / "P9999 Last").mkdir()
    result = run("create", "P", "One Too Many", "--path", archive)
    assert result.exit_code == 1
    assert "full" in result.stderr


def test_allocate_subnumber_refuses_to_overflow(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    (archive / "Project" / "P0001 Alpha" / "P0001.99 Last").mkdir()
    result = run("mksub", "P1", "One Too Many", "--path", archive)
    assert result.exit_code == 1
    assert "full" in result.stderr


def test_init_is_idempotent(archive: Path) -> None:
    result = run("init", "--path", archive)
    assert result.exit_code == 0
    assert "already initialized" in result.stdout


@pytest.mark.parametrize("name", ["a/b", "x\ny", "tab\there", "e\x1b[31mred", "bell\x07"])
def test_create_and_mksub_refuse_a_name_that_is_not_one_printable_component(archive: Path, name: str) -> None:
    """A newline put a folder past FOLDER_RE, so allocation stopped seeing its
    prefix and the next create reissued it; a slash made nested folders.
    Refused with exit 2 before anything is allocated.
    """
    res = run("create", "P", name, "--path", archive)
    assert res.exit_code == 2
    assert "\x1b" not in res.stderr and "\x07" not in res.stderr
    assert list((archive / "Project").iterdir()) == []

    run("create", "P", "Alpha", "--path", archive)
    res = run("mksub", "P1", name, "--path", archive)
    assert res.exit_code == 2
    assert list((archive / "Project" / "P0001 Alpha").iterdir()) == []


def test_a_newline_name_can_no_longer_reissue_a_prefix(archive: Path) -> None:
    run("create", "P", "x\ny", "--path", archive)
    run("create", "P", "Real", "--path", archive)
    assert [p.name for p in (archive / "Project").iterdir()] == ["P0001 Real"]
