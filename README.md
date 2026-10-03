# gwarchive

A command-line tool for organizing folders under the GWArchive naming standard.

Five categories, each a directory, each with a letter:

| Letter | Directory   | For                             |
| ------ | ----------- | ------------------------------- |
| `P`    | `Project`   | active work with an end         |
| `R`    | `Recurring` | work that comes back            |
| `M`    | `Material`  | reference that is not a project |
| `A`    | `Archive`   | finished, kept                  |
| `O`    | `Old`       | retired, date-stamped           |

Folders are named `P0001 Descriptor`, subfolders `P0001.01 Descriptor`, and
retired folders `YYYY-MM-DD-P0001-Descriptor`.

**A prefix is a permanent identifier**: allocated once, never reissued, and
kept across category moves, so `P0001` stays `P0001` in `Archive`. A copy gets
a fresh one. There is no index; the filesystem is the source of truth.

## Install

```bash
uv tool install gwarchive
```

Or run `g.pyz`, a self-contained zipapp, with no install:

```bash
./g.pyz --help
```

## Use

```bash
gwarchive init                        # create the five category directories
gwarchive create P "Thesis draft"     # -> Project/P0001 Thesis draft
gwarchive mksub P1 "Figures"          # -> Project/P0001 Thesis draft/P0001.01 Figures
gwarchive list P                      # a table, or --json
gwarchive find thesis                 # exits 1 on no match, so it composes like grep
gwarchive mv P1 Archive               # the prefix comes along
gwarchive oldify P1                   # retire into Old/ with today's date
gwarchive verify                      # audit the whole tree
```

Every mutating command takes `--dry-run`, whose output matches the real run's
(`Would move` vs `Moved`).

### Shell navigation

```bash
eval "$(gwarchive shell-init)"
gcd P1        # cd to the folder with that prefix, wherever it now lives
gwarchive here   # the folder you're in, short: P28:GWArchive
```

`shell-init` also sets the terminal title from `here` at each prompt:

| Where you are                           | Title                                             |
| --------------------------------------- | ------------------------------------------------- |
| `Project/P0028 GWArchive/src/...`       | `P28:GWArchive`                                   |
| `Project/P0028 GWArchive/P0028.01 Docs` | `P28:GWArchive` (`here --deepest`: `P28.01:Docs`) |
| `Old/2026-01-05-P0001-Beta`             | `Old:Beta`                                        |
| `Project/`                              | `G:Project`                                       |
| the archive root                        | `G:`                                              |
| anywhere else                           | the directory name                                |

It runs only when the directory changes, and not on `TERM=dumb`. Put the `eval`
after any prompt framework that sets titles (oh-my-zsh does), or pass
`--no-title`.

### Remote sync

Backed by [rclone](https://rclone.org); set `$GWARCHIVE_REMOTE` or pass
`--remote`:

```bash
gwarchive push P1        # upload, keep the local copy
gwarchive offload P1     # upload, delete locally, leave a tombstone
gwarchive pull P1        # fetch back
gwarchive restore P1     # fetch back and clear the tombstone
gwarchive restore P1 --keep-local   # clear the tombstone, fetch nothing
```

An offloaded folder keeps its name on disk while its bytes live on the remote.
Its tombstone, `.gwarchive-offload.json`, is also the version index; the remote
is never listed.

`push` refuses a folder marked offloaded that still holds local files (pulled
then edited, or an interrupted `offload`). `restore --keep-local` resolves it
from disk; `restore` fetches the remote's copy over it.

`pull` and `restore` ask once before overwriting local files. `-y`/`--yes`
skips the prompt; `--json` without `--yes` refuses instead.

Each folder is pushed as one `tar.zst` (`tar.gz` without `zstd`) beside its
mirror path. Even without the tombstone it is a plain archive:
`zstd -d < x.tar.zst | tar -tvf -`.

## Exit codes

| Code | Meaning                                                                      |
| ---- | ---------------------------------------------------------------------------- |
| `0`  | success                                                                      |
| `1`  | runtime failure — not found, ambiguous prefix, refused overwrite, no matches |
| `2`  | invalid input — a bad category, a bad date, a malformed `--remote`           |

Errors go to stderr. Under `--json`, stdout carries only the JSON.

## Environment

| Variable             | Effect                               |
| -------------------- | ------------------------------------ |
| `GWARCHIVE_BASE`     | archive root (default `~/gwarchive`) |
| `GWARCHIVE_REMOTE`   | default rclone target                |
| `GWARCHIVE_CODEC`    | force `zstd` or `gzip`               |
| `GWARCHIVE_COMPRESS` | default for `--no-compress`          |
| `GWARCHIVE_KEEP`     | archived copies retained per remote  |
| `GWARCHIVE_POKEMON`  | sprite for `clears`                  |

## Development

```bash
uv sync                  # installs the dev group
uv run pytest
uv run mypy
uv run ruff check . && uv run ruff format --check .
```

With Nix: `nix develop`, or `nix run github:biosafetylvl5/gwarchive -- --help`
to try it. Commits follow
[Conventional Commits](https://www.conventionalcommits.org/);
`pre-commit install` adds the check. Contributor notes are in
[AGENTS.md](AGENTS.md).
