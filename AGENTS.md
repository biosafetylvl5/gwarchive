# AGENTS.md

## Project

GWArchive: a single-file Python CLI for organizing files under the GWArchive
naming standard. The tool itself is `g.py` (~3,280 lines). Alongside it:
`test_g.py`, four GitHub workflows under `.github/workflows/`, and tool config
in `.ruff.toml` / `mypy.ini` / `.pre-commit-config.yaml` / `.actrc`.

**This document cites symbols, not line numbers.** It used to carry both. Over
one large refactor every bare symbol name still resolved, while 13 of 17 line
numbers had rotted -- several of them onto plausible unrelated code, which
reads as correct rather than as a miss. Grep for the name instead; with no
package layout, that *is* the index:

```sh
grep -n '^# ---' g.py                        # the layer banners
grep -nE '^(def |class |[A-Z_]+ =)' g.py     # every top-level name
```

That list is the whole repo, and it is meant to stay that way. "It is just the
g file" has been relaxed to admit a test file and CI — deliberately, in
`accepted-plan.md` — but **not** a package layout. There is no
`pyproject.toml` on purpose; workflows install with `pip install typer rich
pytest` and both tools read their standalone config files natively. Ask before
adding anything else.

## Running

```sh
python3 g.py --help
python3 g.py list P
```

`g.py` is executable with a `#!/usr/bin/env python3` shebang, so `./g.py`
works too. Dependencies (`typer`, `rich`) are importable in the ambient Python
3.12 environment; there is no lockfile. Don't add a dependency without asking.

## Installing (wget-and-run)

The whole tool is one file, so it installs by fetching it:

```sh
wget <url-to>/g.py && chmod +x g.py
uv run g.py --help      # uv/pipx read the PEP 723 metadata block and self-bootstrap
python3 g.py --help     # bare Python: prompts to `pip install typer rich`, then re-execs
```

Three things make this work, and none of them add a file:

- A **PEP 723 inline script metadata** block sits under the shebang (`# ///
  script` ... `# ///`). It is a comment to plain Python and a dependency
  manifest to `uv run`/`pipx run`. `test_pep723_block_declares_the_dependencies`
  guards its contents; keep `requires-python`, `typer` and `rich` in it.
