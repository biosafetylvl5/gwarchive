"""Tests for g.py.

Run with:  python3 -m pytest -v

Every test drives the CLI through ``CliRunner`` against a ``tmp_path``
archive. ``GWARCHIVE_BASE`` is also redirected in the autouse fixture, so a
command that forgets ``--path`` still cannot reach the real ~/gwarchive.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import g

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test touch the user's real archive."""
    monkeypatch.setenv("GWARCHIVE_BASE", str(tmp_path / "safety-net"))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERM", "dumb")
    # The --quiet/--verbose callback writes module globals; reset between tests.
    monkeypatch.setattr(g, "QUIET", False)
    monkeypatch.setattr(g, "VERBOSE", False)
    # Compression settings come from the environment too, so a developer's own
    # shell must not decide what the transfer tests exercise.
    for name in ("GWARCHIVE_COMPRESS", "GWARCHIVE_CODEC", "GWARCHIVE_KEEP"):
        monkeypatch.delenv(name, raising=False)


def run(*args: object, input: str | None = None) -> Result:  # noqa: A002
    return runner.invoke(g.app, [str(a) for a in args], input=input)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """An initialized archive with a couple of folders."""
    base = tmp_path / "archive"
    assert run("init", "--path", base).exit_code == 0
    return base


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["P1", "P01", "P001", "P0001", "p1", " p0001 "])
def test_normalize_prefix_expands_short_forms(raw: str) -> None:
    assert g.normalize_prefix(raw) == "P0001"


@pytest.mark.parametrize("raw", ["X1", "P12345", "", "P", "0001", "PP01", "P 1"])
def test_normalize_prefix_rejects_junk(raw: str) -> None:
    assert g.normalize_prefix(raw) is None


def test_folder_prefix_reads_live_and_retired_names() -> None:
    assert g.folder_prefix("P0001 Alpha") == "P0001"
    assert g.folder_prefix("2020-01-01-P0002-Beta") == "P0002"
    assert g.folder_prefix("not-a-prefix") is None


def test_folder_descriptor_reads_live_and_retired_names() -> None:
    assert g.folder_descriptor("P0001 Alpha") == "Alpha"
    assert g.folder_descriptor("2020-01-01-P0002-Beta") == "Beta"


def test_rename_preserving_prefix_keeps_the_identifier() -> None:
    assert g.rename_preserving_prefix("P0001 Alpha", "Renamed") == "P0001 Renamed"
    assert g.rename_preserving_prefix("2020-01-01-P0002-Beta", "Renamed") == "2020-01-01-P0002-Renamed"


def test_matches_pattern_exact_is_equality_not_substring() -> None:
    # 6.3: --exact used to be a case-sensitive substring search.
    assert g.matches_pattern("P0001 Notes", "P0001 Notes", exact=True)
    assert not g.matches_pattern("P0001 Notes", "Notes", exact=True)
    assert g.matches_pattern("P0001 Notes", "notes", exact=False)


# ---------------------------------------------------------------------------
# Phase 1 -- the window must not lie
# ---------------------------------------------------------------------------


def test_bracketed_names_survive_display(archive: Path) -> None:
    """1.1: '[draft]' used to be parsed as a style tag and deleted."""
    run("create", "P", "[draft] Chapter [2]", "--path", archive)

    listed = run("list", "P", "--path", archive)
    assert "[draft] Chapter [2]" in listed.stdout

    found = run("find", "draft", "--path", archive)
    assert "[draft]" in found.stdout


def test_bracketed_names_survive_the_create_message(archive: Path) -> None:
    result = run("create", "P", "[WIP] Thing", "--path", archive)
    assert "[WIP] Thing" in result.stdout


def test_create_emits_exactly_one_line(archive: Path) -> None:
    """1.3: ensure_directory used to print alongside the command itself."""
    result = run("create", "P", "Alpha", "--path", archive)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "Created" in lines[0]


