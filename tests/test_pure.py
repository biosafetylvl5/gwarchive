"""Pure functions: naming, paths and output helpers, called directly.

Fixtures live in conftest.py.
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
    # --exact used to be a case-sensitive substring search.
    assert naming.matches_pattern("P0001 Notes", "P0001 Notes", exact=True)
    assert not naming.matches_pattern("P0001 Notes", "Notes", exact=True)
    assert naming.matches_pattern("P0001 Notes", "notes", exact=False)


def test_ensure_directory_is_silent(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths.ensure_directory(archive / "Project" / "scratch")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("parts", "label"),
    [
        ((), "G:"),
        (("Project",), "G:Project"),
        (("old",), "G:Old"),
        (("Project", "P0028 GWArchive"), "P28:GWArchive"),
        (("Project", "P0028 GWArchive", "src", "deep"), "P28:GWArchive"),
        (("Project", "P0028 GWArchive", "P0028.01 Docs"), "P28:GWArchive"),
        (("Archive", "A0001 Gamma"), "A1:Gamma"),
        (("Project", "P0007"), "P7"),
        (("Old", "2026-01-05-P0001-Beta", "sub"), "Old:Beta"),
        (("Old", "2026-01-05-P0001-"), "Old:P1"),
        (("Project", "scratch"), None),
        (("BACKUP",), None),
        (("BACKUP", "P0001 Alpha"), None),
    ],
)
def test_location_label(parts: tuple[str, ...], label: str | None) -> None:
    assert naming.location_label(parts) == label


def test_location_label_deepest_names_the_innermost_subfolder() -> None:
    inside = ("Project", "P0028 GWArchive", "P0028.01 Docs", "drafts")
    assert naming.location_label(inside, deepest=True) == "P28.01:Docs"
    assert naming.location_label(("Project", "P0028 GWArchive", "P0028.02"), deepest=True) == "P28.02"
    # No subfolder to be deeper in: the top-level label, not None.
    assert naming.location_label(("Project", "P0028 GWArchive", "src"), deepest=True) == "P28:GWArchive"


def test_location_label_strips_what_could_escape_a_title_sequence() -> None:
    """The label is written inside an OSC title sequence. A folder name
    carrying ESC or BEL would end that sequence and begin one of its own.
    """
    label = naming.location_label(("Project", "P0001 Esc\x1b]0;pwned\x07 \x9b\udcff"))
    assert label == "P1:Esc]0;pwned"
    assert naming.location_label(("Project", "P0001 \x1b\x07")) == "P1"


def test_parts_below_matches_by_inode_through_a_symlinked_base(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "Project" / "P0001 Alpha").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    inside = (real / "Project" / "P0001 Alpha").resolve()
    assert paths.parts_below(inside, tmp_path / "link") == ("Project", "P0001 Alpha")
    assert paths.parts_below(real.resolve(), tmp_path / "link") == ()
    assert paths.parts_below(tmp_path.resolve(), real) is None
    assert paths.parts_below(inside, tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("text", "path", "problem"),
    [
        ("Plain name", False, None),
        ("[draft] Ünïcödé 👨\u200d👩\u200d👧", False, None),
        ("back\\slash", False, None),
        ("a/b", False, "separator"),
        ("./dir/P0001 x", True, None),
        ("x\ny", False, "control"),
        ("./x\x1b/y", True, "control"),
        ("del\x7f", False, "control"),
        ("c1\x9b", False, "control"),
    ],
)
def test_name_problem(text: str, path: bool, problem: str | None) -> None:
    found = naming.name_problem(text, path=path)
    assert (found is None) if problem is None else (found is not None and problem in found)
