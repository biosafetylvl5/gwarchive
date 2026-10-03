# PLAN.md

Suggested improvements to `g.py`, organized around **what the user sees and
what the user can figure out**. Nothing here is implemented yet.

Everything below is a change *inside* `g.py` except where marked, per the
single-file constraint in `AGENTS.md`.

The findings are from actually running the tool against scratch archives, not
from reading alone. Where I quote output, that's real captured output.

**Guiding principle for this pass:** the archive is the user's filesystem, and
the CLI is the only window onto it. Every place the window distorts, hides, or
lies about what's on disk is a higher-priority defect than any internal
tidiness issue.

---

## Phase 1 — The window is lying (fix first)

These affect *every* invocation and one of them silently corrupts what the user
is shown. Small changes, disproportionate payoff.

### 1.1 Rich markup injection: folder names are being eaten

Every message and table cell passes user-controlled text through Rich's markup
parser. A folder whose name contains square brackets — `[draft]`, `[v2]`,
`[WIP]`, all plausible in a real archive — has that segment interpreted as a
style tag and **silently deleted from the display**.

Observed. On disk:

```
P0001 [draft] Chapter [2]
```

What `list` shows:

```
│ 0001   │  Chapter [2] │ 2026-09-04 │
```

The `[draft]` is gone. `find` shows the same folder as
`Project/P0001  Chapter [2]`. The user is looking at a name that does not
exist on their disk, with no indication anything was dropped. `[2]` survives
only because it isn't a valid style name — so the failure is inconsistent,
which makes it harder to notice than if it broke everything.

This is the most serious UX bug in the file: a tool whose entire job is showing
you your filenames is showing you the wrong filenames.

Fix: escape all interpolated user data.

```python
from rich.markup import escape
...
table.add_row(number, escape(name), modified)
console.print(f"Created: [green]{escape(str(new_folder_path))}[/green]")
```

Every `console.print()` that interpolates a path or name needs this —
`g.py:43`, `g.py:91`, `g.py:98`, `g.py:106`, `g.py:156`, `g.py:195`,
`g.py:216`, `g.py:279`, `g.py:283`, `g.py:304`, `g.py:329`, `g.py:334`,
`g.py:340`, `g.py:356`, `g.py:377`, `g.py:426`, `g.py:437`, `g.py:633`.
Cleanest approach is small wrappers (`ok()`, `warn()`, `fail()`, `note()`) that
escape their arguments, so it's applied by construction rather than by
remembering. That also gives 1.3 and 1.4 a natural place to live.

Add a regression test with a bracketed name — this class of bug will come back
otherwise.

### 1.2 Paths wrap mid-token and can't be copied

Rich wraps at console width, and every path message is long. Observed:

```
Moved: 
/var/folders/_m/sstx95yx3lsd_lq56v7y1hzr0000gn/T/tmp.OPM1ZSS3a8/Project/P0001 
Alpha to 
/var/folders/_m/sstx95yx3lsd_lq56v7y1hzr0000gn/T/tmp.OPM1ZSS3a8/Old/2026-09-04-P
0001-Alpha
```

`2026-09-04-P0001-Alpha` has been broken across a line boundary. You cannot
double-click it, you cannot copy it, and at a glance you cannot tell whether
the trailing `Alpha` on line 3 is part of the source path or a separate word.
Since folder names contain spaces by design, the wrapping is genuinely
ambiguous, not just ugly.

Fix, in two parts:

1. `soft_wrap=True` on any print that contains a path, so Rich stops hard-wrapping.
2. Restructure the move/copy messages so the two paths are on their own lines
   and visually parallel:

```
Moved
  from  Project/P0001 Alpha
  to    Old/2026-09-04-P0001-Alpha
```

Also: display paths **relative to the archive root** wherever the archive root
is implied. `find` already does this (`g.py:436`) and reads far better for it.
`mv`, `cp`, `create`, and `mksub` all print absolute paths, which are mostly
redundant prefix. Keep absolute output only in `cd`, where it's the contract.

