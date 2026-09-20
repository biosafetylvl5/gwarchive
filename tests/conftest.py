"""Tests for g.py.

Run with:  python3 -m pytest -v

Every test drives the CLI through ``CliRunner`` against a ``tmp_path``
archive. ``GWARCHIVE_BASE`` is also redirected in the autouse fixture, so a
command that forgets ``--path`` still cannot reach the real ~/gwarchive.
"""

import io
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from gwarchive import clock, external, naming, options, output, tarball

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test touch the user's real archive."""
    monkeypatch.setenv("GWARCHIVE_BASE", str(tmp_path / "safety-net"))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERM", "dumb")
    # The --quiet/--verbose callback writes module globals; reset between tests.
    # They live in gwarchive.output, and the callback reaches them through
    # output.set_verbosity() -- patch them where they are defined, not where
    # they are read, or the reset writes a different module's names.
    monkeypatch.setattr(output, "QUIET", False)
    monkeypatch.setattr(output, "VERBOSE", False)
    # Compression settings come from the environment too, so a developer's own
    # shell must not decide what the transfer tests exercise.
    for name in ("GWARCHIVE_COMPRESS", "GWARCHIVE_CODEC", "GWARCHIVE_KEEP"):
        monkeypatch.delenv(name, raising=False)


def run(*args: object, input: str | None = None) -> Result:  # noqa: A002
    return runner.invoke(options.app, [str(a) for a in args], input=input)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """An initialized archive with a couple of folders."""
    base = tmp_path / "archive"
    assert run("init", "--path", base).exit_code == 0
    return base


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 1 -- the window must not lie
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 2 -- learnability
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 3 -- feedback and exit codes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 4 -- safety
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 5 -- scriptability
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 6 -- correctness
# ---------------------------------------------------------------------------


# --- 6.5: prefixes are permanent -------------------------------------------


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


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

    monkeypatch.setattr(external, "run_pokeget", _fake)
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

        monkeypatch.setattr(external, "run_rclone", _fake_run)
        return calls

    return install


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
                    if item.is_file() and item.name != naming.TOMBSTONE_NAME:
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

    monkeypatch.setattr(external, "run_rclone", _fake_run)
    return calls


@pytest.fixture
def gzip_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the stdlib codec, so the real archive path runs with no binary."""
    monkeypatch.setattr(tarball, "pick_codec", lambda: "tar.gz")


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


@pytest.fixture
def stamps(monkeypatch: pytest.MonkeyPatch) -> Callable[[Sequence[str]], None]:
    """Successive object stamps, so repeated pushes are distinguishable.

    The real stamp has second resolution; a test that pushed three times in one
    second would otherwise be asserting against one object name.
    """

    def install(values: Sequence[str]) -> None:
        remaining = list(values)
        monkeypatch.setattr(clock, "archive_stamp", lambda: remaining.pop(0))

    return install
