"""verify: the structural audit, its severities, --fix and --quarantine.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from conftest import run

from gwarchive import naming, tombstone


def test_verify_exits_nonzero_when_it_reports_errors(archive: Path) -> None:
    """3.4: verify used to report problems and exit 0."""
    (archive / "RandomDir").mkdir()
    result = run("verify", "--path", archive)
    assert result.exit_code == 1
    assert "Unrecognized directory" in result.stdout


def test_verify_passes_a_clean_archive(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    run("mksub", "P1", "Notes", "--path", archive)
    result = run("verify", "--path", archive)
    assert result.exit_code == 0
    assert "passed" in result.stdout


def test_verify_groups_its_findings(archive: Path) -> None:
    (archive / "RandomDir").mkdir()
    (archive / "Project" / "not-a-prefix").mkdir()
    result = run("verify", "--path", archive)
    assert "Root contents" in result.stdout
    assert "Naming" in result.stdout


def test_verify_checks_the_subfolder_convention(archive: Path, folder: Path) -> None:
    """3.5: the P0001.01 convention mksub enforces was never verified."""
    (folder / "P0001.9 Bad").mkdir()
    result = run("verify", "--path", archive)
    assert result.exit_code == 1
    assert "Subfolders" in result.stdout


def test_verify_allows_ordinary_working_subdirectories(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    (archive / "Project" / "P0001 Alpha" / "drafts").mkdir()
    assert run("verify", "--path", archive).exit_code == 0


def test_verify_reports_duplicate_prefixes(archive: Path, duplicates: None) -> None:
    """3.6: nothing in verify ever compared two folders."""
    result = run("verify", "--path", archive)
    assert result.exit_code == 1
    assert "A0001 is claimed by 2 folders" in result.stdout


def test_duplicate_prefixes_are_not_autofixable(archive: Path, duplicates: None) -> None:
    result = run("verify", "--fix", "--quarantine", "--yes", "--path", archive)
    assert result.exit_code == 1
    assert (archive / "Archive" / "A0001 One").is_dir()
    assert (archive / "Archive" / "A0001 Two").is_dir()


def test_verify_json_carries_every_issue_field(archive: Path) -> None:
    """verify --json is the only structured view of an Issue; nothing read it."""
    (archive / "RandomDir").mkdir()
    (archive / "Project" / "not-a-prefix").mkdir()
    result = run("verify", "--json", "--path", archive)
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"] == 2
    assert payload["notices"] == 0
    groups = {issue["group"] for issue in payload["issues"]}
    assert groups == {"Root contents", "Naming"}
    for issue in payload["issues"]:
        # Every key the report and the --fix pass rely on, and all of them JSON
        # scalars -- a Path here would make json.dumps raise inside emit_json.
        assert set(issue) == {"severity", "group", "message", "fix_hint", "resolution"}
        assert all(isinstance(value, str) for value in issue.values())
        assert issue["severity"] == "error"


def test_verify_json_records_a_resolution(archive: Path) -> None:
    shutil.rmtree(archive / "Old")
    payload = json.loads(run("verify", "--fix", "--json", "--path", archive).stdout)
    assert payload["ok"] is True
    assert [i["resolution"] for i in payload["issues"]] == ["created"]


def test_verify_json_marks_a_notice_as_a_notice(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    run("mv", "P1", "Archive", "--path", archive)
    payload = json.loads(run("verify", "--json", "--path", archive).stdout)
    assert payload["ok"] is True
    assert [i["severity"] for i in payload["issues"]] == ["notice"]


def test_verify_report_writes_every_finding(archive: Path, tmp_path: Path) -> None:
    """--report had no coverage either, and it renders the same Issue fields."""
    (archive / "RandomDir").mkdir()
    report = tmp_path / "report.txt"
    assert run("verify", "--report", report, "--path", archive).exit_code == 1
    text = report.read_text()
    assert "GWArchive Verification Report" in text
    assert str(archive) in text
    assert "1 errors, 0 notices, 1 findings total" in text
    assert "[error] Root contents: Unrecognized directory in root: RandomDir" in text


def test_verify_report_records_a_clean_archive(archive: Path, tmp_path: Path) -> None:
    report = tmp_path / "clean.txt"
    assert run("verify", "--report", report, "--path", archive).exit_code == 0
    assert "Verification passed" in report.read_text()


def test_verify_fix_creates_nothing_but_the_categories(tmp_path: Path) -> None:
    """--fix on a base that does not exist must converge in one run.

    It used to report ``0 errors, 1 resolved`` and exit 0 having created only
    the base -- collect_structure_issues returned early, so the five category
    findings were never raised for --fix to act on. This also catches a --fix
    that reconstructs its target by string-parsing an Issue message: a reworded
    message would leave a directory named after the message fragment.
    """
    base = tmp_path / "fresh"
    result = run("verify", "--fix", "--path", base)
    assert result.exit_code == 0
    assert sorted(p.name for p in base.iterdir()) == sorted(naming.CATEGORIES.values())
    # And a second run has nothing left to say.
    assert run("verify", "--path", base).exit_code == 0


def test_verify_fix_does_not_quarantine(archive: Path) -> None:
    """4.1: --fix used to relocate anything it did not recognize."""
    (archive / "RandomDir").mkdir()
    (archive / "stray.txt").write_text("keep me")
    result = run("verify", "--fix", "--path", archive)
    assert result.exit_code == 1
    assert (archive / "RandomDir").is_dir()
    assert (archive / "stray.txt").is_file()


def test_verify_fix_creates_missing_categories(tmp_path: Path) -> None:
    base = tmp_path / "half"
    base.mkdir()
    result = run("verify", "--fix", "--path", base)
    assert result.exit_code == 0
    for category in naming.CATEGORIES.values():
        assert (base / category).is_dir()


def test_quarantine_moves_only_with_consent(archive: Path, stray: None) -> None:
    declined = run("verify", "--quarantine", "--path", archive, input="n\n")
    assert (archive / "stray.txt").is_file()
    assert declined.exit_code == 1

    accepted = run("verify", "--quarantine", "--yes", "--path", archive)
    assert not (archive / "stray.txt").exists()
    assert accepted.exit_code == 0


def test_quarantine_is_dated_so_repeat_runs_do_not_nest(archive: Path, stray: None) -> None:
    run("verify", "--quarantine", "--yes", "--path", archive)
    dated = list((archive / "BACKUP").iterdir())
    assert len(dated) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", dated[0].name)
    assert (dated[0] / "stray.txt").is_file()


def test_verify_dry_run_changes_nothing(archive: Path, stray: None) -> None:
    result = run("verify", "--quarantine", "--dry-run", "--path", archive)
    assert "would move" in result.stdout
    assert (archive / "stray.txt").is_file()
    assert not (archive / "BACKUP").exists()


def test_cp_into_a_category_allocates_a_fresh_prefix(archive: Path) -> None:
    """A copy is a new thing: it cannot share a permanent identifier."""
    run("create", "M", "Assets", "--path", archive)
    run("create", "A", "Existing", "--path", archive)
    result = run("cp", "M1", "Archive", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Archive" / "A0002 Assets").is_dir()
    assert (archive / "Material" / "M0001 Assets").is_dir()
    assert run("verify", "--path", archive).exit_code == 0


def test_cp_to_a_new_descriptor_allocates_a_fresh_prefix(archive: Path) -> None:
    """The most natural way to duplicate a folder used to be refused outright.

    resolve_destination never applied fresh_number to the descriptor form, so
    the copy kept P0001 and check_prefix_available killed it -- blaming
    permanence for what was really a missing allocation.
    """
    run("create", "P", "Alpha", "--path", archive)
    (archive / "Project" / "P0001 Alpha" / "doc.txt").write_text("body")
    result = run("cp", "P1", "Alpha v2", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Project" / "P0001 Alpha" / "doc.txt").exists()
    assert (archive / "Project" / "P0002 Alpha v2" / "doc.txt").read_text() == "body"
    assert run("verify", "--path", archive).exit_code == 0


def test_verify_passes_after_a_cross_category_move(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    run("mv", "P1", "Archive", "--path", archive)
    result = run("verify", "--path", archive)
    assert result.exit_code == 0  # a notice, not an error
    assert "was created in Project" in result.stdout


def test_full_round_trip(tmp_path: Path) -> None:
    base = tmp_path / "roundtrip"
    assert run("init", "--path", base).exit_code == 0
    assert run("create", "P", "Test Project", "--path", base).exit_code == 0
    assert run("mksub", "P1", "Notes", "--path", base).exit_code == 0
    assert (base / "Project" / "P0001 Test Project" / "P0001.01 Notes").is_dir()
    assert run("list", "P", "--path", base).exit_code == 0
    assert run("find", "Test", "--path", base).exit_code == 0
    assert run("stats", "--path", base).exit_code == 0
    assert run("verify", "--path", base).exit_code == 0


def test_verify_detects_corrupted_tombstone_and_half_offload(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    run("create", "P", "Beta", "--path", archive)

    # Corrupt tombstone in Alpha
    alpha_meta = archive / "Project" / "P0001 Alpha" / naming.TOMBSTONE_NAME
    alpha_meta.write_text("{bad json")

    # Half-offload in Beta (marked offloaded but files still present)
    beta_meta = archive / "Project" / "P0002 Beta" / naming.TOMBSTONE_NAME
    beta_meta.write_text(json.dumps({"offloaded_at": "2026-09-04T12:00:00"}))
    (archive / "Project" / "P0002 Beta" / "leftover.txt").write_text("still here")

    res = run("verify", "--path", archive)
    assert res.exit_code == 1
    assert "Corrupted offload metadata" in res.stdout
    assert "marked offloaded but has 1 file" in res.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("list", "P"),
        ("find", "Alpha"),
        ("stats",),
        ("verify",),
    ],
)
def test_quiet_leaves_json_the_only_thing_on_stdout(args: tuple[str, ...], archive: Path) -> None:
    """--json is a machine surface: nothing presentational may share stdout.

    This replaces the assertion that used to live in the removed theme-mode
    test, which was the only guard that a display mode cannot reach stdout.
    """
    run("create", "P", "Alpha", "--path", archive)
    result = run("--quiet", *args, "--json", "--path", archive)
    assert result.exit_code == 0
    # Parses cleanly, with no leading panel, prompt, spinner or trailing note.
    json.loads(result.stdout)
    assert result.stdout.lstrip()[0] in "[{"


def test_verify_flags_an_unreadable_archive_format(archive: Path) -> None:
    """Better a verify error than a confusing failure at extraction time."""
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    tombstone.write_tombstone(folder, {"archive": {"format": "tar.br", "versions": []}})

    res = run("verify", "--path", archive)
    assert res.exit_code == 1
    assert "unreadable archive format" in res.stdout + res.stderr