- The `typer`/`rich` imports are wrapped in a **`try/except ModuleNotFoundError`
  bootstrap** (stdlib only, since rich is exactly what's missing). On a TTY it
  offers to `pip install` the two packages and, on success, `os.execv`s so the
  original command runs against the fresh install. Non-interactive, it prints
  the `pip`/`uv` hints to stderr and exits 1 — never a traceback, never a word
  on stdout (so `g.py cd` substitution stays safe). An internal
  `_GWARCHIVE_REEXEC` sentinel stops infinite re-exec loops if pip succeeds but
  packages remain unimportable.
  `test_bare_python_gets_a_pip_hint_not_a_traceback` and
  `test_bare_python_reexec_guard_prevents_infinite_loop` guard this.
- `smoke.yaml` has a `uv run --no-project g.py --help` leg that deliberately
  does *not* pre-install the deps, proving the PEP 723 path stays honest.

For shell navigation:

```sh
eval "$(python3 g.py shell-init)"
gcd P1      # ggd is an alias for the same function
```

## The one rule everything follows

**A prefix is a permanent identifier.** `P0001` is allocated once, never
reissued, and travels with the folder across category moves — a folder created
in `Project/` keeps its `P` prefix after moving into `Archive/`. A *copy* is a
new thing and gets a fresh number in the category it lands in.

Three consequences that will surprise you if you don't know them:

- The category letter does **not** tell you which directory a folder is in.
  `find_folders_by_prefix` scans every category; never derive a
  directory from the letter.
- `allocate_number` scans all categories including `Old/`, so a
  retired number is never handed out again.
- Two folders sharing a prefix is an invariant violation, not a state to
  handle. `check_prefix_available` refuses moves that would create
  one, and `collect_uniqueness_issues` reports any that exist.

This is recorded here because it's a decision, not a derivation — see the
Decisions section of `accepted-plan.md`.

## Architecture

Typer app + two Rich consoles at module top, then eleven banner-marked
sections. The load-bearing ones:

- **Output**: `ok`/`note`/`caption`/`detail` on stdout,
  `warn`/`error`/`hint` on stderr, plus `die`, `panel`, `new_table`,
  `confirm_destructive`, `spinner`, `emit_json`, and the `plural`/`pl` counters
  that decide how the tool says "1 error" and "2 errors".
  Everything renders through `rich.text.Text`, so user data is *never* parsed
  as markup — a folder named `P0001 [draft] Notes` displays intact. Style comes
  from `style=`, never inline tags. `die()` returns the exception so callers
  write `raise die(...)`.

  Three things there that only look like plumbing. `symbol()` owns the ASCII
  fallback for every glyph, and `_decorate()` is the only place a symbol is put
  in front of a line, so the kind of a line is legible before the line is read.
  `spinner()` is the single gate on transient UI — a real terminal, never under
  `--quiet` or `--json` — which is what keeps piped output byte-identical.
  And `new_table()` / `panel()` take a `Text` title as well as a `str`, because
  Rich markup-parses a bare `str`: `find "[draft]"` used to lose its brackets,
  and a pattern containing a close tag raised `MarkupError` *after* the search
  had already succeeded.

  `STYLES` has one accent colour (magenta) on purpose. It was in the palette
  twice already, hardcoded, before it had a name: the thing you searched for,
  and the number you came to read.
- **Paths and prefixes**: `get_base_path()` reads
  `$GWARCHIVE_BASE`, falling back to `~/gwarchive`. `normalize_prefix()` expands
  `P1`/`p01`/`P001` to `P0001`. `folder_prefix()` reads the identifier out of
  either a live name or a date-stamped `Old/` name. `resolve_prefix()` is the
  single resolution entry point — it treats ambiguity as a failure rather than
  returning the first match.
- **Destination resolution**: `resolve_destination()` is shared
  by `mv`, `cp`, `rename` and `oldify`. Put destination logic here, not in a
  command. The guards — `check_not_nested`, `check_no_overwrite`,
  `check_prefix_available` — run before any filesystem call.
- **Shared option types**: the `Annotated` aliases every command signature is
  built from — `BasePath`, `DryRun`, `AsJsonTable`/`AsJsonReport`/`AsJsonSync`,
  `Selector`, `CategoryArg`, `CategoryOpt`, `Force`, `Yes`, `PushRemotes`,
  `PullRemote`, `ArchiveVersion`, `Keep`, `NoCompressPush`/`NoCompressPull`.
  `--path` alone had been written out sixteen times, and three real defects had
  grown in that drift: `verify` was missing `-y`, and `find`/`stats` had lost
  the `[P|R|M|A|O]` metavar their siblings show. An option whose help text
  genuinely differs per command stays inline — a shared alias would flatten it.

  `BasePath` resolves itself via `default_factory=get_base_path`, so no command
  body opens by resolving `--path`. `default_factory` forbids a `=` default,
  which is why `base_path` is keyword-only (the bare `*`) in every signature,
  and `show_default=False` is what keeps `[default: (dynamic)]` out of `--help`.
- **Commands** — 18: `init`, `create`, `mksub`, `mv`, `rename`, `cp`, `list`,
  `find`, `oldify`, `push`, `offload`, `pull`, `restore`, `stats`, `verify`,
  `clears`, `cd`, `shell-init`.

  `pull` and `restore` are two thin wrappers over one `fetch_folders` body.
  They had shared a source-resolution ladder byte for byte, comment included,
  and three of the four things only one of them did were bugs in the other.

  `push` and `offload` are deliberately **not** merged. The shared part
  measures ~33 lines out of 310, and merging puts the local-delete loop inside
  a function `push` also calls, gated on a flag. Today `push` cannot delete
  local files because `push`'s source contains no delete; that structural
  impossibility is worth more than 33 lines. What *is* shared lives in
  `carry_remotes`, `transfer_args` and `plural` — the user-facing strings that
  have to agree between the two.

`CATEGORIES` is the letter→directory map, `P`/`R`/`M`/`A`/`O` →
`Project`/`Recurring`/`Material`/`Archive`/`Old`. The four compiled regexes
below it (`FOLDER_RE`, `OLD_RE`, `SUBFOLDER_RE`, `PREFIX_RE`) derive from it,
as do `CATEGORY_LETTERS`, `CATEGORY_METAVAR`, `CATEGORY_NAMES` (the directory
names as a frozenset, for the membership tests) and `CATEGORY_BY_NAME`
(upper-cased, so a category *name* resolves back to its letter in any casing).
Matching names case-sensitively while matching letters case-insensitively is
what made `mv P1 archive` rename the folder in place and report a green
"Moved". Change the dict and everything follows.

`name_parts()` is the single place the group numbering of `FOLDER_RE` and
`OLD_RE` is written down; `folder_prefix()` and `folder_descriptor()` are
one-liners over it.

Naming conventions the code enforces:

- Top level: `P0001 Some Name` — letter, zero-padded 4 digits, space, descriptor.
- Subfolders: `P0001.01 Some Name` — parent prefix, dot, 2 digits.
- `Old/`: `YYYY-MM-DD-P0001-Descriptor` — date first, hyphen separated.

There is no index or database; the filesystem is the source of truth.
`MAX_NUMBER`/`MAX_SUBNUMBER` cap allocation, and overflow is an
error rather than a malformed name.

## Conventions

- 4-space indent, `line-length = 110`, full type annotations including `-> None`
  on commands. `mypy.ini` sets `disallow_untyped_defs`; both files are clean.
- **Never interpolate user data into a markup string.** Use the output helpers,
  or wrap in `Text(...)`. This is the bug class that ate `[draft]` from folder
  names for the whole first version of the tool.
- Errors go to stderr and exit nonzero. Exit codes are documented in the module
  docstring: 0 success, 1 runtime failure, 2 invalid input.
- Category arguments use `callback=validate_category` so casing is
  normalized and a bad value is a Typer error. An invalid category must never
  fall through to "search everything".
- Every mutating command takes `--dry-run`, and dry-run output is the same shape
  as real output (`Would move` vs `Moved`) — **except** `init`, `create` and
  `mksub`. Those three allocate by scanning the tree rather than from a
  counter, so a dry run would report a prediction, not a reservation. If you
  add the flag there, say "would create" and mean it.
- A dry run must not predict success the real run refuses. `mv`/`cp`/`oldify`
  return before `check_no_overwrite` (which *deletes* under `--force`, so it
  cannot run under a dry run), and used to print `Would move` and exit 0 for a
  destination the real command rejects with exit 1. `warn_if_taken` is the
  read-only half.
- Every command that touches the archive takes `base_path: BasePath` as a
  keyword-only parameter and gets a resolved `Path`. Don't re-add a
  `path: Path | None` plus a resolution line. `clears` is the one command with
  no `--path`, because it reads nothing; `shell-init`'s `path` is a *different*
  parameter — it bakes a literal into the emitted snippet rather than resolving
  one — and keeps `Path | None`.
- `--json` is on eight commands, not just the table ones: `list`, `find`,
  `stats`, `verify`, `push`, `offload`, `pull`, `restore`. Assert against it in
  tests rather than against rendered box-drawing or caption wording.
- **`--json` owns stdout.** Nothing presentational may share it: no spinner
  (`spinner()` gates on it), no confirmation panel or prompt
  (`confirm_destructive` writes both to stderr, and passes `err=True`, because
  click prompts on stdout by default), and no trailing receipt — `verify
  --report` suppresses its "Report saved to" note under `--json`.
- `verify` prints its findings and summary to **stdout**: they are the
  command's output the way `list`'s table is, and the exit code carries
  pass/fail. Under `--quiet` the exit code is the whole report.
- `cd` uses bare `print()` and a silent `sys.exit(1)` — its stdout is consumed by
  `cd $(...)`, so a failed lookup must never emit a string that could be
  substituted into a `cd` argument. Keep that path unformatted.

## Remote Sync (rclone)

GWArchive integrates with `rclone` for offsite backup, remote NAS synchronization,
and offloading completed projects to free local disk space while preserving
identifiers and directory structure.

- **Remote configuration**: set `$GWARCHIVE_REMOTE` (e.g. `nas:archive` or
  `proton:Backups/gw`) or pass `--remote`. Repeatable `--remote` flags allow
  backing up to multiple remotes in one command.
- **What counts as a remote**: rclone honours a colon only in the *first* path
  segment, so a value must look like `nas:`, `nas:archive` or a
  `:sftp,host=...:` connection string -- or be an absolute path
  (`/Volumes/Backup`). Anything else, `unraid` and `backup/nas:` alike, is an
  ordinary local directory to rclone: `push --remote unraid` once copied 48 MB
  into `./unraid/` beside the shell's cwd and still printed a green "Pushed".
  `validate_remote_target` refuses those with exit 2 before any
  transfer, in `push`, `offload`, `pull`, `restore`, `$GWARCHIVE_REMOTE`, and on
  a value read back out of a tombstone. `is_remote_target` is the
  same test without the exit; `push`/`offload` use it to drop an entry a laxer
  version recorded rather than carrying it forward forever.
- **Remote layout**: `push`/`offload` write one compressed object per folder,
  as a *sibling* of the mirror path:

  ```
  --no-compress:  nas:archive/Project/P0001 Alpha/...loose files...
  compressed:     nas:archive/Project/P0001 Alpha.20260909-101530.tar.zst
  ```

  `remote_folder_target` still builds the directory path -- it is
  what `remotes` records and what a bare `nas:` resolves against -- and
  `remote_archive_object` turns it into the sibling object name. Sibling, not
  inside the directory: a folder pushed compressed and later re-pushed
  `--no-compress` drops its `archive` block, so `restore` falls back to
  `rclone copy <dir> <local>`, and an object *inside* would be dragged down into
  the live folder as a real file that `compute_folder_stats` then counts.

  The timestamp is `YYYYMMDD-HHMMSS`, not ISO 8601, because a colon in an object
  name is trouble on several backends. Every push writes a new object rather
  than overwriting one, so a push can never damage the copy already up there.
- **Compression**: on by default; `--no-compress` (or `$GWARCHIVE_COMPRESS=0`)
  falls back to the per-file `rclone copy` mirror. `pick_codec` prefers the
  `zstd` binary and falls back to stdlib `tarfile` gzip, and `$GWARCHIVE_CODEC`
  forces either. **The extension carries the codec on purpose** -- lose the
  tombstone entirely and `zstd -d < x.tar.zst | tar -tvf -` is still a complete
  recovery path, which would not be true if the codec lived only in JSON.

  **One rule decides what is counted, what is packed, and what may be
  deleted**, and it is a per-path-*component* test: `_reserved_member` for one
  name, `_reserved_path` for a path. `TOMBSTONE_NAME` and `.gwarchive-*` never
  travel and never extract, in the tar member filter and in `RCLONE_EXCLUDES`
  for the per-file mirror alike.

  It used to exist in five spellings, three of which only looked at the leaf
  name -- and `.gwarchive-cache/junk` has an innocent leaf. So
  `compute_folder_stats` counted bytes that `create_archive` pruned and
  `offload` then deleted, leaving no copy anywhere. `verify_archive` could not
  catch it: expected and found both came from the same pruned walk, so zero
  equalled zero. Anything reserved was never in the archive, which is exactly
  why the delete loop must leave it alone. `create_archive` counts
  members during the walk that writes them rather than re-running
  `compute_folder_stats` -- two walks disagree when a sync daemon touches the
  tree, and `stat` counts a symlink as its target's bytes where tar stores a
  0-byte link member. Symlinks are refused on extraction, matching `rclone
  copy`, which skips them without `-L`.
- **Retention**: `--keep N` (or `$GWARCHIVE_KEEP`, default 1) retains the N
  newest objects per remote; `--keep 0` retains every copy.
  `pull`/`restore --version` reaches an older one by index (`1` is newest) or by
  object name, so retention is readable rather than just storage. Pruning only
  ever deletes names already recorded in `archive.versions` -- it never lists
  the remote and never pattern-matches, so a file somebody else put beside ours
  cannot be caught by it -- and a failed `deletefile` warns and keeps the entry
  for the next push rather than failing a transfer that succeeded.

  **`versions` is newest-first by construction and must not be re-sorted.**
  `record_version` prepends, survivors keep their order, and a failed delete
  goes back on the end -- which is where it already belonged. Sorting by object
  name looks equivalent and is not: `archive_object_name` puts the *folder*
  name first, so a single `rename` between pushes inverted the index. The
  default restore then served the older copy for good, `--version 1` pointed at
  it, and the next prune deleted the second-*newest* object instead of the
  oldest. Reachable at the default `--keep 1` too, once a delete has failed.

  Pruning deduplicates its delete targets. `remotes` is a historical union that
  grows on every rename and every added `--remote`, and entries that differ can
  still resolve to the same sibling object -- so the second `deletefile` failed
  on the file the first had just removed, marking a successful prune as failed
  and keeping the entry for a retry that could never succeed.

  For the same reason `pull`/`restore` resolve to the **last** usable recorded
  remote, not the first. Taking the first meant that after a rename, a restore
  read from the abandoned directory, repopulated the folder with pre-rename
  content, cleared `offloaded_at`, and exited 0.
- **Tombstones (`.gwarchive-offload.json`)**:
  - `push` uploads the folder (backup) and records metadata (`remotes`, `size`,
    `file_count`, `last_pushed_at`). Local files remain intact.
  - `offload` uploads the folder, deletes local files, and writes a tombstone
    with `offloaded_at`. The folder remains in place with its permanent prefix,
    visible in `list` as `[offloaded]`, resolving in `cd` and `mv`, and treated
    as 0 bytes in local `stats`.
  - `pull` brings content back to local without altering tombstone state.
  - `restore` brings content back and clears `offloaded_at`, returning the
    folder to regular live status.
  - A compressed transfer adds one `archive` block, newest version first:

    ```json
    "archive": {"format": "tar.zst", "keep": 2, "versions": [
      {"name": "P0001 Alpha.20260909-101530.tar.zst", "format": "tar.zst",
       "pushed_at": "...", "sha256": "...", "member_count": 1010,
       "uncompressed_size": 8890, "compressed_size": 17036}]}
    ```

    **Absence of that key means the remote is the per-file mirror**, and
    `pull`/`restore` take the plain `rclone copy` path. `archive_meta_of` is the
    only archived-or-not test in the tool -- there is no remote listing
    anywhere, so the tombstone *is* the version index and one operation stays
    one remote round trip. `format` is carried per version as well as on the
    block, so a folder whose codec changed between pushes can still read its
    older objects. Note `compressed_size` can exceed `uncompressed_size` for a
    tree of tiny files: the latter is the sum of member payloads, and 512-byte
    tar headers dominate. The win there is round trips, not bytes.
- **Subprocess boundaries**: one monkeypatchable function per external tool --
  `run_rclone`, `run_zstd`, and `run_pokeget`. Tests patch them and
  never hit the network; most archive tests instead force `pick_codec` to gzip,
  which exercises the real create/verify/extract path with no external binary at
  all. That matters because CI has neither rclone nor a guaranteed zstd.

  `_tar_stream` is the one reader *and* writer for both codecs. Two orderings
  inside it are load-bearing: `tarfile` is closed **inside** the `try`, before
  the pipe is, so its end-of-archive blocks reach zstd before zstd sees EOF
  (invert it and every zstd archive is silently truncated); and the gzip write
  arm stays `w:gz` rather than `w|gz`, because `w:gz` sets `TarFile.name`,
  which is what enables tarfile's own refusal to pack an archive into itself.

## verify

Six collectors, each `(base_path) -> Iterator[Issue]`, composed in `verify`.
`Issue` is a dataclass whose `severity` **defaults to ERROR**, so a collector
yields a finding on one line and only the two notices say otherwise.

A finding carries its own repair target: `target: Path` and `action`
(`"create"` / `"quarantine"`). `--fix` used to recover the directory to create
by string-parsing `issue.message` with `rsplit(": ", 1)[1]`, so rewording a
message silently changed which directory got created, and a message without a
`": "` raised a bare `IndexError`.

`collect_structure_issues` reports the missing base **and** the five missing
categories in one pass. Returning early after the base is what made `verify
--fix` create one directory, resolve its single finding, and exit 0 on an
archive that still had no categories at all -- so `--fix` needed two runs to
converge and said nothing about it.

`read_tombstone_state` distinguishes `absent` / `corrupt` / `not-object` /
`ok`. `read_tombstone` is the two-line wrapper for callers that only want the
dict. The distinction matters beyond `verify`: collapsing all four to `None`
made a *corrupt* tombstone read as *no* tombstone, so the next `push` discarded
the recorded remotes and the whole version index -- and since pruning only ever
deletes recorded names, every object the old list named became unreclaimable.

`--json` and `--report` are the structured views, and both are tested. Keep
`as_dict` hand-written: `dataclasses.asdict` would put a `PosixPath` into
`emit_json` and make `json.dumps` raise.

Two collectors must **not** be rebuilt on `iter_archive_folders`:
`collect_naming_issues` needs the folders that have *no* valid prefix, which is
precisely what that helper filters out, and it needs the category letter;
`collect_root_issues` walks the archive root, not a category.

## Safety

`verify --fix` only creates missing category directories — safe and idempotent.
Relocating unrecognized root entries is a separate `--quarantine` flag that
prompts unless `--yes`, and writes into a dated `BACKUP/<YYYY-MM-DD>/` so
repeated runs never nest. Don't merge those two flags back together, and don't
run `--quarantine` against a directory that isn't a GWArchive — pointed at this
repo it would relocate `.github/`, `test_g.py` and `g.py`.

`offload` is a destructive operation that deletes local folder contents after
remote upload. It prompts for confirmation unless `--yes` is passed, and local
deletion only occurs if `rclone` returns exit code 0. If `rclone` fails or is
interrupted, local files are preserved untouched.

Three things follow from that ordering, and all three are load-bearing:

- **The archive is staged before anything is deleted**, so a compressed
  `offload` briefly needs room for a second copy of the folder on whatever
  volume `$TMPDIR` points at -- precisely the situation `offload` exists to
  relieve. `ensure_scratch_space` checks it *before* the prompt and dies with a
  `TMPDIR=` hint, and `offload_summary` shows the requirement next to the
  "frees" total, so the cost is part of what the user agrees to.
- **`offload` reads the finished archive back** (`verify_archive`) and dies if
  the member count disagrees with what the write walk counted, with local files
  untouched. `push` skips that pass -- it keeps the local copy.
- **`pull`/`restore` re-hash the downloaded object and refuse it on a mismatch
  before touching the folder**, so a corrupt or truncated object leaves an
  offloaded folder a clean tombstone rather than half-overwritten. The remote
  copy is still there to retry, or to fall back from with `--version 2` when
  `--keep` left a spare.

Staging goes in `tempfile.TemporaryDirectory`, never under the archive root:
`collect_root_issues` would flag it, `verify --quarantine` would relocate it,
and on a sync-daemon-backed mount the daemon would start uploading a
multi-gigabyte scratch file.

`--no-compress` has no `verify_archive` pass -- the whole guarantee on that
path is "rclone exited 0" -- so `offload --no-compress` refuses a folder
containing a symlink and passes `--create-empty-src-dirs`. Without those,
`rclone copy` skipped both and exited 0 while the delete loop removed them.

`ensure_scratch_space` sizes the need as the payload bytes *plus* 512 per
member, because tar headers dominate a tree of tiny files: 100,000 empty files
measured as `need=0`, so the guard passed with one byte free and the write then
died on ENOSPC. `pull_archive` checks the same way before staging a download,
using the `compressed_size` already recorded for it.

Extraction vets every member on every Python rather than trusting
`tarfile`'s `filter="data"`: that landed in 3.12 and was backported to 3.11.4,
while the PEP 723 pin admits 3.11.0 -- and CI resolves `'3.11'` to the newest
patch, so the vulnerable range is exactly the range nothing exercises.

## Testing

```sh
python3 -m pytest -v          # 182 tests from 148 test functions
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m mypy g.py test_g.py
```

`ruff`, `mypy` and `pytest` are not in the ambient environment — use a venv
(`python3 -m venv /tmp/gwv && /tmp/gwv/bin/pip install typer rich pytest ruff
mypy`). CI installs them per-run.

`test_g.py` sits next to `g.py` and does `import g` under pytest's default
`prepend` import mode — no `sys.path` surgery, no `conftest.py`. The autouse
`isolate` fixture repoints `$GWARCHIVE_BASE` at a tmpdir, so a test that forgets
`--path` still can't reach the real archive. It also resets the `QUIET`/`VERBOSE`
module globals, which the root callback writes.

A test that guards a numbered `accepted-plan.md` finding names it (`6.5`,
`1.1`, …) — 20 of them do. The rest cannot: the remote-sync and archive work
has no section in that document. Keep the convention where a finding exists,
and don't invent numbers where one doesn't.

Six fixtures carry the setup, so a test asks for what it needs and nothing
else: `archive` (initialized, empty), `folder` (P0001 Alpha with a file),
`sample` (a nested tree plus both reserved metadata shapes — it keeps its
bracketed name deliberately, since that is what guards folder names against
being read as markup), `duplicates`, `stray`, and the tool stand-ins `rclone`,
`rclone_local`, `gzip_codec`, `stamps`, `pokeget`. `rclone()` returns the calls
it recorded and takes `returncode=`/`fail_on=`; `rclone_local` is separate on
purpose — it performs the real file operations, which is what makes an
end-to-end push→restore round trip possible with no rclone and no network.

When exercising the CLI by hand, always pass `--path` (or set
`GWARCHIVE_BASE`) — the default is the user's live `~/gwarchive`:

```sh
tmp=$(mktemp -d)
python3 g.py init --path "$tmp"
python3 g.py create P "Test Project" --path "$tmp"
python3 g.py verify --path "$tmp"
```

## CI

Four workflows: `pytest.yaml` (3.11/3.12/3.13 matrix), `smoke.yaml` (help
renders twice — once plain, once through `uv run --no-project` for the PEP 723
path — plus four scratch-archive round trips), `lint.yaml` (ruff-action),
`mypy.yaml`. All four run on pushes to `main` and on pull requests, and all
four carry `timeout-minutes: 10`: `offload` and `verify --quarantine` prompt,
so a leg that forgets `--yes` would otherwise sit at the six-hour default.
`.pre-commit-config.yaml` runs ruff-check, ruff-format, yamlfmt and actionlint
locally.

`smoke.yaml` asserts through `--json` where it can. One leg used to `grep -q
'tar.gz'` out of a `caption()` line, which made a cosmetic rewording turn CI
red. The one text assertion left is deliberate: `test "$(g.py cd P1 …)" = …`
*is* the unformatted-`cd` contract.

Four things to know before touching them:

- **There is no git remote.** Actions run on nothing until the repo is pushed.
- `.ruff.toml` selects `E,F,W,I,UP,B,SIM,C4` with no complexity cap — ratchet
  rather than adopting a config that fails on commit one. **The `B008` ignore
  is gone**: it existed because `typer.Option(...)` in a default is Typer's
  whole calling convention, and the `Annotated` aliases leave no call-valued
  defaults for it to flag. Verified with `ruff check --extend-select B008`;
  re-check before re-adding a classic-style option.
- `ERA` and `ARG` were **measured and declined**, and the reasons are in
  `.ruff.toml`. `ERA001` reads a commented-out assignment as dead code, and the
  PEP 723 dependency block under the shebang is exactly that shape — a rule
  whose only candidate finding would be the file's own load-bearing metadata.
  `ARG` flags seven things, all convention: the eager `--version` callback
  parameter, pytest fixtures requested for their side effect, and the
  monkeypatch stand-ins that must match `run_rclone`'s real signature.
- A complexity cap is still worth having, but the old note here deferred it
  "once `verify` is split" and `verify` was never the outlier. Measure it
  against the current shape.
- `.ruff.toml` sets `extend-exclude = ["*.md"]`. Ruff's formatter reaches into
  Python blocks inside Markdown, and the plan documents quote the *old* code
  verbatim as evidence — reformatting them would edit the record.
- **Two Markdown hooks are deliberately absent**, and the reasons are recorded
  in `.pre-commit-config.yaml`. `trailing-whitespace`: `accepted-plan.md` quotes
  captured terminal output whose trailing spaces are the evidence for findings
  1.2 and 1.3. `mdformat`: measured against the plan documents, it rewrites
  `---` rules to `______`, pads every table, renumbers ordered lists to all-`1.`
  (the plans cite their own step numbers in prose), and unwraps paragraphs.
  `--number --wrap keep` stops only the renumbering.

### Running the workflows locally with `act`

`act` 0.2.89 is installed at `~/.local/bin/act` (nektos release tarball for
`darwin/arm64`, checksum verified). `~/.local/bin` may or may not be on `PATH`
depending on the shell; export it or invoke the binary by full path.

All four jobs have been run against a live Docker daemon from this directory
and pass — including the pytest matrix on 3.11.16 / 3.12.14 / 3.13.15. Re-run
them after any change; the test count in the line above is the one to check
against, not a number remembered from a previous pass:

```sh
export PATH="$HOME/.local/bin:$PATH"
act -l                                   # list what would run
act -j smoke                             # one job
act -j test                              # the 3-leg matrix
act push -W .github/workflows/pytest.yaml --dryrun
```

Running from this path works: `actions/checkout` under act is a `docker cp`,
not a bind mount, so neither the space in `P0028 GWArchive` nor the
sync-daemon-backed `ProtonDrive` mount causes trouble. (An earlier draft of
this file predicted otherwise; measured, it's fine.)

`.actrc` carries two non-obvious settings, both documented in the file itself:
`--container-architecture linux/amd64` for Apple silicon, and an explicit
`--env PATH` that works around [nektos/act#107] — when a step writes to
`$GITHUB_PATH`, act rebuilds `PATH` from its own hardcoded Debian default and
drops the image's node directory, so every JS action's *post* step dies with
`"node": executable file not found in $PATH` **after** all the real steps have
passed. The node versions in that `PATH` are pinned because `.actrc` cannot
glob; regenerate them if the runner image bumps node:

```sh
docker run --rm --platform linux/amd64 catthehacker/ubuntu:act-latest \
  bash -c 'ls -d /opt/acttoolcache/node/*/x64/bin | paste -sd: -; echo "$PATH"'
```

[nektos/act#107]: https://github.com/nektos/act/issues/107

## Related documents

- `accepted-plan.md` — the plan of record this implementation follows, with
  measured before/after output for each finding. Numbered sections (1.1, 6.5, …)
  are cited from test names and comments. **Don't edit it silently**; it's the
  user's annotated document.
- `PLAN.md` — the earlier draft. Superseded; kept for history.
