#!/usr/bin/env python3
"""Build dist/g.pyz -- a single self-contained gwarchive, dependencies vendored.

Run: uv run python scripts/build_zipapp.py

Four choices here are not the obvious ones, and each was measured:

1. zipapp.create_archive() from Python, NOT `python -m zipapp`. The CLI does not
   expose `filter=`, so it cannot exclude __pycache__, *.dist-info or bin/.
   Those are not merely bloat: zipimport prefers bytecode, so a stale .pyc
   inside the archive ships a bug that reproduces only from the .pyz.

2. Dependencies come from uv.lock via `uv export`, not from `uv pip install
   --target typer rich`. --target ignores the lockfile entirely, so the shipped
   artifact would be built from whatever resolved that day -- the one thing
   committing a lock is supposed to prevent.

3. The staging directory is wiped every build. `uv pip install --target` into a
   dirty directory accumulates stale files silently.

4. NO bytecode is vendored. `compileall -b` measurably helps -- 0.069s cold
   start against 0.114s for the source-only build -- but a .pyc is locked to the
   CPython that wrote it, and the failure mode is `zipimport.ZipImportError: bad
   magic number`, raised inside the import system BEFORE __main__.py's version
   guard can say anything useful. A portable artifact that starts in a tenth of
   a second beats a fast one that dies on someone else's interpreter. Plain
   `compileall` is not an alternative: it writes into __pycache__, and
   zipimport's search order never looks there, so it is a no-op with a cost.

Mtimes are normalised to the ZIP epoch so the build is byte-reproducible;
create_archive already iterates sorted(), so ordering is deterministic already.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "pyz"
DIST = ROOT / "dist"
OUT = DIST / "g.pyz"
# 1980-01-02, not -01-01. A ZIP entry stores LOCAL time and cannot represent
# anything before 1980, so the exact epoch raises "ZIP does not support
# timestamps before 1980" for every builder west of UTC. One day of margin
# costs nothing and makes the build work in every timezone.
ZIP_EPOCH = 315619200


def _run(*args: str) -> None:
    subprocess.run(args, check=True, cwd=ROOT)


def _keep(arcname: Path) -> bool:
    parts = arcname.parts
    if "__pycache__" in parts:
        return False
    if parts[0] in {"bin", "Scripts"}:
        return False
    return not parts[0].endswith((".dist-info", ".data"))


def main() -> int:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    DIST.mkdir(exist_ok=True)

    reqs = STAGE.parent / "requirements.txt"
    with reqs.open("w") as fh:
        subprocess.run(
            ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes"],
            check=True,
            cwd=ROOT,
            stdout=fh,
        )
    _run("uv", "pip", "install", "-r", str(reqs), "--target", str(STAGE), "--no-compile-bytecode", "-q")
    shutil.copytree(ROOT / "src" / "gwarchive", STAGE / "gwarchive")

    # zipapp needs a __main__.py at the archive root; the package's own lives
    # one level down and is the thing that carries the version guard.
    (STAGE / "__main__.py").write_text("from gwarchive.__main__ import main\n\nmain()\n")

    for p in sorted(STAGE.rglob("*")):
        os.utime(p, (ZIP_EPOCH, ZIP_EPOCH))
    os.utime(STAGE, (ZIP_EPOCH, ZIP_EPOCH))

    zipapp.create_archive(
        STAGE, target=OUT, interpreter="/usr/bin/env python3", compressed=True, filter=_keep
    )
    OUT.chmod(0o755)

    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    (DIST / "g.pyz.sha256").write_text(f"{digest}  g.pyz\n")

    staged = sum(p.stat().st_size for p in STAGE.rglob("*.py"))
    print(f"{OUT.relative_to(ROOT)}  {OUT.stat().st_size / 1e6:.2f} MB compressed")
    print(f"  from {staged / 1e6:.2f} MB of Python source")
    print(f"  sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
