# AGENTS.md

## Project

gwarchive: a CLI for organizing files under the GWArchive naming standard, with
an rclone-backed remote sync layer. `src/gwarchive/`, 12 modules plus 9 under
`commands/`, tests in `tests/`, packaged with hatchling and managed with uv.

It used to be one 3,240-line `g.py`. It is not any more, and this document no
longer needs to tell you to grep for an index — the layout below is the index.

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
uv run pytest              # 295 tests
uv run mypy
uv run ruff check . && uv run ruff format --check .
uv run vulture && uv run typos
```

That five-command block is the gate. It ran green at every commit in the
history below it, and it should keep doing so.

## The two rules

Both are enforced by `tests/test_structure.py`, and both were written because
the obvious version of the check did not work. Read the docstrings there before
changing either.

**1. Imports point downward.**

| Layer | Modules |
|---|---|
| 0 | `naming`, `clock`, `__init__` |
| 1 | `output` |
| 2 | `paths`, `tombstone`, `external` |
| 3 | `destination`, `tarball` |
| 4 | `sync`, `options` |
| 5 | `commands/*` |
| 6 | `__main__` |

A module may import its own layer or below, never above. `output` is layer 1
because everything calls `die()` on it; if it ever reached upward the whole
graph would cycle. The guard parses imports with `ast` rather than grepping —
a substring version failed on `naming.py`, which contains
`TOMBSTONE_NAME = ".gwarchive-offload.json"` and imports nothing at all.

**2. Patched callables are imported as modules, never by name.**

Eight functions are monkeypatched by the suite:

| Module | Patched |
|---|---|
| `clock` | `today`, `now_stamp`, `archive_stamp` |
| `external` | `run_rclone`, `run_zstd`, `run_pokeget` |
| `tarball` | `pick_codec`, `create_archive` |

Write `tarball.pick_codec()`, not `from gwarchive.tarball import pick_codec`. A
from-import binds the real function at import time and the double then has
nothing to reach. Everything *else* in those modules — constants, and helpers
nobody patches — is fine to from-import; banning the whole module would mean
~30 dotted calls that protect nothing.

That narrowness is only safe because `test_the_patched_set_is_complete` reads
the suite and fails if anything is patched that the table above does not name.
**Add a new test double, add it there.**

Calls *within* a seam module need no change: `rclone_or_die` resolves
`run_rclone` through its own module globals at call time, so the double is
picked up regardless of the caller.

Why this matters more than it looks: on CI there is no zstd, so a bypassed
`pick_codec` patch returns `"tar.gz"` anyway and all 23 `gzip_codec` tests pass
with their seam dead. The failure is invisible exactly where you would look.

## Conventions

- 4-space indent, `line-length = 110`, full annotations including `-> None`.
  `disallow_untyped_defs` covers `src/` and `tests/`, so monkeypatch stand-ins
  must be annotated — which is what stops them drifting from the real signature.
- **Never interpolate user data into a markup string.** Use the output helpers
  or wrap in `Text(...)`. This is the bug class that ate `[draft]` from folder
  names for the whole first version.
- Errors to stderr, nonzero exit. 0 success, 1 runtime failure, 2 invalid input.
- `--json` owns stdout: no spinner, no prompt, no trailing receipt.
- `cd` uses a bare `print()` and a silent `sys.exit(1)`. Its stdout is consumed
  by `cd $(...)`, so a failed lookup must never emit something substitutable.
  `here` keeps the same contract: shell-init's hook writes its stdout into an
  OSC title sequence, which is also why it strips control characters.
- Every mutating command takes `--dry-run`, and dry-run output has the same
  shape as real output — **except** `init`, `create` and `mksub`, which
  allocate by scanning and would be predicting rather than reserving.
- A dry run must not predict success the real run refuses. `warn_if_taken` is
  the read-only half of `check_no_overwrite`, which deletes under `--force` and
  so cannot run under `--dry-run`.
- `program()` is what to TYPE; `launcher()` is what to EXECUTE. Under the zipapp
  they differ, because there is no `gwarchive` on PATH — that is the premise of
  a self-contained `.pyz`. Hints use `program()`; the emitted shell functions
  use `launcher()`.

## Things that look wrong and are not

- **`push` and `offload` are ~74% identical and stay that way.** Merging puts
  the local-delete loop inside a function `push` also calls, gated on a flag,
  and a flag can be wrong. Today `push` *cannot* delete local files because its
  source contains no delete. One file is what lets you diff them by eye.
  (`AGENTS.md` used to say "~33 shared lines". Re-measured with difflib: 86.)
- **`pull` and `restore` share one body, for the opposite reason.** They drifted
  apart once and three of four differences were bugs.
- **`clock.py` is 12 lines and stays its own module.** It is a seam; folding it
  into `naming` would make a module everything from-imports into a seam and
  spread rule 2 across the package.
- **`.ruff.toml` is standalone, not in `pyproject.toml`.** Its 22 lines of
  measured rationale read as a design document when they are the whole file. A
  `[tool.ruff]` table in pyproject would be ignored *silently* while that file
  exists. Consequence: `target-version` cannot be inferred from
  `requires-python`, so `test_config_floors_agree` keeps the four declarations
  in step.
- **`extend-exclude = ["*.md"]` in `.ruff.toml` is load-bearing.** ruff 0.16.0
  promoted Markdown formatting out of preview. Measured on this repo: remove it
  and `ruff format` rewrites a quoted `Optional[str]` signature inside
  `docs/history/accepted-plan.md` — old code held as evidence.
- **`typos` must never run with `-w`, and never as a pre-commit hook.**
  `.gwarchive-offload.json`, `tool_version`, `offloaded_at` are on-disk wire
  format, and `Alph` in `tests/test_browse.py` is a deliberate fixture proving
  `find --exact` rejects a partial match. "Fixing" it to `Alpha` makes two
  assertions test the same thing.
- **`commands/__init__.py`'s import order is the `--help` order**, and carries a
  per-file ignore for `I001`. isort merged and alphabetised those lines once
  already; `test_every_command_is_registered_in_help_order` is what makes a
  regression loud.
- **The version is declared twice**, in `[project].version` and
  `__init__.__version__`. commitizen's pep621 provider needs a static literal,
  and `importlib.metadata` raises `PackageNotFoundError` inside the zipapp.
  `cz bump` writes both; `test_version_matches_package_metadata` checks them.

## Remote sync

`$GWARCHIVE_REMOTE` or `--remote` (repeatable on push/offload). rclone honours a
colon only in the *first* path segment, so `unraid` and `backup/nas:` are
ordinary local directories to it — `push --remote unraid` once copied 48 MB into
`./unraid/` beside the shell's cwd and printed a green "Pushed".
`validate_remote_target` refuses those with exit 2 before any transfer.

One compressed object per folder, written as a **sibling** of the mirror path:

```
--no-compress:  nas:archive/Project/P0001 Alpha/...loose files...
compressed:     nas:archive/Project/P0001 Alpha.20260909-101530.tar.zst
```

The extension is the codec's own on purpose: lose the tombstone entirely and
`zstd -d < x.tar.zst | tar -tvf -` is still a complete recovery path.

`offload` writes its tombstone **before** deleting, so an interruption reads as
offloaded-with-leftovers rather than a silently emptied folder.

Extraction vets every member rather than trusting `tarfile`'s `filter="data"`:
that landed in 3.12 and was backported to 3.11.4, while the floor is 3.11.0 — so
the vulnerable range is exactly the range nothing exercises. `tests.yaml` has a
`3.11.0` leg for that reason.

## Testing

`tests/conftest.py` holds the fixtures. The autouse `isolate` fixture repoints
`$GWARCHIVE_BASE` at a tmpdir, so a test that forgets `--path` still cannot
reach a real archive, and it clears every `GWARCHIVE_*` variable by prefix
rather than by a list — the list version missed `GWARCHIVE_POKEMON` and
`GWARCHIVE_REMOTE`, and a developer with the former exported failed the suite.

A test guarding a numbered `docs/history/accepted-plan.md` finding cites it as
`N.M` in its docstring. 17 do. `test_every_cited_finding_exists_in_the_record`
resolves them all, which also pins that file's path.

Assert against `--json` rather than rendered box drawing. `COLUMNS=200` is set
by a fixture and dozens of table assertions depend on it.

## CI

`tests.yaml` (3.11.0/3.11/3.12/3.13, `uv sync --locked`, plus a weekly
unpinned canary), `static.yaml` (ruff / ruff-format / mypy / vulture / typos as
separate jobs), `smoke.yaml` (the install, not the behaviour), `build.yaml`
(wheel, sdist, and the `g.pyz`).

`UV_LOCKED`, not `UV_FROZEN`: uv refuses `--locked` and `UV_FROZEN` together
with an argument-parsing error, and `--locked` is the stronger guarantee — it
also fails when `uv.lock` no longer matches `pyproject.toml`.

**There is still no git remote**, so none of this has ever run on GitHub. `act`
is the only verification available, and it cannot run `build.yaml`'s
`upload-artifact` step without `--artifact-server-path`. Prefer putting a check
in pytest over putting it in CI: pytest is the thing that actually runs.

Two hooks are deliberately absent from `.pre-commit-config.yaml` and the reasons
are in that file. `default_install_hook_types` is load-bearing — without it a
plain `pre-commit install` skips the commit-msg hook entirely and commitizen
never validates anything.

## Related documents

- `docs/history/accepted-plan.md` — the annotated record this implementation
  follows, cited by finding number from test docstrings. **Don't edit it**;
  four tools are configured not to touch it for the same reason.
- `CHANGELOG.md` / `changelog/` — brassy fragments. brassy owns the release
  notes; commitizen owns the version and the tag and nothing else.