### 1.3 One action, two success messages, six lines

`create` prints from `ensure_directory` (`g.py:43`) *and* from `create` itself
(`g.py:156`). Measured: **6 lines of output for one folder creation.**

```
Created directory: 
/var/folders/.../Project/P0001 Alpha
Created: 
/var/folders/.../Project/P0001 Alpha
```

The same duplication hits `mksub` (`g.py:195`). And `ensure_directory`'s
chatter leaks into unrelated commands — `mv` into an uninitialized `Old/`
prints a "Created directory" line about a directory the user never asked about:

```
Created directory: 
/var/folders/.../Old
Moved: 
...
```

Fix: `ensure_directory()` should be silent — it's a helper, not a command. Give
it a `quiet: bool = True` default, and let the *commands* decide what to
report. `init` is the one place the per-directory listing is genuinely useful,
so pass `quiet=False` there.

Target: one line per action.

```
Created  Project/P0001 Alpha
```

### 1.4 Errors go to stdout

Verified: every error message lands on stdout and stderr is empty.

```
--stdout:
Source not found: P9
--stderr:
(empty)
```

This breaks two things at once. Piping `list`/`find` output anywhere mixes
diagnostics into the data, and `cd $(g.py cd P1)` can capture an error string
and feed it to `cd`. (`cd` itself is careful to stay silent — `g.py:672`,
`g.py:678` — but `find_folder_by_prefix` prints on the way in at `g.py:91` and
`g.py:106`, so the care is undone one frame down the stack.)

Fix:

```python
err_console = Console(stderr=True)
```

Route all `[red]` and `[yellow]` diagnostic output through it. Data — tables,
`cd`'s path — stays on stdout.

### 1.5 Tracebacks reach the user

`cp` onto an existing directory produces a raw Python traceback:

```
FileExistsError: [Errno 17] File exists: '/var/folders/.../Project/P0002 Gamma'
```

Any `PermissionError`, `OSError`, or cross-device `shutil.move` failure does
the same. Fix: wrap the filesystem calls in `mv`/`cp` and re-raise as a clean
message plus `typer.Exit(1)`. Set `pretty_exceptions_show_locals=False` on the
Typer app so genuine crashes don't dump the user's paths and variables either.

---

## Phase 2 — The tool is hard to learn

Everything here is about a user being able to figure out what's possible
without reading the source.

### 2.1 `mv` has an undocumented mini-language

`mv`'s help says exactly this:

```
│ *    destination      TEXT  Destination path [required]  │
```

The actual accepted forms, reading `g.py:225-267`, are **four different
things** dispatched by regex:

| You type | What happens |
|---|---|
| `Old` (a category *name*) | move into that category, renumber, date-stamp if `Old` |
| `O` (a category *letter*) | same, via a recursive self-call (`g.py:247`) |
| `P0001` (a prefix) | move *inside* that folder |
| `Some New Name` | rename in place, preserving the prefix |

None of this is discoverable. The fourth form in particular — that `mv` is
secretly the rename command — is something no user will find without reading
the code, and it's the most common operation in an archive tool.

Fix, in order of value:

1. **Add a `rename` command.** Give the most common operation its own verb.
   `g.py rename P0001 "New Title"` — obvious, greppable in `--help`, and it
   stops overloading `mv`'s destination slot.
2. **Document the DSL in `mv`'s docstring**, since Typer renders docstrings
   into `--help`. A four-line table costs nothing and removes the mystery.
3. Make the letter-shorthand branch a normalization step rather than a
   positional recursive call (`g.py:247` relies on argument order lining up).

### 2.2 Category arguments are untyped, case-sensitive, and inconsistent

`category` is a bare `TEXT` argument everywhere — no `Enum`, no `Choice`. Three
consequences:

