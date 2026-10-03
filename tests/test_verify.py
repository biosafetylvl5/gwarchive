"""verify: the structural audit, its severities, --fix and --quarantine.

Fixtures live in conftest.py.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from conftest import run

from gwarchive import clock, naming, tombstone


def test_verify_exits_nonzero_when_it_reports_errors(archive: Path) -> None:
    """Verify used to report problems and exit 0."""
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
    """The P0001.01 convention mksub enforces was never verified."""
    (folder / "P0001.9 Bad").mkdir()
    result = run("verify", "--path", archive)
    assert result.exit_code == 1
    assert "Subfolders" in result.stdout


def test_verify_allows_ordinary_working_subdirectories(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    (archive / "Project" / "P0001 Alpha" / "drafts").mkdir()
    assert run("verify", "--path", archive).exit_code == 0


def test_verify_reports_duplicate_prefixes(archive: Path, duplicates: None) -> None:
    """Nothing in verify ever compared two folders."""
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
    """--fix used to relocate anything it did not recognize."""
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


def test_quarantine_is_stamped_not_dated(archive: Path, stray: None) -> None:
    """A date-only destination let two same-day runs collide (REVIEW H3).

    The directory now carries a full second-resolution stamp instead of just
    the date.
    """
    run("verify", "--quarantine", "--yes", "--path", archive)
    dated = list((archive / "BACKUP").iterdir())
    assert len(dated) == 1
    assert re.fullmatch(r"\d{8}-\d{6}", dated[0].name)
    assert (dated[0] / "stray.txt").is_file()


def test_quarantine_refuses_to_overwrite_an_earlier_run(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW H3: a same-second collision used to nest or overwrite silently.

    Two runs landing on the same stamp (forced here, since real collisions
    need two runs in the same second) must not let the second clobber or nest
    into what the first wrote -- it must refuse with a clear message and
    leave both the earlier quarantine and the new stray alone.
    """
    monkeypatch.setattr(clock, "archive_stamp", lambda: "20260101-000000")
    (archive / "stray.txt").write_text("first")
    first = run("verify", "--quarantine", "--yes", "--path", archive)
    assert first.exit_code == 0

    (archive / "stray.txt").write_text("second")
    second = run("verify", "--quarantine", "--yes", "--path", archive)
    assert second.exit_code == 1
    assert "already exists" in second.stderr

    assert (archive / "BACKUP" / "20260101-000000" / "stray.txt").read_text() == "first"
    assert (archive / "stray.txt").read_text() == "second"


def test_verify_dry_run_changes_nothing(archive: Path, stray: None) -> None:
    result = run("verify", "--quarantine", "--dry-run", "--path", archive)
    assert "would move" in result.stdout
    assert (archive / "stray.txt").is_file()
    assert not (archive / "BACKUP").exists()


def test_quarantine_dry_run_would_refuse_a_collision(archive: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dry run must not predict success the real run would refuse.

    See AGENTS.md's dry-run rule, and REVIEW H3's same-second collision case.
    """
    monkeypatch.setattr(clock, "archive_stamp", lambda: "20260101-000000")
    (archive / "stray.txt").write_text("first")
    run("verify", "--quarantine", "--yes", "--path", archive)

    (archive / "stray.txt").write_text("second")
    result = run("verify", "--quarantine", "--dry-run", "--path", archive)
    assert result.exit_code == 1
    assert "would refuse" in result.stdout
    assert "would move" not in result.stdout
    assert (archive / "BACKUP" / "20260101-000000" / "stray.txt").read_text() == "first"
    assert (archive / "stray.txt").read_text() == "second"


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


def _case_insensitive_fs(path: Path) -> bool:
    """True when this filesystem treats two names differing only by case as one."""
    probe = path / "CaseProbe"
    probe.mkdir()
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.rmdir()


def test_renamed_category_is_not_offered_for_quarantine(archive: Path) -> None:
    """REVIEW H3: a category renamed in place to another case is still that category.

    On a case-insensitive volume, renaming "Project" to "project" changes only
    the displayed casing -- same inode, same directory. A name-only comparison
    against the allowed set would flag it as an unrecognized root entry and
    offer to --quarantine it, which would move the whole category tree into
    BACKUP/.
    """
    if not _case_insensitive_fs(archive):
        pytest.skip("requires a case-insensitive filesystem")
    (archive / "Project").rename(archive / "project")
    result = run("verify", "--path", archive)
    assert result.exit_code == 0
    assert "Unrecognized" not in result.stdout


def test_lowercase_backup_is_not_offered_for_quarantine(archive: Path) -> None:
    """REVIEW H3: a root entry that is the same directory as BACKUP must not
    be quarantined into itself.
    """
    if not _case_insensitive_fs(archive):
        pytest.skip("requires a case-insensitive filesystem")
    (archive / "Backup").mkdir()
    result = run("verify", "--path", archive)
    assert result.exit_code == 0
    assert "Unrecognized" not in result.stdout


def test_distinct_lowercase_directory_is_still_a_stray(archive: Path) -> None:
    """REVIEW H3: on a case-sensitive filesystem, "project" really is a
    different, unrecognised directory and the identity check must not
    over-match on name alone.
    """
    if _case_insensitive_fs(archive):
        pytest.skip("requires a case-sensitive filesystem")
    (archive / "project").mkdir()
    result = run("verify", "--path", archive)
    assert result.exit_code == 1
    assert "Unrecognized directory in root: project" in result.stdout


def test_verify_flags_a_tombstone_prefix_that_does_not_match_its_folder(archive: Path) -> None:
    """REVIEW C4: cp copies the tombstone wholesale, so the copy's own prefix
    and its carried-over tombstone prefix can diverge.
    """
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    tombstone.write_tombstone(folder, {"prefix": "P0099"})

    res = run("verify", "--path", archive)
    assert res.exit_code == 1
    assert "P0099" in res.stdout
    assert "P0001" in res.stdout


def test_verify_json_includes_the_prefix_mismatch_finding(archive: Path) -> None:
    """REVIEW C4: the mismatch is a finding like any other, so --json carries it."""
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    tombstone.write_tombstone(folder, {"prefix": "P0099"})

    payload = json.loads(run("verify", "--json", "--path", archive).stdout)
    assert payload["ok"] is False
    assert any("P0099" in i["message"] for i in payload["issues"])


def test_fix_dry_run_does_not_count_as_resolved(tmp_path: Path) -> None:
    """REVIEW M6: a dry-run fix used to count as resolved and exit 0 having
    created nothing.
    """
    base = tmp_path / "unfixed"
    result = run("verify", "--fix", "--dry-run", "--path", base)
    assert result.exit_code == 1
    assert "would create" in result.stdout
    assert not base.exists()


def test_quarantine_dry_run_does_not_count_as_resolved(archive: Path, stray: None) -> None:
    """REVIEW M6: same bug, on the --quarantine side."""
    result = run("verify", "--quarantine", "--dry-run", "--path", archive)
    assert result.exit_code == 1
    assert "would move" in result.stdout


def test_verify_json_reports_dry_run(archive: Path, stray: None) -> None:
    """REVIEW M6: --json gained a top-level dry_run field."""
    payload = json.loads(run("verify", "--quarantine", "--dry-run", "--json", "--path", archive).stdout)
    assert payload["dry_run"] is True
    assert payload["ok"] is False

    payload = json.loads(run("verify", "--json", "--path", archive).stdout)
    assert payload["dry_run"] is False
