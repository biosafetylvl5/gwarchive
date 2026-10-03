# gwarchive

A command-line tool for organizing folders under the GWArchive naming standard.

Five categories, each a directory, each with a letter:

| Letter | Directory   | For |
|--------|-------------|-----|
| `P`    | `Project`   | active work with an end |
| `R`    | `Recurring` | work that comes back |
| `M`    | `Material`  | reference that is not a project |
| `A`    | `Archive`   | finished, kept |
| `O`    | `Old`       | retired, date-stamped |

Folders are named `P0001 Descriptor`, subfolders `P0001.01 Descriptor`, and
retired folders `YYYY-MM-DD-P0001-Descriptor`.

**A prefix is a permanent identifier.** It is allocated once, it is never
reissued, and it travels with the folder across category moves — so a folder
created in `Project` keeps its `P` prefix after it is moved into `Archive`. A
*copy* is a new thing and gets a fresh identifier. There is no index or
database; the filesystem is the source of truth.

## Install

```bash
uv tool install gwarchive
```

Or run a single self-contained artifact with no install at all — `g.pyz` vendors
its dependencies:

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

Every mutating command takes `--dry-run`, and dry-run output has the same shape
as the real thing (`Would move` vs `Moved`).

### Shell navigation

```bash
eval "$(gwarchive shell-init)"
gcd P1        # cd to the folder with that prefix, wherever it now lives
gwarchive here   # the folder you're in, short: P28:GWArchive
```

`shell-init` also sets the terminal title from `here` at each prompt:

| Where you are | Title |
|---|---|
| `Project/P0028 GWArchive/src/...` | `P28:GWArchive` |
| `Project/P0028 GWArchive/P0028.01 Docs` | `P28:GWArchive` (`here --deepest`: `P28.01:Docs`) |
| `Old/2026-01-05-P0001-Beta` | `Old:Beta` |
| `Project/` | `G:Project` |
| the archive root | `G:` |
| anywhere else | the directory name |

It asks `here` only when the directory changes, and is skipped on
`TERM=dumb`. Put the `eval` after a prompt framework that sets titles of its
own (oh-my-zsh does), or pass `--no-title` to leave the title alone.

### Remote sync

Backed by [rclone](https://rclone.org). Set `$GWARCHIVE_REMOTE` or pass
`--remote`:

```bash
gwarchive push P1        # upload, keep the local copy
gwarchive offload P1     # upload, delete locally, leave a tombstone
gwarchive pull P1        # fetch back
gwarchive restore P1     # fetch back and clear the tombstone
gwarchive restore P1 --keep-local   # clear the tombstone, keeping what's on disk, fetching nothing
```

An offloaded folder keeps its name and prefix on disk while the bytes live on
the remote. The tombstone it leaves — `.gwarchive-offload.json` — is also the
version index; the tool never lists the remote to find out what is there.

A folder that still holds local files despite being marked offloaded — pulled
and then edited, or left behind by an interrupted `offload` — is something
`push` now refuses rather than skipping as "already on the remote". Use
`restore --keep-local` to resolve it from what is already on disk, or `restore`
to fetch the remote's copy over it.

`pull` and `restore` ask before overwriting local files the remote also has,
with one prompt for the whole batch. Pass `-y`/`--yes` to skip it; `--json`
without `--yes` refuses rather than prompting, since there is nothing to
answer it with.

Folders are pushed as one compressed object (`tar.zst`, falling back to
`tar.gz` when `zstd` is absent), written as a *sibling* of the mirror path. The
extension is the codec's own on purpose: lose the tombstone entirely and
`zstd -d < x.tar.zst | tar -tvf -` is still a complete recovery path.

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | success |
| `1`  | runtime failure — not found, ambiguous prefix, refused overwrite, no matches |
| `2`  | invalid input — a bad category, a bad date, a malformed `--remote` |

Errors go to stderr. `--json` owns stdout: no spinner, no prompt, no trailing
receipt shares it.

## Environment

| Variable | Effect |
|----------|--------|
| `GWARCHIVE_BASE` | archive root (default `~/gwarchive`) |
| `GWARCHIVE_REMOTE` | default rclone target |
| `GWARCHIVE_CODEC` | force `zstd` or `gzip` |
| `GWARCHIVE_COMPRESS` | default for `--no-compress` |
| `GWARCHIVE_KEEP` | archived copies retained per remote |
| `GWARCHIVE_POKEMON` | sprite for `clears` |

## Development

```bash
uv sync                  # installs the dev group
uv run pytest
uv run mypy
uv run ruff check . && uv run ruff format --check .
```

Conventions, the module layering, and the rules that keep the test seams
working are in [AGENTS.md](AGENTS.md).
