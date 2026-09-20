"""Pure functions: naming, paths and output helpers, called directly.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

from pathlib import Path

import pytest

from gwarchive import naming, paths


@pytest.mark.parametrize("raw", ["P1", "P01", "P001", "P0001", "p1", " p0001 "])
def test_normalize_prefix_expands_short_forms(raw: str) -> None:
    assert naming.normalize_prefix(raw) == "P0001"


@pytest.mark.parametrize("raw", ["X1", "P12345", "", "P", "0001", "PP01", "P 1"])
def test_normalize_prefix_rejects_junk(raw: str) -> None:
    assert naming.normalize_prefix(raw) is None


def test_folder_prefix_reads_live_and_retired_names() -> None:
    assert naming.folder_prefix("P0001 Alpha") == "P0001"
    assert naming.folder_prefix("2020-01-01-P0002-Beta") == "P0002"
    assert naming.folder_prefix("not-a-prefix") is None


def test_folder_descriptor_reads_live_and_retired_names() -> None:
    assert naming.folder_descriptor("P0001 Alpha") == "Alpha"
    assert naming.folder_descriptor("2020-01-01-P0002-Beta") == "Beta"


def test_rename_preserving_prefix_keeps_the_identifier() -> None:
    assert naming.rename_preserving_prefix("P0001 Alpha", "Renamed") == "P0001 Renamed"
    assert naming.rename_preserving_prefix("2020-01-01-P0002-Beta", "Renamed") == "2020-01-01-P0002-Renamed"


def test_matches_pattern_exact_is_equality_not_substring() -> None:
    # 6.3: --exact used to be a case-sensitive substring search.
    assert naming.matches_pattern("P0001 Notes", "P0001 Notes", exact=True)
    assert not naming.matches_pattern("P0001 Notes", "Notes", exact=True)
    assert naming.matches_pattern("P0001 Notes", "notes", exact=False)


def test_ensure_directory_is_silent(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths.ensure_directory(archive / "Project" / "scratch")
    assert capsys.readouterr().out == ""
