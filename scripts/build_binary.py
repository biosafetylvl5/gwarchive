#!/usr/bin/env python3
"""Build dist/gwarchive-<os>-<arch>[.exe] -- one native executable, Python bundled.

Run: uv run --locked --no-dev --group binary python scripts/build_binary.py

This is the g.pyz's sibling for machines without Python 3.11+. It is per
platform, so the release workflow runs it once on each runner it targets;
PyInstaller does not cross-compile.

Three choices here are not the obvious ones:

1. The entry point is a generated two-line script, not src/gwarchive/__main__.py.
   Handed __main__.py directly, PyInstaller runs it as the top-level script and
   then imports the package's own __main__ a second time if anything asks for
   it. The stub is the same one build_zipapp.py writes, for the same reason.

2. `--collect-submodules rich`. rich loads its unicode width tables with
   importlib.import_module() on a computed name, which PyInstaller's static
   analysis cannot see; the binary then dies on the first table it renders.

3. The name carries the platform and no version, so
   releases/latest/download/gwarchive-linux-x86_64 is a stable URL. `program()`
   hints with the binary's own name, so the hints stay right even if it is never renamed.
"""

import hashlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "binary"
DIST = ROOT / "dist"

SYSTEMS = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
MACHINES = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "arm64", "aarch64": "arm64", "ARM64": "arm64"}


def target_name() -> str:
    system = SYSTEMS[platform.system()]
    machine = MACHINES[platform.machine()]
    suffix = ".exe" if system == "windows" else ""
    return f"gwarchive-{system}-{machine}{suffix}"


def main() -> int:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    DIST.mkdir(exist_ok=True)

    name = target_name()
    entry = STAGE / "gwarchive_entry.py"
    entry.write_text("from gwarchive.__main__ import main\n\nmain()\n")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--noconfirm",
            "--clean",
            "--log-level=WARN",
            "--name",
            Path(name).stem,
            "--collect-submodules",
            "rich",
            "--distpath",
            str(DIST),
            "--workpath",
            str(STAGE / "work"),
            "--specpath",
            str(STAGE),
            str(entry),
        ],
        check=True,
        cwd=ROOT,
    )

    out = DIST / name
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (DIST / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    print(f"{out.relative_to(ROOT)}  {out.stat().st_size / 1e6:.2f} MB")
    print(f"  sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
