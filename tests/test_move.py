"""The relocation verbs: mv, rename, cp, oldify -- and prefix permanence.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

from pathlib import Path

import pytest
import typer
from conftest import run, runner

from gwarchive import clock, naming, options, tombstone
from gwarchive.destination import check_not_nested


def _fs_is_case_insensitive(directory: Path) -> bool:
    """True when this filesystem folds case, the way APFS does by default."""
    probe = directory / "CaseProbeDir"
    probe.mkdir()
    try:
        return (directory / "caseprobedir").is_dir()
    finally:
        probe.rmdir()


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


def test_a_descriptor_with_a_separator_that_drops_the_prefix_is_refused(
    archive: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVIEW H8: this used to land the folder at <base>/Q1/Q2 Report, with its
    permanent P0001 prefix silently gone -- invisible to every lookup
    afterward, and its number free to be reissued. A literal path whose final
    component doesn't carry the source's prefix must be refused instead.
    """
    run("create", "P", "Alpha", "--path", archive)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = run("mv", "P1", "Q1/Q2 Report", "--path", archive)
    assert result.exit_code == 2
    assert "permanent prefix" in result.stderr
    assert list(elsewhere.iterdir()) == []
    assert not (archive / "Q1").exists()
    assert (archive / "Project" / "P0001 Alpha").is_dir()


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


# ---------------------------------------------------------------------------
# REVIEW C1 -- a target that is an ancestor of the source must never be
# deleted, and "", ".", ".." are not usable destinations.
# ---------------------------------------------------------------------------


def test_mv_force_into_the_current_directory_does_not_empty_it(archive: Path) -> None:
    """REVIEW C1: 'mv report.pdf . --force' used to rmtree the directory
    report.pdf itself lives in, taking every sibling file down with it.
    """
    (archive / "report.pdf").write_text("body")
    (archive / "other.txt").write_text("keep me")
    result = run("mv", archive / "report.pdf", ".", "--force", "--path", archive)
    assert result.exit_code == 2
    assert "--force" not in result.stderr
    assert (archive / "report.pdf").is_file()
    assert (archive / "other.txt").is_file()


@pytest.mark.parametrize("destination", ["", ".", "..", "  "])
def test_bare_dot_and_empty_descriptors_are_rejected(archive: Path, destination: str) -> None:
    """REVIEW C1: these descriptors resolve to an ancestor of the source, so
    they must be refused up front rather than reaching the delete path.
    """
    run("create", "P", "Alpha", "--path", archive)
    result = run("mv", "P1", destination, "--path", archive)
    assert result.exit_code == 2
    assert (archive / "Project" / "P0001 Alpha").is_dir()


def test_check_not_nested_refuses_a_target_that_is_an_ancestor_of_the_source(tmp_path: Path) -> None:
    """REVIEW C1: only the 'target is inside source' direction was refused;
    the opposite direction -- target is an ancestor of source -- was not.
    """
    source = tmp_path / "dl" / "report.pdf"
    source.parent.mkdir()
    source.write_text("body")
    with pytest.raises(typer.Exit):
        check_not_nested(source, source.parent)


# ---------------------------------------------------------------------------
# REVIEW C2 -- identity by inode, not string: case-only and
# Unicode-normalisation-only variants must never be deleted.
# ---------------------------------------------------------------------------


def test_check_no_overwrite_never_deletes_a_samefile_target(archive: Path) -> None:
    """REVIEW C2: a symlink alias stands in for a case/Unicode-normalisation
    variant on a case-folding volume -- portable across filesystems, since a
    real case-insensitive alias only exists on some of them. Either way the
    target is the *same file* as the source by inode, so it must never be
    deleted, even under force, and 'copy onto itself' must refuse.
    """
    from gwarchive.destination import check_no_overwrite

    real = archive / "File.txt"
    real.write_text("body")
    alias = archive / "alias.txt"
    alias.symlink_to(real)

    with pytest.raises(typer.Exit):
        check_no_overwrite(alias, archive, force=True, source_path=real)
    assert real.is_file()
    assert real.read_text() == "body"


