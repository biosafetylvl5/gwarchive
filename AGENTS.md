# AGENTS.md

## Project

gwarchive: a CLI for organizing files under the GWArchive naming standard, with
an rclone-backed remote sync layer. Packaged with hatchling, managed with uv;
the remote is `github.com/biosafetylvl5/gwarchive`.

```
src/gwarchive/
    __init__.py     __version__ and the package docstring. Imports no sibling.
    __main__.py     main(): the version guard, then registration, then app()
    naming.py       taxonomy, the 4 regexes, name parsing, reserved names
    clock.py        today / now_stamp / archive_stamp                    SEAM
    output.py       consoles, QUIET/VERBOSE, every emitter, die, program()
    paths.py        archive root, walking, prefix resolution, allocation
    tombstone.py    .gwarchive-offload.json read/write, tolerant JSON readers
    external.py     rclone / zstd / pokeget, remote target validation    SEAM
    destination.py  resolve_destination, the pre-flight guards, run_fs
    tarball.py      codecs, tar streaming, hashing, the version index    SEAM
    sync.py         staging, scratch space, retention, target resolution
    options.py      the Typer app, the root callback, 17 Annotated aliases
    commands/       structure, move, browse, upload, download, stats,
                    verify, shell -- and an __init__ whose import ORDER matters
```

## Running

```sh
uv sync                    # installs the dev group
uv run gwarchive --help
uv run pytest
uv run mypy
uv run ruff check . && uv run ruff format --check .
uv run vulture && uv run typos
```

That block is the gate; keep it green at every commit. `nix develop` provides
the same environment on the 3.11 floor (`.#py312`, `.#py313` for the other CI
legs), and `nix build` builds and tests the package.

## The two rules

Both are enforced by `tests/test_structure.py`; read its docstrings before
changing either.

**1. Imports point downward.** A module may import its own layer or below.

| Layer | Modules                          |
| ----- | -------------------------------- |
| 0     | `naming`, `clock`, `__init__`    |
| 1     | `output`                         |
| 2     | `paths`, `tombstone`, `external` |
| 3     | `destination`, `tarball`         |
| 4     | `sync`, `options`                |
| 5     | `commands/*`                     |
| 6     | `__main__`                       |

`output` is layer 1 because everything calls `die()` on it. The guard parses
imports with `ast`; a text search misfires on strings like `naming.py`'s
`".gwarchive-offload.json"`.

**2. Patched callables are imported as modules, never by name.**

| Module     | Patched                                 |
| ---------- | --------------------------------------- |
| `clock`    | `today`, `now_stamp`, `archive_stamp`   |
| `external` | `run_rclone`, `run_zstd`, `run_pokeget` |
| `tarball`  | `pick_codec`, `create_archive`          |

Write `tarball.pick_codec()`, not `from gwarchive.tarball import pick_codec`: a
from-import binds the real function, and the test double never reaches it.
Everything else in those modules may be from-imported. Calls within a seam
module resolve through its globals, so they need nothing.
`test_the_patched_set_is_complete` fails if the suite patches anything this
table omits — **add a new test double, add it there.**

The failure is silent: CI has no zstd, so a bypassed `pick_codec` patch still
returns `"tar.gz"` and all 23 `gzip_codec` tests pass with their seam dead.

## Conventions

- 4-space indent, `line-length = 110`, full annotations including `-> None`,
  tests included — so monkeypatch stand-ins cannot drift from the real
  signature.
- **Never interpolate user data into a markup string.** Use the output helpers
  or `Text(...)`; this once ate `[draft]` from folder names.
- Errors to stderr. Exit 0 success, 1 runtime failure, 2 invalid input.
- `--json` owns stdout: no spinner, no prompt, no trailing receipt.
- `cd` and `here` print only their result — bare `print()`, silent
  `sys.exit(1)` — because their stdout is substituted by `cd $(...)` and by
  shell-init's title hook. `here` also strips control characters for that hook.
- Every mutating command takes `--dry-run`, and dry-run output has the same
  shape as real output — except `init`, `create` and `mksub`, which allocate by
  scanning.
- A dry run must not predict success the real run refuses. `warn_if_taken` is
  the read-only half of `check_no_overwrite`, which deletes under `--force`.