def test_ensure_directory_is_silent(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    g.ensure_directory(archive / "Project" / "scratch")
    assert capsys.readouterr().out == ""


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


def test_paths_are_shown_relative_to_the_archive_root(archive: Path) -> None:
    result = run("create", "P", "Alpha", "--path", archive)
    assert "Project/P0001 Alpha" in result.stdout
    assert str(archive) not in result.stdout


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


# ---------------------------------------------------------------------------
# Phase 2 -- learnability
# ---------------------------------------------------------------------------


def test_version_flag(archive: Path) -> None:
    result = run("--version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"gwarchive {g.__version__}"


def test_rename_command_exists_and_keeps_the_prefix(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("rename", "P1", "Renamed", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Project" / "P0001 Renamed").is_dir()


@pytest.mark.parametrize("command", [("list",), ("create", "Thing")])
def test_category_arguments_accept_lower_case(command: tuple[str, ...], archive: Path) -> None:
    """2.2: 'create p X' worked while 'list p' failed."""
    result = run(command[0], "p", *command[1:], "--path", archive)
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "args",
    [
        ("list", "Q"),
        ("create", "Q", "Thing"),
        ("find", "x", "--category", "Q"),
        ("stats", "--category", "Q"),
    ],
)
def test_invalid_category_exits_two_instead_of_widening(args: tuple[str, ...], archive: Path) -> None:
    """2.2: find/stats used to silently search the whole archive."""
    result = run(*args, "--path", archive)
    assert result.exit_code == 2
    assert "not a category" in result.stderr + result.stdout


def test_cd_help_does_not_reference_a_nonexistent_binary() -> None:
    result = run("cd", "--help")
    assert "gwarchive cd" not in result.stdout
    assert "shell-init" in result.stdout


def test_shell_init_defines_both_gcd_and_ggd() -> None:
    result = run("shell-init")
    assert result.exit_code == 0
    assert "gcd()" in result.stdout
    assert "ggd()" in result.stdout


def test_shell_init_defines_clears_but_only_runs_it_with_greet() -> None:
    plain = run("shell-init")
    assert "clears()" in plain.stdout
    assert not plain.stdout.rstrip().endswith("clears")

    greeted = run("shell-init", "--greet")
    assert "clears()" in greeted.stdout
    assert greeted.stdout.rstrip().endswith("clears")


def test_clears_asks_pokeget_for_bulbasaur_with_name_hidden(pokeget: list[list[str]]) -> None:
    assert run("clears").exit_code == 0
    assert pokeget == [["bulbasaur", "--hide-name"]]


def test_clears_pokemon_choice_argument_beats_env(
    pokeget: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GWARCHIVE_POKEMON", "eevee")
    assert run("clears").exit_code == 0
    assert run("clears", "snorlax").exit_code == 0
    assert pokeget == [["eevee", "--hide-name"], ["snorlax", "--hide-name"]]


def test_clears_survives_a_missing_pokeget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "run_pokeget", lambda args: None)
    result = run("clears")
    assert result.exit_code == 0


def test_pep723_block_declares_the_dependencies() -> None:
    """The inline script metadata is what makes `uv run g.py` work after a wget."""
    source = Path(g.__file__).read_text()
    match = re.search(r"^# /// script$(.*?)^# ///$", source, re.MULTILINE | re.DOTALL)
    assert match is not None
    block = match.group(1)
    assert '"typer"' in block
    assert '"rich"' in block
    assert 'requires-python = ">=3.11"' in block


def test_bare_python_gets_a_pip_hint_not_a_traceback(tmp_path: Path) -> None:
    """The no-dependencies branch: run g.py where typer/rich can't import."""
    shim = tmp_path / "shims"
    shim.mkdir()
    for name in ("typer", "rich"):
        (shim / f"{name}.py").write_text("raise ModuleNotFoundError\n")
    proc = subprocess.run(
        [sys.executable, str(Path(g.__file__)), "--help"],
        env={**os.environ, "PYTHONPATH": str(shim)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "pip install typer rich" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stdout == ""


def test_bare_python_reexec_guard_prevents_infinite_loop(tmp_path: Path) -> None:
    """If _GWARCHIVE_REEXEC is already set and imports still fail, abort immediately."""
    shim = tmp_path / "shims"
    shim.mkdir()
    for name in ("typer", "rich"):
        (shim / f"{name}.py").write_text("raise ModuleNotFoundError\n")
    proc = subprocess.run(
        [sys.executable, str(Path(g.__file__)), "--help"],
        env={**os.environ, "PYTHONPATH": str(shim), "_GWARCHIVE_REEXEC": "1"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "still not importable" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_list_shows_a_prefix_you_can_feed_back_in(archive: Path) -> None:
    """2.4: the Number column used to show '0001'."""
    run("create", "P", "Alpha", "--path", archive)
    result = run("list", "P", "--path", archive)
    assert "P0001" in result.stdout

    match = re.search(r"P\d{4}", result.stdout)
    assert match is not None
    assert run("cd", match.group(0), "--path", archive).exit_code == 0


# ---------------------------------------------------------------------------
# Phase 3 -- feedback and exit codes
# ---------------------------------------------------------------------------


def test_empty_category_names_the_next_action(archive: Path) -> None:
    result = run("list", "P", "--path", archive)
    assert result.exit_code == 0
    assert "create P" in result.stdout


def test_list_on_an_uninitialized_archive_fails_with_a_hint(tmp_path: Path) -> None:
    result = run("list", "P", "--path", tmp_path / "nothing")
    assert result.exit_code == 1
    assert "g.py init" in result.stderr


def test_find_collapses_nested_matches(archive: Path) -> None:
    run("create", "M", "Notes", "--path", archive)
    run("mksub", "M1", "Notes", "--path", archive)
    result = run("find", "Notes", "--recursive", "--path", archive)
    assert result.exit_code == 0
    assert "1 match" in result.stdout
    assert "collapsed" in result.stdout


def test_find_exits_nonzero_with_no_matches(archive: Path) -> None:
    """3.4: grep convention."""
    result = run("find", "nothing-matches-this", "--path", archive)
    assert result.exit_code == 1


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
    assert sorted(p.name for p in base.iterdir()) == sorted(g.CATEGORIES.values())
    # And a second run has nothing left to say.
    assert run("verify", "--path", base).exit_code == 0


def test_stats_scales_size_units(archive: Path, folder: Path) -> None:
    """3.3: everything used to be reported in MB."""
    (folder / "big.bin").write_bytes(b"x" * 3_000_000)
    result = run("stats", "--path", archive)
    assert "MB" in result.stdout
    assert "0.00 MB" not in result.stdout


def test_stats_reports_zero_as_bytes_not_zero_mb(archive: Path) -> None:
    result = run("stats", "--path", archive)
    assert "0 bytes" in result.stdout


# ---------------------------------------------------------------------------
# Phase 4 -- safety
# ---------------------------------------------------------------------------


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
    for category in g.CATEGORIES.values():
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


def test_ambiguous_prefix_fails_instead_of_guessing(archive: Path, duplicates: None) -> None:
    """4.4: the first iterdir() match used to win, silently."""
    result = run("mksub", "A1", "Notes", "--path", archive)
    assert result.exit_code == 1
    assert "ambiguous" in result.stderr


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


# ---------------------------------------------------------------------------
# Phase 5 -- scriptability
# ---------------------------------------------------------------------------


def test_list_json(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("list", "P", "--json", "--path", archive)
    payload = json.loads(result.stdout)
    assert payload == [
        {
            "prefix": "P0001",
            "name": "Alpha",
            "path": "Project/P0001 Alpha",
            "modified": payload[0]["modified"],
        }
    ]


def test_find_json_and_exit_code(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    hit = run("find", "Alpha", "--json", "--path", archive)
    assert hit.exit_code == 0
    assert json.loads(hit.stdout)[0]["prefix"] == "P0001"

    miss = run("find", "zzz", "--json", "--path", archive)
    assert miss.exit_code == 1
    assert json.loads(miss.stdout) == []


def test_stats_json(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("stats", "--json", "--path", archive)
    payload = json.loads(result.stdout)
    assert payload["total"]["folders"] == 1
    assert "size_human" in payload["total"]


def test_json_output_is_not_box_drawn(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("list", "P", "--json", "--path", archive)
    assert "\u2501" not in result.stdout
    assert "|" not in result.stdout


def test_tables_use_ascii_when_not_a_terminal(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("list", "P", "--path", archive)
    assert "\u2501" not in result.stdout


def test_quiet_suppresses_success_output(archive: Path) -> None:
    result = runner.invoke(g.app, ["--quiet", "create", "P", "Alpha", "--path", str(archive)])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""
    assert (archive / "Project" / "P0001 Alpha").is_dir()


def test_quiet_still_reports_errors(archive: Path) -> None:
    result = runner.invoke(g.app, ["--quiet", "mv", "P9", "Archive", "--path", str(archive)])
    assert result.exit_code == 1
    assert "Source not found" in result.stderr


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
    versions = (g.read_tombstone(sample) or {})["archive"]["versions"]  # type: ignore[index]
    assert len(versions) == 1


def test_shell_init_quotes_a_base_path_with_a_space(tmp_path: Path) -> None:
    """This repo's own path has a space in it, so the quoting is not academic."""
    base = tmp_path / "dir with space"
    base.mkdir()
    out = run("shell-init", "--path", base).stdout
    assert f"--path '{base}'" in out
    # And the interpreter and script paths are quoted the same way.
    assert out.count("'") >= 6


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


def test_find_exact_at_the_cli(archive: Path) -> None:
    """--exact was only ever tested through the pure function."""
    run("create", "P", "Alpha", "--path", archive)
    assert run("find", "Alph", "--path", archive).exit_code == 0
    assert run("find", "Alph", "--exact", "--path", archive).exit_code == 1
    assert run("find", "P0001 Alpha", "--exact", "--path", archive).exit_code == 0


def test_verbose_surfaces_detail_output(archive: Path) -> None:
    """detail() is invisible without --verbose, which nothing exercised."""
    quiet_run = run("init", "--path", archive)
    assert "exists" not in quiet_run.stdout
    loud = run("--verbose", "init", "--path", archive)
    assert "exists" in loud.stdout


# ---------------------------------------------------------------------------
# Phase 6 -- correctness
# ---------------------------------------------------------------------------


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


def test_cp_into_a_category_allocates_a_fresh_prefix(archive: Path) -> None:
    """A copy is a new thing: it cannot share a permanent identifier."""
    run("create", "M", "Assets", "--path", archive)
    run("create", "A", "Existing", "--path", archive)
    result = run("cp", "M1", "Archive", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Archive" / "A0002 Assets").is_dir()
    assert (archive / "Material" / "M0001 Assets").is_dir()
    assert run("verify", "--path", archive).exit_code == 0


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
    assert (archive / "Old" / f"{g.today()}-P0001-Alpha").is_dir()
    assert not (archive / "Project" / "P0001 old").exists()


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


def test_a_search_pattern_is_never_parsed_as_markup(archive: Path) -> None:
    """The table title took a bare str, which Rich markup-parses.

    `find "[draft]"` printed a title with the brackets eaten, and a pattern
    holding a close tag raised MarkupError *after* the search had succeeded --
    a traceback instead of results.
    """
    run("create", "P", "Alpha [draft]", "--path", archive)
    found = run("find", "[draft]", "--path", archive)
    assert found.exit_code == 0
    assert "[draft]" in found.stdout

    hostile = run("find", "[/bold]nothing", "--path", archive)
    assert hostile.exit_code == 1
    assert "Traceback" not in hostile.stderr + hostile.stdout


def test_a_file_where_a_directory_belongs_is_a_message(archive: Path) -> None:
    """ensure_directory tested exists(), so a file there read as success."""
    (archive / "Project" / "P0001 Alpha").write_text("not a directory")
    result = run("mksub", "P1", "Notes", "--path", archive)
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr


def test_list_function_is_not_named_list() -> None:
    """6.4: the command function used to shadow the builtin."""
    assert not isinstance(g.list_folders, type(list))
    assert g.list_folders.__name__ == "list_folders"


# --- 6.5: prefixes are permanent -------------------------------------------


def test_mv_across_categories_keeps_the_prefix(archive: Path) -> None:
    """The headline bug: this used to produce two A0001 folders."""
    run("create", "A", "Existing Archive Item", "--path", archive)
    run("create", "P", "Alpha", "--path", archive)

    result = run("mv", "P1", "Archive", "--path", archive)
    assert result.exit_code == 0
    assert (archive / "Archive" / "P0001 Alpha").is_dir()

    prefixes = sorted(g.folder_prefix(p.name) or "" for p in (archive / "Archive").iterdir())
    assert prefixes == ["A0001", "P0001"]


def test_cd_follows_a_folder_across_a_category_move(archive: Path) -> None:
    run("create", "A", "Existing Archive Item", "--path", archive)
    run("create", "P", "Alpha", "--path", archive)
    run("mv", "P1", "Archive", "--path", archive)

    moved = run("cd", "P1", "--path", archive)
    assert moved.stdout.strip().endswith("Archive/P0001 Alpha")

    original = run("cd", "A1", "--path", archive)
    assert original.stdout.strip().endswith("Archive/A0001 Existing Archive Item")


def test_verify_passes_after_a_cross_category_move(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    run("mv", "P1", "Archive", "--path", archive)
    result = run("verify", "--path", archive)
    assert result.exit_code == 0  # a notice, not an error
    assert "was created in Project" in result.stdout


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


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


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


def test_init_is_idempotent(archive: Path) -> None:
    result = run("init", "--path", archive)
    assert result.exit_code == 0
    assert "already initialized" in result.stdout


# ---------------------------------------------------------------------------
# Remote sync (push, offload, pull, restore)
# ---------------------------------------------------------------------------


@pytest.fixture
def folder(archive: Path) -> Path:
    """P0001 Alpha, created and holding one file. The plain-vanilla subject."""
    run("create", "P", "Alpha", "--path", archive)
    made = archive / "Project" / "P0001 Alpha"
    (made / "doc.txt").write_text("body\n")
    return made


@pytest.fixture
def duplicates(archive: Path) -> None:
    """Two folders claiming A0001 -- the invariant violation, pre-made."""
    for name in ("A0001 One", "A0001 Two"):
        (archive / "Archive" / name).mkdir()


@pytest.fixture
def stray(archive: Path) -> None:
    """An unrecognized file at the archive root, for --quarantine."""
    (archive / "stray.txt").write_text("junk")


@pytest.fixture
def pokeget(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The pokeget stand-in, and the calls it recorded."""
    calls: list[list[str]] = []

    def _fake(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args=["pokeget", *args], returncode=0)

    monkeypatch.setattr(g, "run_pokeget", _fake)
    return calls


@pytest.fixture
def rclone(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """Install an rclone stand-in and hand back the calls it recorded.

    ``rclone()`` succeeds; ``rclone(returncode=1)`` fails every call; and
    ``rclone(fail_on="bad:")`` fails only the calls naming that remote, which
    is how the multi-remote ordering guarantees are exercised.
    """

    def install(
        returncode: int = 0, stderr: str = "network unreachable", fail_on: str | None = None
    ) -> list[list[str]]:
        calls: list[list[str]] = []

        def _fake_run(
            args: Sequence[str], *, capture_output: bool = False
        ) -> subprocess.CompletedProcess[str]:
            calls.append(list(args))
            failed = returncode if fail_on is None else int(any(fail_on in a for a in args))
            return subprocess.CompletedProcess(
                args=["rclone", *args],
                returncode=failed,
                stdout="",
                stderr=stderr if failed else "",
            )

        monkeypatch.setattr(g, "run_rclone", _fake_run)
        return calls

    return install


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
    assert g.read_tombstone(archive / "Project" / "P0001 Alpha") is None

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
    assert not g.is_offloaded(archive / "Project" / "P0001 Alpha")


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
    g.write_tombstone(
        folder,
        {"prefix": "P0001", "offloaded_at": "2026-01-01T00:00:00", "remotes": ["unraid/Project/P0001 Alpha"]},
    )

    res = run(command, "P1", "--path", archive)
    assert res.exit_code == 2
    assert g.TOMBSTONE_NAME in res.stderr
    assert calls == []


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
    g.write_tombstone(
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


def test_push_drops_a_stray_recorded_remote_from_the_metadata(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    """A push heals metadata a laxer version poisoned instead of carrying it forward."""
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    g.write_tombstone(folder, {"prefix": "P0001", "remotes": ["unraid/Project/P0001 Alpha"]})

    res = run("push", "P1", "--remote", "nas:archive", "--path", archive)
    assert res.exit_code == 0
    meta = g.read_tombstone(folder)
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
    assert calls[0] == ["copy", folder_str, "nas:archive/Project/P0001 Alpha", *g.RCLONE_EXCLUDES]

    # Local file is still there (push is non-destructive)
    assert doc.exists()

    # Metadata file is created
    meta = g.read_tombstone(archive / "Project" / "P0001 Alpha")
    assert meta is not None
    assert meta["prefix"] == "P0001"
    assert meta["remotes"] == ["nas:archive/Project/P0001 Alpha"]
    assert "last_pushed_at" in meta
    assert "offloaded_at" not in meta  # not offloaded!
    assert not g.is_offloaded(archive / "Project" / "P0001 Alpha")


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
    meta = g.read_tombstone(archive / "Project" / "P0001 Alpha")
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
    before = g.read_tombstone(folder)
    assert before is not None and before["size"] == 1234

    calls.clear()
    res = run("push", "P", "--remote", "nas:archive", "--path", archive)
    assert res.exit_code == 0
    assert "Skipped" in res.stdout
    assert len(calls) == 0

    after = g.read_tombstone(folder)
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
    assert g.is_offloaded(archive / "Project" / "P0002 Beta")

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
    assert (folder / g.TOMBSTONE_NAME).exists()
    assert g.is_offloaded(folder)
    meta = g.read_tombstone(folder)
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
    assert not g.is_offloaded(archive / "Project" / "P0001 Alpha")


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


def test_restore_downloads_and_clears_offloaded_status(
    archive: Path,
    rclone: Callable[..., list[list[str]]],
) -> None:
    calls = rclone()
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    run("offload", "P1", "--remote", "nas:archive", "--yes", "--no-compress", "--path", archive)
    assert g.is_offloaded(folder)

    calls.clear()
    res = run("restore", "P1", "--no-compress", "--path", archive)
    assert res.exit_code == 0
    assert "Restored" in res.stdout
    assert len(calls) == 1
    assert calls[0][1] == "nas:archive/Project/P0001 Alpha"

    # Status cleared
    assert not g.is_offloaded(folder)
    meta = g.read_tombstone(folder)
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


def test_verify_detects_corrupted_tombstone_and_half_offload(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    run("create", "P", "Beta", "--path", archive)

    # Corrupt tombstone in Alpha
    alpha_meta = archive / "Project" / "P0001 Alpha" / g.TOMBSTONE_NAME
    alpha_meta.write_text("{bad json")

    # Half-offload in Beta (marked offloaded but files still present)
    beta_meta = archive / "Project" / "P0002 Beta" / g.TOMBSTONE_NAME
    beta_meta.write_text(json.dumps({"offloaded_at": "2026-09-04T12:00:00"}))
    (archive / "Project" / "P0002 Beta" / "leftover.txt").write_text("still here")

    res = run("verify", "--path", archive)
    assert res.exit_code == 1
    assert "Corrupted offload metadata" in res.stdout
    assert "marked offloaded but has 1 file" in res.stdout


def test_sync_rejects_subfolders(archive: Path, rclone: Callable[..., list[list[str]]]) -> None:
    """Sync works on top-level folders only; a subfolder would flatten on the remote."""
    rclone()
    run("create", "P", "Alpha", "--path", archive)
    run("mksub", "P1", "Notes", "--path", archive)
    sub = archive / "Project" / "P0001 Alpha" / "P0001.01 Notes"

    res = run("push", sub, "--remote", "nas:archive", "--path", archive)
    assert res.exit_code == 1
    assert "top-level" in res.stderr
    assert "g.py push P0001" in res.stderr


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
    real_rmtree = g.shutil.rmtree

    def spy_unlink(self: Path, missing_ok: bool = False) -> None:
        seen.append(g.is_offloaded(folder))
        real_unlink(self, missing_ok=missing_ok)

    real_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", spy_unlink)
    monkeypatch.setattr(g.shutil, "rmtree", real_rmtree)

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

    monkeypatch.setattr(g, "run_rclone", _fake_run)

    res = run(
        "offload", "P1", "--remote", "good:archive", "--remote", "bad:archive", "--yes", "--path", archive
    )
    assert res.exit_code == 1
    # Local files preserved, not offloaded...
    assert doc.exists()
    assert not g.is_offloaded(folder)
    # ...but the copy that did land is on record.
    meta = g.read_tombstone(folder)
    assert meta is not None
    assert "good:archive/Project/P0001 Alpha" in g.get_recorded_remotes(meta)


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
    assert not g.is_offloaded(archive / "Project" / "P0001 Alpha")


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


# ---------------------------------------------------------------------------
# Compressed transfer (one archive per folder, hashed, retained)
# ---------------------------------------------------------------------------


@pytest.fixture
def rclone_local(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """rclone replaced by the local file operations it would perform.

    Every remote in these tests is an absolute path, which ``is_remote_target``
    accepts, so the arguments rclone would be handed are already real paths.
    That makes a genuine push/offload -> pull/restore round trip possible with
    no network and no rclone installed -- which matters, because CI has neither
    rclone nor a guaranteed zstd.
    """
    calls: list[list[str]] = []

    def _fake_run(args: Sequence[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        returncode = 0
        stderr = ""
        try:
            if args[0] == "copyto":
                src, dst = Path(args[1]), Path(args[2])
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif args[0] == "copy":
                src, dst = Path(args[1]), Path(args[2])
                dst.mkdir(parents=True, exist_ok=True)
                for item in sorted(src.rglob("*")):
                    if item.is_file() and item.name != g.TOMBSTONE_NAME:
                        out = dst / item.relative_to(src)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, out)
            elif args[0] == "deletefile":
                Path(args[1]).unlink()
        except OSError as exc:
            returncode, stderr = 1, str(exc)
        return subprocess.CompletedProcess(
            args=["rclone", *args], returncode=returncode, stdout="", stderr=stderr
        )

    monkeypatch.setattr(g, "run_rclone", _fake_run)
    return calls


@pytest.fixture
def gzip_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the stdlib codec, so the real archive path runs with no binary."""
    monkeypatch.setattr(g, "pick_codec", lambda: "tar.gz")


@pytest.fixture
def sample(archive: Path) -> Path:
    """A folder with a nested subfolder and both reserved metadata shapes.

    Keeps the bracketed name: it is what guards folder names against being
    read as Rich markup on every path that renders one.
    """
    name = "Alpha [draft]"
    run("create", "P", name, "--path", archive)
    folder = archive / "Project" / f"P0001 {name}"
    (folder / "notes.txt").write_text("top level\n")
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.bin").write_bytes(bytes(range(256)) * 4)
    (folder / ".gwarchive-cache").mkdir()
    (folder / ".gwarchive-cache" / "junk").write_text("never travels\n")
    return folder


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
    assert g.is_offloaded(folder)
    assert sorted(p.name for p in folder.iterdir()) == [".gwarchive-cache", g.TOMBSTONE_NAME]

    assert run("restore", "P1", "--path", archive).exit_code == 0
    assert not g.is_offloaded(folder)
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
    assert sorted(p.name for p in folder.iterdir()) == [".gwarchive-cache", g.TOMBSTONE_NAME]
    assert g.is_offloaded(folder)

    # With a spare copy retained, the same failure points at it.
    meta = g.read_tombstone(folder) or {}
    versions = meta["archive"]["versions"]  # type: ignore[index]
    versions.append({**versions[0], "name": "P0001 Alpha [draft].20250101-000000.tar.gz"})
    g.write_tombstone(folder, meta)
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

    meta = g.read_tombstone(folder) or {}
    meta["archive"]["versions"][0].pop("sha256")  # type: ignore[index]
    g.write_tombstone(folder, meta)

    res = run("restore", "P1", "--path", archive)
    assert res.exit_code == 0
    assert "no recorded checksum" in res.stderr
    assert (folder / "notes.txt").read_text() == "top level\n"


def hostile_archive(path: Path, names: Sequence[str]) -> None:
    """An archive we did not write, carrying members we must refuse."""
    with tarfile.open(path, "w:gz") as tar:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)


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
    assert calls[0][3:] == g.RCLONE_EXCLUDES


def test_extraction_refuses_members_that_escape_the_folder(tmp_path: Path) -> None:
    """Path traversal and absolute members are dropped, not written."""
    obj = tmp_path / "hostile.tar.gz"
    hostile_archive(obj, ["../escape.txt", "/abs.txt", "sub/../../up.txt", "good.txt"])
    dest = tmp_path / "dest"
    dest.mkdir()

    assert g.extract_archive(obj, dest, "tar.gz") == 1
    assert [p.name for p in dest.rglob("*")] == ["good.txt"]
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "up.txt").exists()


def test_extraction_drops_reserved_members(tmp_path: Path) -> None:
    """An archive cannot overwrite local bookkeeping on the way in."""
    obj = tmp_path / "sneaky.tar.gz"
    hostile_archive(obj, [g.TOMBSTONE_NAME, ".gwarchive-other", "sub/.gwarchive-nested", "good.txt"])
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / g.TOMBSTONE_NAME).write_text('{"keep": "me"}')

    assert g.extract_archive(obj, dest, "tar.gz") == 1
    assert (dest / g.TOMBSTONE_NAME).read_text() == '{"keep": "me"}'
    assert not (dest / ".gwarchive-other").exists()


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
    assert "archive" not in (g.read_tombstone(folder) or {})


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
    assert "archive" in (g.read_tombstone(folder) or {})

    assert run("push", "P1", "--remote", remote, "--no-compress", "--path", archive).exit_code == 0
    assert "archive" not in (g.read_tombstone(folder) or {})


@pytest.fixture
def stamps(monkeypatch: pytest.MonkeyPatch) -> Callable[[Sequence[str]], None]:
    """Successive object stamps, so repeated pushes are distinguishable.

    The real stamp has second resolution; a test that pushed three times in one
    second would otherwise be asserting against one object name.
    """

    def install(values: Sequence[str]) -> None:
        remaining = list(values)
        monkeypatch.setattr(g, "archive_stamp", lambda: remaining.pop(0))

    return install


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
    versions = (g.read_tombstone(folder) or {})["archive"]["versions"]  # type: ignore[index]
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
    versions = (g.read_tombstone(folder) or {})["archive"]["versions"]  # type: ignore[index]
    assert [v["name"] for v in versions] == [
        "P0001 Aardvark.20260101-000003.tar.gz",
        "P0001 Alpha [draft].20260101-000002.tar.gz",
    ]
    # The oldest went, not the second-newest.
    deleted = [Path(call[1]).name for call in calls if call[0] == "deletefile"]
    assert deleted == ["P0001 Alpha [draft].20260101-000001.tar.gz"]
    # And the newest is what a bare restore reaches for.
    arch = (g.read_tombstone(folder) or {})["archive"]
    assert g.select_version(arch, None)["name"] == "P0001 Aardvark.20260101-000003.tar.gz"  # type: ignore[arg-type,index]


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
    meta = g.read_tombstone(folder) or {}
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
    versions = (g.read_tombstone(folder) or {})["archive"]["versions"]  # type: ignore[index]
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
    assert not (folder / g.TOMBSTONE_NAME).exists()


def test_pick_codec_prefers_zstd_and_falls_back_to_gzip(monkeypatch: pytest.MonkeyPatch) -> None:
    """gzip is the fallback, not the default -- and the env can force either."""
    monkeypatch.setattr(g.shutil, "which", lambda _name: "/opt/homebrew/bin/zstd")
    assert g.pick_codec() == "tar.zst"

    monkeypatch.setattr(g.shutil, "which", lambda _name: None)
    assert g.pick_codec() == "tar.gz"

    monkeypatch.setenv("GWARCHIVE_CODEC", "zstd")
    assert g.pick_codec() == "tar.zst"
    monkeypatch.setenv("GWARCHIVE_CODEC", "gzip")
    assert g.pick_codec() == "tar.gz"


def test_zstd_argv_carries_the_flags_streaming_needs() -> None:
    """-f so it never prompts, -q so it never fights the spinner."""
    assert g.zstd_argv(Path("/tmp/out.tar.zst")) == [
        "-T0",
        "-3",
        "-q",
        "-f",
        "-o",
        "/tmp/out.tar.zst",
    ]


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")
def test_zstd_round_trips_a_real_archive(tmp_path: Path) -> None:
    """The streamed codec, end to end, where the binary actually exists."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.txt").write_text("streamed\n")
    (src / "b.bin").write_bytes(b"\x01\x02" * 4096)
    obj = tmp_path / "out.tar.zst"

    members, raw = g.create_archive(src, obj, "tar.zst")
    g.verify_archive(obj, "tar.zst", members)
    assert (members, raw) == (3, 8201)

    dest = tmp_path / "dest"
    dest.mkdir()
    assert g.extract_archive(obj, dest, "tar.zst") == members
    assert (dest / "sub" / "a.txt").read_text() == "streamed\n"
    assert (dest / "b.bin").read_bytes() == (src / "b.bin").read_bytes()


def test_verify_flags_an_unreadable_archive_format(archive: Path) -> None:
    """Better a verify error than a confusing failure at extraction time."""
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    g.write_tombstone(folder, {"archive": {"format": "tar.br", "versions": []}})

    res = run("verify", "--path", archive)
    assert res.exit_code == 1
    assert "unreadable archive format" in res.stdout + res.stderr


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

    monkeypatch.setattr(g.shutil, "disk_usage", lambda _path: NoRoom())

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

    real_create = g.create_archive

    def short_count(source: Path, out: Path, codec: str) -> tuple[int, int]:
        members, total = real_create(source, out, codec)
        return members + 1, total

    monkeypatch.setattr(g, "create_archive", short_count)

    res = run("offload", "P1", "--remote", tmp_path / "remote", "--yes", "--path", archive)
    assert res.exit_code == 1
    assert "expected" in res.stderr
    assert calls == []
    assert (folder / "notes.txt").read_text() == "top level\n"
    assert not g.is_offloaded(folder)


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
    size, count = g.compute_folder_stats(folder)
    summary = g.offload_summary([("Project", folder, size, count)], ["nas:archive"], archive, "tar.gz").plain
    assert "packs" in summary and "one tar.gz object per folder" in summary
    assert "needs" in summary and "$TMPDIR" in summary
    assert "frees" in summary