def test_mv_case_only_rename_succeeds_without_force(archive: Path, folder: Path) -> None:
    """REVIEW C2: on a case-folding volume, 'mv P1 alpha' used to be refused
    as 'destination already exists', and --force turned that refusal into
    rmtree-ing the very folder it was supposed to rename.
    """
    if not _fs_is_case_insensitive(archive):
        pytest.skip("requires a case-insensitive filesystem")
    result = run("mv", "P1", "alpha", "--path", archive)
    assert result.exit_code == 0
    names = [p.name for p in (archive / "Project").iterdir()]
    assert names == ["P0001 alpha"]
    assert (archive / "Project" / "P0001 alpha" / "doc.txt").read_text() == "body\n"


def test_rename_case_only_variant_succeeds(archive: Path, folder: Path) -> None:
    """REVIEW C2: same bug, reached through 'rename' instead of 'mv'."""
    if not _fs_is_case_insensitive(archive):
        pytest.skip("requires a case-insensitive filesystem")
    result = run("rename", "P1", "alpha", "--path", archive)
    assert result.exit_code == 0
    names = [p.name for p in (archive / "Project").iterdir()]
    assert names == ["P0001 alpha"]


def test_mv_into_a_lowercase_category_alias_does_not_delete_the_source(archive: Path, folder: Path) -> None:
    """REVIEW C2: 'mv P1 project/ --force' resolved 'project' to the real
    Project directory on a case-folding volume; the old string-based identity
    check missed that it was the source's own category and rmtree'd it,
    deleting the source along with it.
    """
    if not _fs_is_case_insensitive(archive):
        pytest.skip("requires a case-insensitive filesystem")
    result = run("mv", "P1", "project/", "--force", "--path", archive)
    assert result.exit_code != 0
    assert folder.is_dir()
    assert list((archive / "Project").iterdir()) != []


# ---------------------------------------------------------------------------
# REVIEW C4 -- cp must not inherit the source's tombstone, and must refuse to
# copy an offloaded folder.
# ---------------------------------------------------------------------------


def test_cp_skips_reserved_metadata_at_any_depth(archive: Path, sample: Path) -> None:
    """REVIEW C4: cp used to copy .gwarchive-offload.json along with everything
    else, so pushing the copy could delete the original's only remote backup.
    'sample' carries a nested .gwarchive-cache/ too, at depth -- the packer and
    rclone already skip reserved names at any depth; cp must match that.
    """
    tombstone.write_tombstone(sample, {"prefix": "P0001", "remotes": ["nas:archive/Project/P0001 Alpha"]})
    result = run("cp", "P1", "Archive", "--path", archive)
    assert result.exit_code == 0
    copies = list((archive / "Archive").iterdir())
    assert len(copies) == 1
    copy = copies[0]
    assert not (copy / naming.TOMBSTONE_NAME).exists()
    assert not (copy / ".gwarchive-cache").exists()
    assert (copy / "notes.txt").is_file()
    assert (copy / "sub" / "deep.bin").is_file()


def test_cp_refuses_an_offloaded_folder(archive: Path, folder: Path) -> None:
    """REVIEW C4: an offloaded folder carries no local bytes worth copying --
    cp must refuse rather than produce a tombstone-only "copy" reported as a
    green success.
    """
    tombstone.write_tombstone(
        folder, {"prefix": "P0001", "offloaded_at": "2026-09-11T00:00:00", "remotes": ["nas:archive"]}
    )
    result = run("cp", "P1", "Archive", "--path", archive)
    assert result.exit_code == 1
    assert "offloaded" in result.stderr
    assert list((archive / "Archive").iterdir()) == []


