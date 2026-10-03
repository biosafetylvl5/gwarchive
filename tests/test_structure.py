"""Guards on the package's shape rather than its behaviour.

Each of these replaces something that was tried as a text grep first and did
not work. The failures are recorded next to the tests, because "we already
tried that" is the useful part.
"""

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import gwarchive
from gwarchive import commands  # noqa: F401  registers the commands on `app`
from gwarchive.options import app

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "gwarchive"

# The machine-readable copy of the layering table in AGENTS.md. A module may
# import from its own layer or below, never above.
LAYER = {
    "__init__": 0,
    "naming": 0,
    "clock": 0,
    "output": 1,
    "paths": 2,
    "tombstone": 2,
    "external": 2,
    "destination": 3,
    "tarball": 3,
    "sync": 4,
    "options": 4,
    "commands": 5,
    "__main__": 6,
}

# Modules that wrap non-determinism. What actually has to be imported as a
# module is not the whole seam but the callables the suite PATCHES: a
# from-import binds the real function at import time and the double then has
# nothing to reach. Everything else in these modules -- constants, and helpers
# nobody patches -- is safe to from-import, and banning it outright would mean
# ~30 dotted calls that buy nothing.
#
# test_the_patched_set_is_complete keeps this honest: it reads the suite and
# fails if anything is patched that is not listed here.
SEAMS = {"clock", "external", "tarball"}
PATCHED = {
    "clock": {"today", "now_stamp", "archive_stamp"},
    "external": {"run_rclone", "run_zstd", "run_pokeget"},
    "tarball": {"pick_codec", "create_archive"},
}


def _modules() -> list[tuple[str, Path]]:
    out = []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC)
        out.append(("commands" if rel.parts[0] == "commands" and len(rel.parts) > 1 else p.stem, p))
    return out


def _package_imports(path: Path) -> list[tuple[str, bool, int]]:
    """(module, is_from_import, lineno) for every gwarchive import in a file."""
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("gwarchive"):
            parts = node.module.split(".")
            found.append((parts[1] if len(parts) > 1 else "__init__", True, node.lineno))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("gwarchive"):
                    parts = a.name.split(".")
                    found.append((parts[1] if len(parts) > 1 else "__init__", False, node.lineno))
    return found


def test_the_import_graph_points_downward() -> None:
    """Prevents a cycle, by catching the edge that would create one.

    Read as TEXT, never by importing gwarchive.* -- conftest has already
    resolved the whole graph by the time this runs, so an import-based check
    would be asserting about an outcome it caused.

    A substring version of this test failed by construction: naming.py contains
    TOMBSTONE_NAME = ".gwarchive-offload.json", so `assert "gwarchive" not in
    source` was false for a module that imports nothing.
    """
    for owner, path in _modules():
        for imported, _is_from, lineno in _package_imports(path):
            if imported not in LAYER:
                continue
            assert LAYER[imported] <= LAYER[owner], (
                f"{path.relative_to(ROOT)}:{lineno} — {owner} (layer {LAYER[owner]}) "
                f"imports up into {imported} (layer {LAYER[imported]})"
            )


def test_the_patched_functions_are_never_from_imported() -> None:
    """Prevents a monkeypatch that silently patches nothing.

    `from gwarchive.tarball import pick_codec` binds the real function into the
    importing module, so setattr(tarball, "pick_codec", ...) rebinds a name
    nobody reads. The test that relied on it then passes for the wrong reason --
    on CI, where zstd is absent, the real pick_codec returns "tar.gz" anyway, so
    all 23 gzip_codec tests would go green with their seam dead.

    A grep for `from gwarchive.external import` missed `from .external import
    run_rclone`; parsing imports catches both spellings.
    """
    for owner, path in _modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            parts = node.module.split(".")
            mod = parts[1] if len(parts) > 1 and parts[0] == "gwarchive" else None
            if mod not in PATCHED or owner == mod:
                continue
            for alias in node.names:
                if alias.name in PATCHED[mod]:
                    pytest.fail(
                        f"{path.relative_to(ROOT)}:{node.lineno} — from-import of "
                        f"{mod}.{alias.name}, which the suite patches. Import the "
                        f"module and call {mod}.{alias.name}() instead."
                    )


