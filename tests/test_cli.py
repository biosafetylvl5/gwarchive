"""Cross-cutting CLI behaviour: exit codes, --json, --quiet, --version.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

from pathlib import Path

from conftest import run

import gwarchive
from gwarchive.commands import browse


def test_version_flag(archive: Path) -> None:
    result = run("--version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"gwarchive {gwarchive.__version__}"


def test_cd_on_an_ambiguous_prefix_fails_silently(archive: Path, duplicates: None) -> None:
    """cd must stay quiet -- its stdout is substituted into a shell command."""
    result = run("cd", "A1", "--path", archive)
    assert result.exit_code == 1
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == ""


def test_cd_is_silent_on_a_missing_prefix(archive: Path) -> None:
    result = run("cd", "P9", "--path", archive)
    assert result.exit_code == 1
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == ""


def test_list_function_is_not_named_list() -> None:
    """6.4: the command function used to shadow the builtin."""
    assert not isinstance(browse.list_folders, type(list))
    assert browse.list_folders.__name__ == "list_folders"
