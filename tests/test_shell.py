"""Shell integration: clears, cd, here, shell-init.

Fixtures live in conftest.py.
"""

import os
import shlex
import shutil
import subprocess
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


def test_here_names_the_folder_from_anywhere_inside_it(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run("create", "P", "Alpha", "--path", archive)
    deep = archive / "Project" / "P0001 Alpha" / "src" / "deep"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    result = run("here", "--path", archive)
    assert result.exit_code == 0
    assert result.stdout == "P1:Alpha\n"


def test_here_names_the_root_and_categories_as_a_drive(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(archive)
    assert run("here", "--path", archive).stdout == "G:\n"
    monkeypatch.chdir(archive / "Project")
    assert run("here", "--path", archive).stdout == "G:Project\n"


def test_here_deepest_names_the_subfolder(archive: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run("create", "P", "Alpha", "--path", archive)
    run("mksub", "P1", "Docs", "--path", archive)
    monkeypatch.chdir(archive / "Project" / "P0001 Alpha" / "P0001.01 Docs")
    assert run("here", "--path", archive).stdout == "P1:Alpha\n"
    assert run("here", "--deepest", "--path", archive).stdout == "P1.01:Docs\n"


def test_here_prints_nothing_where_it_has_no_name(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract as cd: the stdout is substituted, so a miss is silent."""
    (archive / "Project" / "scratch").mkdir()
    for where in (tmp_path, archive / "Project" / "scratch"):
        monkeypatch.chdir(where)
        result = run("here", "--path", archive)
        assert result.exit_code == 1
        assert result.stdout == ""


def test_shell_init_title_hook_is_on_by_default() -> None:
    assert "_gwarchive_title" in run("shell-init").stdout
    assert "_gwarchive_title" not in run("shell-init", "--no-title").stdout


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_the_title_hook_runs_in_a_real_shell(archive: Path, tmp_path: Path, shell: str) -> None:
    """Every other shell-init test greps the text; this one evals it.

    Checks the title for a folder and for the fallback outside the archive,
    that $? reaches whatever runs after the hook, and that a second eval --
    re-sourcing an rc file -- does not install the hook twice.
    """
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} is not installed")
    run("create", "P", "Alpha", "--path", archive)
    folder = archive / "Project" / "P0001 Alpha"
    init = tmp_path / "init.sh"
    init.write_text(run("shell-init", "--path", archive).stdout)
    hooks = "$precmd_functions" if shell == "zsh" else "$PROMPT_COMMAND"
    script = f"""
        PROMPT_COMMAND='echo mine'
        eval "$(cat {shlex.quote(str(init))})"
        eval "$(cat {shlex.quote(str(init))})"
        cd {shlex.quote(str(folder))}; false; _gwarchive_title; echo " rc=$?"
        cd {shlex.quote(str(tmp_path))}; _gwarchive_title; echo
        printf '%s' "{hooks}"
        TERM=dumb; cd /; _gwarchive_title
    """
    flags = ["-f"] if shell == "zsh" else []
    # TERM explicitly: bash sets an unset one to "dumb", which the hook skips.
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "TERM": "xterm-256color"}
    proc = subprocess.run([exe, *flags, "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.split("\n")
    assert lines[0] == "\x1b]0;P1:Alpha\x07 rc=1"
    assert lines[1] == f"\x1b]0;{tmp_path.name}\x07"
    installed = "\n".join(lines[2:])
    assert "\x1b" not in installed  # TERM=dumb: no title written
    assert installed.count("_gwarchive_title") == 1
    if shell == "bash":
        assert installed == "echo mine\n_gwarchive_title"