def test_cp_dry_run_on_an_offloaded_folder_does_not_predict_success(archive: Path, folder: Path) -> None:
    """REVIEW C4 / AGENTS.md: a dry run must not predict success the real run
    refuses.
    """
    tombstone.write_tombstone(folder, {"prefix": "P0001", "offloaded_at": "2026-09-11T00:00:00"})
    result = run("cp", "P1", "Archive", "--dry-run", "--path", archive)
    assert result.exit_code == 0
    assert "Would copy" not in result.stdout


# ---------------------------------------------------------------------------
# REVIEW H4 -- oldify --date must normalise to zero-padded YYYY-MM-DD.
# ---------------------------------------------------------------------------


def test_oldify_normalizes_an_unpadded_date(archive: Path) -> None:
    """REVIEW H4: '--date 2026-1-5' used to write the raw string into the
    name, producing 'Old/2026-1-5-P0001-Beta' -- a name OLD_RE does not
    match, so the folder becomes invisible to every scan, cd and restore.
    """
    run("create", "P", "Beta", "--path", archive)
    result = run("oldify", "P1", "--date", "2026-1-5", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Old" / "2026-01-05-P0001-Beta").is_dir()
    assert naming.folder_prefix("2026-01-05-P0001-Beta") == "P0001"


# ---------------------------------------------------------------------------
# REVIEW H8 -- literal-path destinations: a trailing separator must name an
# existing directory, a same-as-category path routes through the category
# branch, and a prefix must never be silently dropped.
# ---------------------------------------------------------------------------


def test_trailing_slash_into_a_nonexistent_directory_is_refused(archive: Path) -> None:
    """REVIEW H8: a mistyped category name -- 'mv P1 Arkive/' for Archive/ --
    used to rename the folder to that literal misspelling, outside every
    category, prefix and descriptor stripped, and report a green 'Moved'.
    """
    run("create", "P", "Alpha", "--path", archive)
    result = run("mv", "P1", "Arkive/", "--path", archive)
    assert result.exit_code == 2
    assert (archive / "Project" / "P0001 Alpha").is_dir()
    assert not (archive / "Arkive").exists()


def test_cp_into_a_category_via_trailing_slash_allocates_a_fresh_number(archive: Path) -> None:
    """REVIEW H8: 'cp P1 Archive/' -- what tab completion produces -- used to
    skip the category branch entirely, so it never allocated a new prefix and
    died with 'already taken' the moment a same-prefixed folder existed there.
    """
    run("create", "A", "Existing", "--path", archive)
    run("create", "P", "Alpha", "--path", archive)
    result = run("cp", "P1", "Archive/", "--path", archive)
    assert result.exit_code == 0
    prefixes = sorted(naming.folder_prefix(p.name) or "" for p in (archive / "Archive").iterdir())
    assert prefixes == ["A0001", "A0002"]


def test_mv_trailing_slash_into_old_applies_the_date_stamp(archive: Path) -> None:
    """REVIEW H8: 'mv P1 Old/' used to skip the date stamp entirely, since the
    literal-path branch never ran the category logic.
    """
    run("create", "P", "Beta", "--path", archive)
    result = run("mv", "P1", "Old/", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Old" / f"{clock.today()}-P0001-Beta").is_dir()


def test_cp_into_a_lowercase_category_alias_does_not_duplicate_the_prefix(archive: Path) -> None:
    """REVIEW H8: 'cp P1 archive/' (case mismatch on macOS) used to skip the
    prefix check entirely, leaving two P0001 folders -- because the literal
    path 'archive' compared unequal to the category name 'Archive' as text.
    """
    if not _fs_is_case_insensitive(archive):
        pytest.skip("requires a case-insensitive filesystem")
    run("create", "P", "Alpha", "--path", archive)
    result = run("cp", "P1", "archive/", "--path", archive)
    assert result.exit_code == 0
    prefixes = [naming.folder_prefix(p.name) for p in (archive / "Archive").iterdir()]
    assert prefixes == ["A0001"]
    assert naming.folder_prefix((archive / "Project" / "P0001 Alpha").name) == "P0001"