def test_the_patched_set_is_complete() -> None:
    """Prevents PATCHED going stale the moment someone adds a test double.

    This is what makes the narrower rule safe. A per-module ban would survive a
    new patch point automatically but costs ~30 dotted calls that protect
    nothing; this costs one line in PATCHED, and forgetting that line fails
    here rather than somewhere subtle later.
    """
    patched_in_suite: dict[str, set[str]] = {}
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        for mod, name in re.findall(r'setattr\(\s*(\w+)\s*,\s*"(\w+)"', path.read_text()):
            if mod in SEAMS:
                patched_in_suite.setdefault(mod, set()).add(name)
    for mod, names in patched_in_suite.items():
        missing = names - PATCHED[mod]
        assert not missing, (
            f"the suite patches {mod}.{sorted(missing)} but PATCHED does not list "
            f"{'it' if len(missing) == 1 else 'them'}; add it, and check no module "
            f"from-imports that name"
        )


def test_every_command_is_registered_in_help_order() -> None:
    """Prevents a dropped registration and a silent reorder.

    Typer lists commands in registration order and registration happens at
    import, so commands/__init__.py's import order IS this list. `ruff check
    --fix` merged and alphabetised those imports once already; .ruff.toml now
    carries a per-file-ignore, and this test is what makes a regression loud.

    `app` comes through gwarchive.__main__'s path, not straight from options,
    so a missing registration there is visible here.

    The shell version of this check, `gwarchive --help | grep -c '^  [a-z]'`,
    returns 0: Typer renders the command list inside a Rich box, so every line
    starts with a vertical bar.
    """
    expected = [
        "init",
        "create",
        "mksub",
        "mv",
        "rename",
        "cp",
        "oldify",
        "list",
        "find",
        "push",
        "offload",
        "pull",
        "restore",
        "stats",
        "verify",
        "clears",
        "cd",
        "here",
        "shell-init",
    ]
    actual = [c.name or (c.callback.__name__ if c.callback else "?") for c in app.registered_commands]
    assert actual == expected


def test_config_floors_agree() -> None:
    """Prevents ruff autofixing code into syntax the floor Python cannot parse.

    .ruff.toml is standalone on purpose, so ruff cannot infer target-version
    from requires-python. That makes this the only thing keeping the four
    declarations in step.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requires = pyproject["project"]["requires-python"]
    floor_match = re.search(r"(\d+)\.(\d+)", requires)
    assert floor_match, f"cannot read a floor out of requires-python = {requires!r}"
    floor = floor_match.groups()

    mypy_version = pyproject["tool"]["mypy"]["python_version"]
    assert tuple(mypy_version.split(".")) == floor, "[tool.mypy].python_version"

    ruff_target = re.search(r'target-version = "py(\d)(\d+)"', (ROOT / ".ruff.toml").read_text())
    assert ruff_target, ".ruff.toml has no target-version"
    assert ruff_target.groups() == floor, ".ruff.toml target-version"

    workflow = (ROOT / ".github" / "workflows" / "tests.yaml").read_text()
    legs = re.findall(r"['\"](\d+\.\d+(?:\.\d+)?)['\"]", workflow)
    assert legs, "no python-version matrix found in tests.yaml"
    lowest = min(tuple(int(x) for x in leg.split(".")) for leg in legs)
    assert lowest[:2] == tuple(int(x) for x in floor), "tests.yaml matrix floor"


def test_every_cited_finding_exists_in_the_record() -> None:
    """Prevents a citation that points at nothing, and pins the record's path.

    Tests that guard a numbered accepted-plan.md finding name it in a docstring
    or comment. Moving or renaming that file now fails here rather than
    silently orphaning 17 references.
    """
    record = ROOT / "docs" / "history" / "accepted-plan.md"
    assert record.exists(), f"the annotated record moved: {record}"
    headings = set(re.findall(r"^### (\d+\.\d+) ", record.read_text(), re.M))
    assert headings, "heading format changed; this test can no longer resolve citations"

    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        for cited in re.findall(r'(?:"""|#\s*)(\d+\.\d+):', path.read_text()):
            assert cited in headings, f"{path.name} cites finding {cited}, not in {record.name}"


