## 0.4.0 (2026-09-20)

- **Enhancement**: Install as a package, or run the self-contained g.pyz
- **Removal**: The install prompt for missing dependencies is gone

### Enhancement

#### Install as a package, or run the self-contained g.pyz

The tool is now `gwarchive` on your PATH rather than a `g.py` you fetch
and run. Install it with `uv tool install gwarchive` or `pipx install
gwarchive`.

If you liked having one file you could drop anywhere, that still exists:
`g.pyz` is a self-contained archive that vendors typer and rich, needs no
install, and runs on any Python 3.11 or newer. It is built and checksummed
by CI.

Nothing about the archive format, the naming standard, or the on-disk
tombstones changed. An archive managed by the previous version is read and
written identically by this one.




- `src/gwarchive/` (added)
- `pyproject.toml` (added)
- `scripts/build_zipapp.py` (added)
- `g.py` (deleted)
- `.github/workflows/` (modified)

### Removal

#### The install prompt for missing dependencies is gone

Running gwarchive on an interpreter without typer and rich no longer
offers to `pip install` them and re-exec. There are three supported ways
to get a working tool now -- a package install, the zipapp, or `uv run`
in a checkout -- and all three have the dependencies already.




- `g.py` (deleted)