- `program()` is what to type (hints); `launcher()` is what to execute (the
  emitted shell functions). They differ under the zipapp, which has no
  `gwarchive` on PATH.

## Things that look wrong and are not

- **`push` and `offload` are ~74% identical and stay separate**, so that `push`
  cannot delete local files. The reasoning is in `commands/upload.py`.
- **`pull` and `restore` share one body**: they drifted apart once, and three of
  four differences were bugs.
- **`clock.py` is 12 lines and stays its own module.** It is a seam; merged into
  `naming`, rule 2 would cover a module everything from-imports.
- **`.ruff.toml` is standalone**, and a `[tool.ruff]` table in pyproject would
  be ignored silently; see the comment in `pyproject.toml`.
  `test_config_floors_agree` keeps its `target-version` matching the floor.
- **Never run `typos -w`, or typos as a pre-commit hook.**
  `.gwarchive-offload.json`, `tool_version` and `offloaded_at` are wire format,
  and `Alph` in `tests/test_browse.py` is a deliberate partial match.
- **`commands/__init__.py`'s import order is the `--help` order**, hence its
  `I001` ignore. `test_every_command_is_registered_in_help_order` guards it.
- **The version is declared three times** — `[project].version`,
  `__init__.__version__`, `uv.lock` — because commitizen needs a static literal
  and `importlib.metadata` fails inside the zipapp. `cz bump` writes all three;
  never bump by hand.

## Remote sync

`$GWARCHIVE_REMOTE` or `--remote` (repeatable on push/offload). rclone honours a
colon only in the *first* path segment, so `unraid` or `backup/nas:` would be
local directories; `validate_remote_target` refuses them with exit 2.

One compressed object per folder, a **sibling** of the mirror path, with the
codec's own extension so it stays readable without the tombstone:

```
--no-compress:  nas:archive/Project/P0001 Alpha/...loose files...
compressed:     nas:archive/Project/P0001 Alpha.20260909-101530.tar.zst
```

`offload` writes its tombstone **before** deleting, so an interruption reads as
offloaded-with-leftovers rather than a silently emptied folder.

Extraction vets every member rather than trusting `tarfile`'s `filter="data"`:
that landed in 3.12 and was backported to 3.11.4, while the floor is 3.11.0 — so
the vulnerable range is exactly the range nothing exercises. Hence the `3.11.0`
leg in `tests.yaml`.

## Testing

- The autouse `isolate` fixture points `$GWARCHIVE_BASE` at a tmpdir and clears
  every `GWARCHIVE_*` variable by prefix, so no test reaches a real archive or
  inherits a developer's settings.
- Assert against `--json`, not rendered tables. A fixture sets `COLUMNS=200`
  and the table assertions depend on it.

## CI

- `tests.yaml`: 3.11.0/3.11/3.12/3.13 with `uv sync --locked`, plus a weekly
  unpinned canary.
- `static.yaml`: ruff, ruff-format, mypy, vulture and typos as separate jobs.
- `smoke.yaml`: the install, not the behaviour.
- `build.yaml`: wheel, sdist and `g.pyz`.
- `ci-pipeline.yaml` ("Check Code"): every pre-commit hook, then `cz check` from
  `COMMITIZEN_START_REV`.
- `package-and-publish.yaml`: publishes to PyPI on a GitHub release, by trusted
  publishing. PyPI pins that filename and the `pypi` environment; renaming
  either breaks releases.

Use `UV_LOCKED`, not `UV_FROZEN`: uv rejects `UV_FROZEN` alongside `--locked`,
and `--locked` also catches a lock out of step with `pyproject.toml`. Pin
`astral-sh/setup-uv` to a full tag; it publishes no floating major tags.
Prefer a pytest check to a CI check — pytest runs everywhere.

`default_install_hook_types` in `.pre-commit-config.yaml` is load-bearing:
without it `pre-commit install` skips the commit-msg stage and commitizen
validates nothing. New cspell words go in `cspell.config.yaml`. Locally, `act`
needs `--artifact-server-path` for `build.yaml`'s `upload-artifact` step.

Releasing: `cz bump`, push the commit and tag, then `gh release create vX.Y.Z`.
The release, not the tag, triggers `package-and-publish.yaml`.

## Related documents

- `CHANGELOG.md` / `changelog/` — brassy fragments. brassy owns the release
  notes; commitizen owns only the version and the tag.
