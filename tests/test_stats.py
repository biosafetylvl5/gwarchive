"""stats.

Fixtures live in conftest.py.
"""

import json
from pathlib import Path

import pytest
from conftest import run


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
    """The find and stats commands used to silently search the whole archive."""
    result = run(*args, "--path", archive)
    assert result.exit_code == 2
    assert "not a category" in result.stderr + result.stdout


def test_stats_scales_size_units(archive: Path, folder: Path) -> None:
    """Everything used to be reported in MB."""
    (folder / "big.bin").write_bytes(b"x" * 3_000_000)
    result = run("stats", "--path", archive)
    assert "MB" in result.stdout
    assert "0.00 MB" not in result.stdout


def test_stats_reports_zero_as_bytes_not_zero_mb(archive: Path) -> None:
    result = run("stats", "--path", archive)
    assert "0 bytes" in result.stdout


def test_stats_json(archive: Path) -> None:
    run("create", "P", "Alpha", "--path", archive)
    result = run("stats", "--json", "--path", archive)
    payload = json.loads(result.stdout)
    assert payload["total"]["folders"] == 1
    assert "size_human" in payload["total"]
