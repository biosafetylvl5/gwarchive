"""The archive format: codec choice, hashing, member vetting.

Fixtures live in conftest.py. A test that guards a numbered finding cites it
as "N.M" and resolves against docs/history/accepted-plan.md;
test_structure.py enforces that.
"""

import shutil
from pathlib import Path

import pytest
from conftest import hostile_archive

from gwarchive import naming, tarball


def test_extraction_refuses_members_that_escape_the_folder(tmp_path: Path) -> None:
    """Path traversal and absolute members are dropped, not written."""
    obj = tmp_path / "hostile.tar.gz"
    hostile_archive(obj, ["../escape.txt", "/abs.txt", "sub/../../up.txt", "good.txt"])
    dest = tmp_path / "dest"
    dest.mkdir()

    assert tarball.extract_archive(obj, dest, "tar.gz") == 1
    assert [p.name for p in dest.rglob("*")] == ["good.txt"]
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "up.txt").exists()


def test_extraction_drops_reserved_members(tmp_path: Path) -> None:
    """An archive cannot overwrite local bookkeeping on the way in."""
    obj = tmp_path / "sneaky.tar.gz"
    hostile_archive(obj, [naming.TOMBSTONE_NAME, ".gwarchive-other", "sub/.gwarchive-nested", "good.txt"])
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / naming.TOMBSTONE_NAME).write_text('{"keep": "me"}')

    assert tarball.extract_archive(obj, dest, "tar.gz") == 1
    assert (dest / naming.TOMBSTONE_NAME).read_text() == '{"keep": "me"}'
    assert not (dest / ".gwarchive-other").exists()


def test_pick_codec_prefers_zstd_and_falls_back_to_gzip(monkeypatch: pytest.MonkeyPatch) -> None:
    """gzip is the fallback, not the default -- and the env can force either."""
    monkeypatch.setattr(shutil, "which", lambda _name: "/opt/homebrew/bin/zstd")
    assert tarball.pick_codec() == "tar.zst"

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert tarball.pick_codec() == "tar.gz"

    monkeypatch.setenv("GWARCHIVE_CODEC", "zstd")
    assert tarball.pick_codec() == "tar.zst"
    monkeypatch.setenv("GWARCHIVE_CODEC", "gzip")
    assert tarball.pick_codec() == "tar.gz"


def test_zstd_argv_carries_the_flags_streaming_needs() -> None:
    """-f so it never prompts, -q so it never fights the spinner."""
    assert tarball.zstd_argv(Path("/tmp/out.tar.zst")) == [
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

    members, raw = tarball.create_archive(src, obj, "tar.zst")
    tarball.verify_archive(obj, "tar.zst", members)
    assert (members, raw) == (3, 8201)

    dest = tmp_path / "dest"
    dest.mkdir()
    assert tarball.extract_archive(obj, dest, "tar.zst") == members
    assert (dest / "sub" / "a.txt").read_text() == "streamed\n"
    assert (dest / "b.bin").read_bytes() == (src / "b.bin").read_bytes()
