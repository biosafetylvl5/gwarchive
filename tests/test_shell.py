"""Shell integration: clears, cd, shell-init.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

from pathlib import Path

import pytest
from conftest import run

from gwarchive import external


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
    monkeypatch.setattr(external, "run_pokeget", lambda args: None)
    result = run("clears")
    assert result.exit_code == 0


def test_shell_init_quotes_a_base_path_with_a_space(tmp_path: Path) -> None:
    """This repo's own path has a space in it, so the quoting is not academic."""
    base = tmp_path / "dir with space"
    base.mkdir()
    out = run("shell-init", "--path", base).stdout
    assert f"--path '{base}'" in out
    # And the interpreter and script paths are quoted the same way.
    assert out.count("'") >= 6
