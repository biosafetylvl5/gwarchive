"""list and find.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

import json
import re
from pathlib import Path

import pytest
from conftest import run


def test_bracketed_names_survive_display(archive: Path) -> None:
    """1.1: '[draft]' used to be parsed as a style tag and deleted."""
    run("create", "P", "[draft] Chapter [2]", "--path", archive)

    listed = run("list", "P", "--path", archive)
    assert "[draft] Chapter [2]" in listed.stdout

    found = run("find", "draft", "--path", archive)
    assert "[draft]" in found.stdout


@pytest.mark.parametrize("command", [("list",), ("create", "Thing")])
def test_category_arguments_accept_lower_case(command: tuple[str, ...], archive: Path) -> None:
    """2.2: 'create p X' worked while 'list p' failed."""
    result = run(command[0], "p", *command[1:], "--path", archive)
    assert result.exit_code == 0


def test_list_shows_a_prefix_you_can_feed_back_in(archive: Path) -> None:
    """2.4: the Number column used to show '0001'."""
    run("create", "P", "Alpha", "--path", archive)
    result = run("list", "P", "--path", archive)
    assert "P0001" in result.stdout

    match = re.search(r"P\d{4}", result.stdout)
    assert match is not None
    assert run("cd", match.group(0), "--path", archive).exit_code == 0


def test_empty_category_names_the_next_action(archive: Path) -> None:
    result = run("list", "P", "--path", archive)
    assert result.exit_code == 0
    assert "create P" in result.stdout


def test_list_on_an_uninitialized_archive_fails_with_a_hint(tmp_path: Path) -> None:
    result = run("list", "P", "--path", tmp_path / "nothing")
    assert result.exit_code == 1
    assert "gwarchive init" in result.stderr


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


def test_json_output_is_not_box_drawn(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("list", "P", "--json", "--path", archive)
    assert "\u2501" not in result.stdout
    assert "|" not in result.stdout


def test_tables_use_ascii_when_not_a_terminal(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("list", "P", "--path", archive)
    assert "\u2501" not in result.stdout


def test_find_exact_at_the_cli(archive: Path) -> None:
    """--exact was only ever tested through the pure function."""
    run("create", "P", "Alpha", "--path", archive)
    assert run("find", "Alph", "--path", archive).exit_code == 0
    assert run("find", "Alph", "--exact", "--path", archive).exit_code == 1
    assert run("find", "P0001 Alpha", "--exact", "--path", archive).exit_code == 0


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