- `--help` shows `TEXT`, with the valid values only mentioned in prose.
- Shell completion can't offer them.
- Validation is hand-rolled per command, and it's inconsistent.

Measured inconsistency: `create p X` works (it upper-cases at `g.py:132`);
`list p` fails (`g.py:350` doesn't). Worse, `find` and `stats` use

```python
[CATEGORIES[category]] if category in CATEGORIES else CATEGORIES.values()
```

(`g.py:392`, `g.py:471`) — so a typo'd or lower-case category **silently
widens the search to the entire archive** and exits 0. `stats --category Q`
returns a full-archive report with no warning. Silent wrong answers are worse
than errors.

Fix: a `str`-backed `Enum` for the category, so Typer validates, documents, and
completes it for free:

```python
class Category(str, Enum):
    P = "P"
    R = "R"
    M = "M"
    A = "A"
    O = "O"
```

If case-insensitivity matters more than the free help text, use a shared
`resolve_category()` helper instead — but either way, `find`/`stats` must reach
the search-all path *only* via `category is None`, never via an invalid value.

### 2.3 `cd`'s help documents a program that doesn't exist

`cd --help` currently tells the user:

```
 Usage in bash/zsh: cd $(gwarchive cd P1)
 gcd() {
     cd "$(gwarchive cd $1)"
 }
```

There is no `gwarchive` binary and no entry point that installs one. Copy this
verbatim, as instructed, and you get `command not found`.

Fix: add a `shell-init` command that emits a working shell function with the
real interpreter and script path baked in, so the instruction becomes:

```sh
eval "$(python3 g.py shell-init)"
```

Then rewrite `cd`'s docstring to point at that. Also fix the argument
declaration while you're there — `prefix` is `typer.Argument(...)` (required)
but defaulted to `None` (`g.py:648`), while the body tests `if not prefix` and
prints the base path (`g.py:665`). Contradictory; make it
`typer.Argument(None, ...)` so the bare-`cd` behavior is honestly declared.

### 2.4 `list` shows a number you can't use

The `Number` column shows `0001` (`g.py:360`, `g.py:377`). Every other command
takes `P0001`. So the output of `list` doesn't feed the input of `cd`, `mksub`,
or `mv` without the user mentally re-attaching the category letter.

Fix: show the full prefix. Rename the column `ID`, print `P0001`. The category
letter is constant per-table, so it costs 1 character and makes every row
directly copy-pasteable into the next command. Same for `find`, which should
surface the prefix as its own column rather than burying it in the path string.

### 2.5 No `--version`

`g.py --version` exits 2 with `No such option: --version`. Add a `__version__`
and a `--version` callback. Cheap, and it's the first thing anyone types when
reporting a problem.

---

## Phase 3 — Feedback: empty states, results, progress

### 3.1 Empty states say nothing

`list` on a freshly-`init`ed archive:

```
      Project Folders       
┏━━━━━━━━┳━━━━━━┳━━━━━━━━━━┓
┃ Number ┃ Name ┃ Modified ┃
┡━━━━━━━━╇━━━━━━╇━━━━━━━━━━┩
└────────┴──────┴──────────┘
```

A headers-only table. It's not *wrong*, but it spends five lines to say
"nothing here" and offers no next step. Compare `list` on an *uninitialized*
archive, which says `Category directory not found: /path/Project` and exits 0 —
a message that describes an internal condition rather than telling the user
that their archive isn't set up and `init` is the fix.

Fix: replace empty tables with a one-line message that names the next action.

```
No folders in Project yet — create one with:  g.py create P "Name"
```

```
No archive at ~/gwarchive — run:  g.py init
```

The uninitialized case should probably also be a nonzero exit (see 3.4).

### 3.2 `find` results are duplicative and unranked

Recursive search returns a parent and its child as independent rows:

```
│ Material │ Material/M0001 Notes                │
│ Material │ Material/M0001 Notes/M0001.01 Notes │
```

Both matched "Notes"; the second row adds nothing the first didn't already
imply. There's also no result count, no indication of *why* something matched,
and no ordering beyond directory iteration order.

Fix: add a result count line, sort deterministically (category, then prefix),
and either collapse nested matches under their parent or add `--depth` so the
user controls it. Highlighting the matched substring in each row costs one
`re.sub` and makes scanning far faster.

Also worth deciding: `find` skips non-directories entirely (`g.py:401`,
`g.py:415`). For a tool that manages files, `--files` is a reasonable ask.

### 3.3 `stats` is slow, silent, and formats sizes badly

Three separate problems:

- **Sizes are always MB** (`g.py:516`, `g.py:520`). Measured: a 3 GB archive
  reads `3072.00 MB`, and a small text file reads `0.00 MB` — indistinguishable
  from empty. Fix: scale the unit (B/KB/MB/GB/TB) per row, or use
  `rich.filesize.decimal`.
- **No progress.** `stats` walks `**/*` and `stat()`s every file in the archive
  (`g.py:481-499`) with zero output until it finishes. On a real archive this
  looks like a hang. Fix: `rich.progress`, or default to a top-level count and
  put the full walk behind `--deep`.
- **`--detail` is thin.** It shows the 5 most recently modified top-level
  folders (`g.py:541`), hardcoded, and the implementation is still marked
  `# Add more detailed statistics here` (`g.py:526`). Either make the count a
  flag (`--recent N`) and add something genuinely useful — largest folders,
  category balance, age distribution — or drop the flag.

### 3.4 Exit codes are wrong for scripting

Measured:

| Command | Exit |
|---|---|
| `find` with no matches | 0 |
| `list` on an uninitialized archive | 0 |
| `stats --category Q` (invalid) | 0 |
| `verify` with 3 issues found | **0** |

The last one is the important one. `verify` reports problems and then tells the
shell everything is fine, so it can't gate anything — not a pre-commit hook,
not a cron job, not `verify && backup`.

Fix: `verify` exits 1 when issues remain unfixed. `find` exits 1 on no matches
(grep convention). Invalid input exits 2. Document the codes in the module
docstring.

### 3.5 `verify` output doesn't scale

```
Found 3 issues:
- Unauthorized directory in root: RandomDir
- Unauthorized file in root: stray.txt
- Invalid Project folder name: not-a-prefix
```

A flat bullet list, all issues equal weight. At 3 issues it's fine; at 300 it's
unusable. And "Unauthorized" is the wrong word for "a folder I didn't
recognize" — it sounds like a security finding and primes the user to accept
the destructive `--fix` behavior described in 4.1.

Fix: group by category, add severity (error vs. notice), show a summary count
per group, and soften the wording — `Unrecognized directory in root`. If
`--fix` can address an issue, say so per-issue rather than making the user
guess what the flag will touch.

Note also that `verify` never checks subfolder naming — it validates top-level
names per category (`g.py:608-624`) but ignores the `P0001.01` convention
entirely, so it passes archives that violate a convention `mksub` enforces.

---

## Phase 4 — Safety is a UX problem

Confirmed by inspection: **there are no confirmation prompts anywhere in the
file, and no `--force`, `--yes`, `--quiet`, or `--json` flags.** Every mutation
is immediate and irreversible.

### 4.1 `verify --fix` is the most dangerous path in the tool

`g.py:575-612` moves *anything* it doesn't recognize at the archive root into
`BACKUP/` — files included, with `allowed_files` hardcoded to
`{'.gitignore', 'README.md'}`. Point it at a directory that isn't a GWArchive
and it will relocate that directory's contents. The flag is described only as
"Attempt to fix issues", which does not convey that.

Fix, in order:

1. Split the flag. `--fix` creates missing category directories (safe,
   idempotent). A separate `--quarantine` opts into the moving.
2. Add `--dry-run`, matching every other mutating command. Its absence here, of
   all places, is the sharpest edge in the tool.
3. `typer.confirm()` before the first move unless `--yes`, showing the full
   list of what's about to be relocated.
4. Timestamp the quarantine directory (`BACKUP/2026-09-04/`). Repeated runs
   currently collide, and `shutil.move` onto an existing directory nests
   instead of failing — a quiet data-shuffling bug.

### 4.2 No overwrite protection on `mv`/`cp`

Neither checks whether the destination exists. `shutil.move` overwrites files
silently; `shutil.copytree` throws the traceback from 1.5; `os.symlink`
(`g.py:333`) throws on an existing target. Fix: check first, error cleanly,
offer `--force`.

### 4.3 `--dry-run` isn't available where it matters most

`mv` and `cp` have it. `verify --fix` — the destructive one — doesn't. Make
`--dry-run` a universal convention on anything that writes, and have dry-run
output be *byte-identical in shape* to real output (prefixed `Would move` vs
`Moved`, as now) so the user can trust the preview.

### 4.4 Ambiguous prefixes resolve silently

`find_folder_by_normalized_prefix` returns the first `iterdir()` match
(`g.py:73-75`). Two folders sharing a number — recoverable from a bad rename or
a sync conflict — means `cd P2` picks one arbitrarily and says nothing. My
runs were consistent, but `iterdir()` order isn't guaranteed, so this can be
stable until suddenly it isn't. Fix: detect multiple matches, list them, exit
nonzero. In `cd`, stay silent per the substitution contract but still fail.

---

## Phase 5 — Scriptability

### 5.1 `--json` on `list`, `find`, `stats`

Rich tables are for humans and unpipeable for anything else. There's currently
no way to get structured data out of this tool. `--json` makes it composable
with `jq`, and incidentally makes every table command far easier to assert on
in tests than screen-scraping box-drawing characters.

### 5.2 Terminal environment handling

Verified behavior: ANSI color *is* correctly stripped when piped (Rich handles
this), and `NO_COLOR=1` works for color. But box-drawing characters survive
both piping and `TERM=dumb`:

```
        Project Folders        
┏━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┓
```

Fix: use `box.ASCII` (or plain rows) when not a TTY or when `TERM=dumb`. If
5.1 lands, `--json` covers most of this need, but plain-text fallback is still
right for `TERM=dumb` and for redirecting a report to a file.

### 5.3 `--quiet` / `--verbose`

With 1.3 fixed, output is one line per action. Add `--quiet` to suppress
success output entirely (errors still to stderr, exit code carries the result)
for scripted use.

---

## Phase 6 — Correctness bugs behind the UX

These are real bugs. Each one presents to the user as the tool doing something
other than what its `--help` promises, which is why they're in a UX plan.

### 6.1 `oldify --date` is a dead flag

`oldify` validates `--date` (`g.py:446-455`) and then calls `mv` (`g.py:460`),
which unconditionally stamps `datetime.now()` (`g.py:234`). Confirmed by
running it: `oldify P0002 --date 2020-01-01` produced
`Old/2026-09-04-P0002-Beta`.

The tool accepts your input, validates it, tells you it succeeded, and ignores
you. Worst of both worlds — a visible flag that silently does nothing.

Fix: thread a `date_override` through to the `Old`-naming branch. Keep it off
`mv`'s public surface.

### 6.2 `cp` is half-implemented

`cp` (`g.py:286`) carries a `# ... (similar destination handling logic as in
mv)` placeholder at `g.py:308` and a "for simplicity in this example" comment.
It handles only category-name destinations and bare paths. `g.py cp foo P0001`
writes a literal `./P0001` directory instead of copying into the P0001 folder.

Two commands documented in near-identical language behave completely
differently, and nothing warns you.

Fix: extract destination resolution out of `mv` into a shared function and call
it from both.

```python
def resolve_destination(source_path: Path, destination: str, base_path: Path,
                        rename: Optional[str], date_override: Optional[str]) -> Path:
    ...
```

Highest-value refactor in the file: fixes `cp`, removes the duplicated rename
block (`g.py:270-275` and `g.py:319-323` are near-identical), and gives 6.1 and
2.1's normalization a home.

### 6.3 `--exact` isn't exact

Both branches at `g.py:405` and `g.py:418` use `in`. `--exact` is just the
case-sensitive substring search, so the flag's name promises equality matching
that the code never does. Make it `pattern == item.name`, or rename it
`--case-sensitive`. Either is defensible; the current state is not.

### 6.4 `list` shadows the builtin

`g.py:343`. Rename the function `list_folders`, keep the CLI name via
`@app.command("list")`.

### 6.5 Number allocation

`create` (`g.py:141`) and `mksub` (`g.py:180`) take max+1 over siblings.
Consequences worth deciding deliberately:

- `create` doesn't scan `Old/` for retired numbers, so a number sent to `Old/`
  **can be reissued** if it was the highest. That breaks prefix permanence —
  two different folders end up having been `P0007`. Scan `Old/` too.
- `mksub` caps at 99 subfolders, `create` at 9999; both overflow silently into
  malformed names. Error instead.

Recommendation: treat a prefix as a permanent identifier. It's currently that
way by accident, not by decision. Whichever you pick, record it in `AGENTS.md`.

---

## Testing

Still the blocker on all of the above: there's no way to know a change didn't
break something.

`pytest` + `typer.testing.CliRunner` + `tmp_path` covers this project almost
entirely, since every command takes `--path`. **This is new scaffolding, so it
needs your sign-off** per the single-file note in `AGENTS.md`.

```
test_g.py          # single file, mirroring g.py
```

UX-focused assertions worth having, most of which fail today:

- A folder named `P0001 [draft] Notes` renders with `[draft]` intact (1.1).
- Errors appear on stderr, not stdout (1.4).
- `create` emits exactly one success line (1.3).
- `verify` exits nonzero when it reports issues (3.4).
- `oldify --date 2020-01-01` produces a `2020-01-01-` prefix (6.1).
- All four category-taking commands agree on case handling (2.2).
- `stats --category Q` errors instead of silently reporting everything (2.2).
- `--dry-run` leaves the filesystem byte-identical.
- `normalize_prefix`: `P1`/`P01`/`P001` → `P0001`; `X1`, `P12345`, `""` → `None`.
  Pure function, trivial, currently untested.

If 5.1 lands first, assert against `--json` rather than rendered tables — much
less brittle.

---

## Suggested order

1. **Phase 1 entirely.** It's the smallest diff with the largest visible
   effect, and 1.1 is a correctness issue about displayed data, not polish.
   Start with 1.1 and 1.3.
2. **Test skeleton** — `test_g.py` with the markup-escaping regression and one
   round-trip. Needs your OK on the new file.
3. **6.2** (extract `resolve_destination`), then **6.1** and **2.1** on top of it.
4. **2.2, 2.4, 2.5, 6.3, 6.4** — independent, batch them.
5. **Phase 4**, starting with 4.1. Don't do this without tests.
6. **Phase 3**, then **Phase 5**, then **6.5** once you've settled the
   prefix-permanence question.

If you'd rather not add a test file, invert 1 and 2 and lean on the `mktemp -d`
recipe in `AGENTS.md` — but Phase 4 is all destructive paths, and it will be
uncomfortable without tests.

---

## Two decisions I can't make for you

- **Is a prefix a permanent identifier?** (6.5) The plan assumes yes. The code
  currently gets that outcome by accident and breaks it in one case.
- **Does `test_g.py` violate "it is just the g file"?** `AGENTS.md` records the
  single-file layout as intentional. I've flagged the test file rather than
  assuming.