def test_version_matches_package_metadata() -> None:
    """Prevents a half-finished `cz bump` from going unnoticed.

    This covers two of the version's three copies -- [project].version and
    __init__.__version__ -- which exist because commitizen needs a static literal
    and importlib.metadata raises PackageNotFoundError inside the zipapp. cz bump
    writes them; nothing else checks they agree. The --version test cannot: it
    compares the output to the same source of truth it came from.

    It matters beyond cosmetics -- __version__ is stamped into every tombstone
    written to a remote as "tool_version".
    """
    importlib_metadata = pytest.importorskip("importlib.metadata")
    try:
        installed = importlib_metadata.version("gwarchive")
    except importlib_metadata.PackageNotFoundError:
        pytest.skip("gwarchive is not installed as a distribution")
    assert installed == gwarchive.__version__

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == gwarchive.__version__


def test_the_lock_records_the_current_version() -> None:
    """Prevents a hand bump that only CI's `uv sync --locked` would notice.

    Locally `uv sync` relocks silently, so a stale lock surfaces only in CI.
    Separate from the test above so a missing distribution cannot skip it.
    """
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    (entry,) = [p for p in lock["package"] if p["name"] == "gwarchive"]
    assert entry["version"] == gwarchive.__version__, "run `uv lock`, or bump with `cz bump`"


def test_setup_uv_is_pinned_to_a_tag_that_exists() -> None:
    """Prevents a workflow that dies in "Set up job" before running anything.

    setup-uv publishes no floating major tags from v8 on; only a full `vX.Y.Z`
    tag or a commit SHA resolves.
    """
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        for ref in re.findall(r"astral-sh/setup-uv@(\S+)", workflow.read_text()):
            major = re.fullmatch(r"v(\d+)", ref)
            assert not (major and int(major.group(1)) >= 8), f"{workflow.name}: setup-uv@{ref}"


def test_the_emitted_launcher_actually_runs(tmp_path: Path) -> None:
    """Prevents shell-init emitting something that parses but cannot execute.

    Every other shell-init test greps the emitted text. None runs it -- which is
    how Path(__file__) survived into a package, where it named a submodule that
    exists and is not runnable. This is also the only check on the cd contract
    outside CliRunner: a real process, real stdout, nothing but the path.
    """
    from gwarchive.commands.shell import launcher

    env = {"GWARCHIVE_BASE": str(tmp_path), "PATH": "/usr/bin:/bin"}
    base = [*launcher()]
    subprocess.run([*base, "init"], check=True, capture_output=True, env=env)
    subprocess.run([*base, "create", "P", "Alpha"], check=True, capture_output=True, env=env)

    proc = subprocess.run([*base, "cd", "P1"], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(tmp_path / "Project" / "P0001 Alpha")
    assert proc.stderr == ""


def test_the_version_guard_parses_on_old_python() -> None:
    """Prevents the guard failing before it can report why.

    __main__.py must parse under the interpreters it exists to turn away. If it
    used syntax newer than its own floor, a 3.9 user would get a SyntaxError
    instead of the sentence explaining the problem.
    """
    source = (SRC / "__main__.py").read_text()
    head = source[: source.index("from gwarchive")]
    compile(head, "__main__.py", "exec")
    assert "sys.version_info" in head
    assert sys.version_info >= (3, 11)
