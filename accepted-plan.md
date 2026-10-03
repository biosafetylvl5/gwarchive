# PLAN.md

Suggested improvements to `g.py`, organized around **what the user sees and
what the user can figure out**. Nothing here is implemented yet.

Everything below is a change *inside* `g.py` except where marked. Per your
answer at the bottom of this file, `test_g.py` and CI config are now in scope;
`AGENTS.md` should be updated to say so, since it currently records the
single-file layout as intentional.

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
| `Old` (a category *name*) | move into that category, **swap the letter, keep the digits**, date-stamp if `Old` |
| `O` (a category *letter*) | same, via a recursive self-call (`g.py:247`) |
| `P0001` (a prefix) | move *inside* that folder |
| `Some New Name` | rename in place, preserving the prefix |

The first row is worth reading twice: `mv` does **not** allocate a fresh number
in the destination. `g.py:239` builds `f"{dest_category}{number} {descriptor}"`
— same digits, new letter. That collides with whatever already holds those
digits in the destination category. See 6.5; it's the most consequential
finding in this plan.

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

Have it define **both `gcd` and `ggd`** — same body, two names — so either
spelling works. Checked on this machine: both are free. `g` itself is not; it
already resolves to `/run/current-system/sw/bin/g`, so don't emit a bare `g`
wrapper without a collision check.

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

### 3.6 `verify` never checks that prefixes are unique

Every naming rule is applied to one name at a time, so nothing in the file
ever compares two folders. An archive holding two `A0001` directories passes
clean:

```
Archive/A0001 Alpha
Archive/A0001 Existing Archive Item

$ g.py verify
Verification passed! The GWArchive structure is valid.
$ echo $?
0
```

That is measured output, and 6.5 shows `mv` reaching that state in a single
command. So the one check that would catch the tool's worst bug is the one
check that doesn't exist.

Fix: group top-level entries by prefix within each category, report any group
with more than one member as an error. A few lines, and it belongs in the same
pass as the subfolder check above. Given 4.1, this issue must **not** be
autofixable — there is no safe automatic answer to "which of these two is the
real `A0001`".

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
(`g.py:73-75`). Two folders sharing a prefix means `cd A1` picks one
arbitrarily and says nothing.

This is not a hypothetical about sync conflicts — **`mv` produces it in one
command** (6.5). Measured: with `Archive/A0001 Existing Archive Item` already
present, `mv P0001 Archive` created a second `A0001`, and `cd A1` then resolved
to the *pre-existing* folder on all three runs — not the one just moved. The
tool relocates your folder and then navigates you somewhere else, silently. My
runs were consistent, but `iterdir()` order isn't guaranteed, so this is stable
until suddenly it isn't.

Fix: detect multiple matches, list them, exit nonzero. In `cd`, stay silent per
the substitution contract but still fail.

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

### 6.5 Prefixes are not unique, and `mv` is why

You've settled that a prefix is a permanent identifier. Good — but the code
breaks that in two places, and the worse one wasn't in the previous draft.

**The serious one: `mv` into a category collides.** `g.py:239` keeps the
source's digits and swaps only the category letter. Measured end to end:

```
before:  Archive/A0001 Existing Archive Item
         Project/P0001 Alpha

$ g.py mv P0001 Archive
Moved: .../Project/P0001 Alpha to .../Archive/A0001 Alpha

after:   Archive/A0001 Alpha
         Archive/A0001 Existing Archive Item     ← two A0001s
```

Nothing warns. `verify` passes and exits 0 (3.6). `cd A1` picks the wrong one
(4.4). One command, three silent failures stacked.

**The smaller one: retired numbers get reissued.** `create` doesn't scan `Old/`,
so the highest number can be handed out twice. Measured: `oldify P0002` produced
`Old/2026-09-04-P0002-Beta`, and the next `create P Gamma` produced
`Project/P0002 Gamma`.

Implementation note that will otherwise bite you: `create`'s scan pattern is
`rf"{category}(\d{{4}})"` (`g.py:141`), which is anchored at the start of the
name and **will not match** an `Old/` entry, because those are named
`2026-09-04-P0002-Beta`. Verified — the pattern returns `None` against that
string. "Also scan `Old/`" implemented naively is a no-op. It needs the
date-prefixed form, `rf"\d{{4}}-\d{{2}}-\d{{2}}-{category}(\d{{4}})-"`.

**Overflow.** `mksub` caps at 99 subfolders and `create` at 9999; both roll
over silently into malformed names. Error instead.

#### The decision "permanent" doesn't settle

If prefixes are permanent, what should a cross-category move do to the
identifier? Three options:

1. **Keep the full prefix.** `P0001` stays `P0001` inside `Archive/`. Genuinely
   permanent, and no collision is possible. Cost: the letter stops encoding
   current category, and `find_folder_by_prefix` derives the directory from the
   letter (`base_path / CATEGORIES[letter]`, `g.py:66`), so it would never find
   a moved folder. The resolver has to scan all categories instead.
2. **Swap letter, keep digits** — today's behavior. Collides, as above.
3. **Allocate a fresh number in the destination.** No collisions, but the folder
   changes identity, which is what "permanent" was meant to rule out.

**Recommendation: option 1**, with the resolver changed to scan all categories
rather than deriving the directory from the letter. It's the only one under
which "permanent" is actually true. It's a wider change than it looks — it
touches `find_folder_by_prefix`, `find_folder_by_normalized_prefix`, `verify`'s
per-category naming rules, and `mv`'s destination naming — so do it after 6.2
has consolidated destination resolution in one place.

Whichever you pick, record it in `AGENTS.md`, and add the 3.6 uniqueness check
regardless: option 1 makes collisions impossible going forward but does nothing
about archives that already have them.

---

## Testing

Still the blocker on all of the above: there's no way to know a change didn't
break something.

`pytest` + `typer.testing.CliRunner` + `tmp_path` covers this project almost
entirely, since every command takes `--path`. Signed off — a separate test file
is fine.

```
test_g.py          # single file, mirroring g.py
```

No packaging needed. Verified locally: `import g` works from the repo root
under pytest's default `prepend` import mode, so `test_g.py` sitting next to
`g.py` can just `import g` and drive it with `CliRunner`. Don't reach for
`sys.path` surgery or a `conftest.py`.

UX-focused assertions worth having, most of which fail today:

- A folder named `P0001 [draft] Notes` renders with `[draft]` intact (1.1).
- Errors appear on stderr, not stdout (1.4).
- `create` emits exactly one success line (1.3).
- `verify` exits nonzero when it reports issues (3.4).
- `mv` into a category that already holds those digits does not produce two
  folders with the same prefix (6.5) — the highest-value single test here.
- `verify` reports duplicate prefixes (3.6).
- `cd` on a duplicated prefix fails rather than picking one (4.4).
- `create` after `oldify` does not reissue the retired number (6.5), with the
  `Old/` date-prefixed naming in the fixture so the regex bug can't hide.
- `oldify --date 2020-01-01` produces a `2020-01-01-` prefix (6.1).
- All four category-taking commands agree on case handling (2.2).
- `stats --category Q` errors instead of silently reporting everything (2.2).
- `--dry-run` leaves the filesystem byte-identical.
- `normalize_prefix`: `P1`/`P01`/`P001` → `P0001`; `X1`, `P12345`, `""` → `None`.
  Pure function, trivial, currently untested.

If 5.1 lands first, assert against `--json` rather than rendered tables — much
less brittle.

---

## Phase 7 — CI (GitHub Actions and `act`)

Adapted from `CIRA-GEOIPS/courier`. I read all nine of its workflows plus its
`pyproject.toml`, `.pre-commit-config.yaml`, and `cspell.config.yaml`. Most of
it assumes a packaged `./src` layout — `ruff-action` with `src: "./src"`,
`mypy ./src`, `cspell ./src`, `--cov=src`, and `pip install -e .[test]`
everywhere — none of which exists here. What follows is the adaptation.

### 7.0 Two things to do before any of it runs

**There is no git remote.** `git remote -v` is empty; the repo is one local
commit. Actions run on nothing until it's pushed.

**There is no `.gitignore`, and `__pycache__/g.cpython-312.pyc` is currently
untracked.** A `git add -A` today commits a build artifact. Add this first:

```gitignore
__pycache__/
*.py[co]
.pytest_cache/
.ruff_cache/
.mypy_cache/
```

### 7.1 No `pyproject.toml` — install deps inline

You've allowed `test_g.py`, but a `pyproject.toml` is a bigger concession to
"it is just the g file" than a test file was, and nothing here needs one. Every
workflow below installs with `pip install typer rich pytest` instead of
`pip install -e .`. Tool config goes in standalone files (`.ruff.toml`,
`mypy.ini`), which both tools read natively.

Deliberately unpinned: the matrix exists partly to notice when typer or rich
move under you. Locally you're on typer 0.21.1 / Python 3.12.14.

### 7.2 `.github/workflows/pytest.yaml`

Courier's, with the install swapped and the integration-marker step dropped.
Local Python is 3.12 only — no 3.11 or 3.13 on this machine — so the other two
matrix legs are reachable only through CI or `act`.

```yaml
name: Run Pytest
on:
  push:
    branches: 'main'
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ['3.11', '3.12', '3.13']
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install dependencies
        run: |
          python3 -m pip install --upgrade pip
          python3 -m pip install typer rich pytest
      - name: Run unit tests
        timeout-minutes: 10
        run: python3 -m pytest -v
```

One inherited inconsistency not to copy: courier pins `checkout@v4` /
`setup-python@v5` in six workflows and `@v6` in `pytest.yaml`. Pick one set.

### 7.3 `.github/workflows/smoke.yaml`

Courier's `install.yaml` tests `pip install -e .`, which doesn't apply. Replace
it with something that exercises the actual happy path — this catches import
errors and the full round trip, which is more than an install check would.

```yaml
name: Smoke
on:
  push:
    branches: 'main'
  pull_request:
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python3 -m pip install --upgrade pip typer rich
      - name: Help renders
        run: python3 g.py --help
      - name: Round trip against a scratch archive
        run: |
          set -euo pipefail
          tmp=$(mktemp -d)
          python3 g.py init --path "$tmp"
          python3 g.py create P "Test Project" --path "$tmp"
          python3 g.py mksub P1 "Notes" --path "$tmp"
          python3 g.py list P --path "$tmp"
          python3 g.py verify --path "$tmp"
          test -d "$tmp/Project/P0001 Test Project/P0001.01 Notes"
```

Note what's load-bearing there: **until 3.4 lands, the `verify` line asserts
nothing** — it exits 0 no matter what it finds. The `test -d` is what actually
fails the job. Worth a comment in the file so nobody trusts it prematurely.

And `--path` on every line is not decoration. Without it these commands write
to the runner's `~/gwarchive`, and more importantly, the same habit is what
keeps a stray local run away from your real archive.

**Do not add a `g.py verify --fix` step against the checkout.** Per 4.1, `--fix`
relocates anything it doesn't recognize at the root into `BACKUP/` — pointed at
this repo it would move `.github/`, `test_g.py`, `PLAN.md`, and `g.py` itself,
since `allowed_files` is hardcoded to `{'.gitignore', 'README.md'}`.

### 7.4 `.github/workflows/lint.yaml` and `.ruff.toml`

Courier's is 8 lines and transfers with one change:

```yaml
name: Ruff
on: [push]
jobs:
  ruff:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/ruff-action@v3
        with:
          src: "."
```

**Do not inherit courier's ruff config.** It selects
`E,F,W,I,N,D,UP,B,C4,SIM,ARG,ERA` with `max-complexity = 5`. A rough
decision-point count over `g.py` — approximate, not ruff's exact mccabe number
— puts `verify` around 30, `find` ~19, `stats` ~18, `mv` ~15, `cp` ~11, with
**11 of 16 functions over the cap** before `D` starts on the docstrings. That's
a wall of red on commit one, and a linter everyone learns to ignore.

Start narrow in `.ruff.toml` and ratchet:

```toml
target-version = "py311"
line-length = 88

[lint]
select = ["E", "F", "W", "I", "UP", "B"]
```

`B` alone will flag real things here — `list` shadowing the builtin (6.4) and
the mutable-ish default patterns. Add `SIM` and `C4` once Phase 1 lands, and
leave the complexity cap until after 6.2 has broken `mv` apart.

### 7.5 `.github/workflows/mypy.yaml` — after a small prep step

Courier's mypy config sets `disallow_untyped_defs = true`. Measured: **11 of
`g.py`'s 16 functions have no return annotation**, all of them commands (`init`,
`create`, `mksub`, `mv`, `cp`, `list`, `find`, `oldify`, `stats`, `verify`,
`cd`). That's about fifteen minutes of adding `-> None`, after which courier's
config drops in essentially unmodified. Do it in that order — annotate first,
then turn the workflow on, so the first run is green.

Put the config in `mypy.ini` rather than a pyproject:

```ini
[mypy]
python_version = 3.11
warn_return_any = True
warn_unused_configs = True
disallow_untyped_defs = True
disallow_incomplete_defs = True
no_implicit_optional = True
warn_unreachable = True
```

Workflow is courier's with `mypy ./src` → `mypy g.py test_g.py` and the
install swapped for `pip install mypy typer rich`.

### 7.6 `.pre-commit-config.yaml`

The only piece that pays off *today*, since it runs locally and needs no
remote. `pre-commit` 4.5.1 is already installed here.

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.6
    hooks:
      - id: ruff-check
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/executablebooks/mdformat
    rev: "1.0.0"
    hooks:
      - id: mdformat
        additional_dependencies: [mdformat-gfm]
  - repo: https://github.com/google/yamlfmt
    rev: v0.19.0
    hooks:
      - id: yamlfmt
  - repo: https://github.com/Mateusz-Grzelinski/actionlint-py
    rev: v1.7.9.24
    hooks:
      - id: actionlint
```

Dropped from courier's: `blacken-docs` and `validate-pyproject` (nothing to
validate). `actionlint` is worth keeping specifically because you're about to
write four workflows by hand.

One warning: **don't add a `trailing-whitespace` hook.** This file contains
nine lines of captured terminal output with meaningful trailing spaces — the
`Moved: ` / `Created directory: ` wraps that are the evidence for 1.2 and 1.3.
They're inside fenced blocks, so `mdformat` leaves them alone, but a
whitespace-stripping hook would quietly destroy the exhibit.

### 7.7 Running it locally with `act`

Neither `act` nor `gh` is installed here, and while the `docker` CLI is at
`/usr/local/bin/docker`, **the daemon isn't reachable** — no colima, podman, or
lima, and no Homebrew to install them from. So there are two prerequisites
before `act -l` will do anything:

1. A running container runtime. Without brew, that means the Docker Desktop
   `.dmg` or the Podman `.pkg` installer.
2. `act` itself — the nektos release tarball for `darwin/arm64`, or the
   project's install script (which is a `curl | sudo bash`; the tarball is the
   more inspectable option).

Then:

```sh
act -l                                   # list what would run
act -j smoke                             # one job
act push -W .github/workflows/pytest.yaml
```

Three environment-specific notes, all verified:

- **This machine is arm64** (macOS 26.5.2). `act` will pull arm64 images by
  default. `actions/setup-python` publishes arm64 Linux builds for recent
  versions but coverage is thinner than amd64 — if a matrix leg can't find
  3.11 or 3.13, re-run that job with `--container-architecture linux/amd64`
  and accept the emulation penalty.
- **The repo path contains a space** (`P0028 GWArchive`) and lives under
  `Library/CloudStorage/ProtonDrive-…`. `act` bind-mounts the working directory
  into the container; a space plus a sync-daemon-backed filesystem is a
  reliable source of confusing failures. Run `act` from a plain clone —
  `git clone . ~/src/gwarchive` — rather than debugging the mount.
- Add a `.actrc` so the runner image is explicit, since `ruff-action` needs
  node and curl and the micro image doesn't have them:

  ```
  -P ubuntu-latest=catthehacker/ubuntu:act-latest
  ```

### 7.8 Skip for now

- **`generate-badges.yaml`** — 8 KB, needs `contents: write` to commit SVGs
  back, and badges are meaningless without a remote or a README. Revisit after
  7.0 and a README exist.
- **`mutation.yaml`** — its reasoning is genuinely worth reading ("a surviving
  mutant is a change to production code that no test objected to"), and this
  project's `verify` is exactly the kind of code where coverage would lie. But
  it needs a real suite first. Queue it behind Testing.
- **`sphinx.yaml`**, **`package-and-publish.yaml`** — no docs, not published.
- **`cspell.yaml`** — cheap, but needs its own word list before it's anything
  but noise: `gwarchive`, `mksub`, `oldify`, `iterdir`, `copytree`, `PRMAO`,
  `Typer`, `pyproject`.

---

## Suggested order

1. **7.0** — `.gitignore`, then push to GitHub. Two minutes, and it unblocks
   everything else in Phase 7.
2. **Phase 1 entirely.** Smallest diff, largest visible effect; 1.1 is a
   correctness issue about displayed data, not polish. Start with 1.1 and 1.3.
3. **Test skeleton** — `test_g.py` with the markup-escaping regression, the
   `mv` collision regression, and one round trip. Then **7.2** and **7.3** so
   it runs on every push.
4. **6.2** (extract `resolve_destination`), then **6.1** and **2.1** on top of it.
5. **6.5 + 3.6 + 4.4 together.** They're one bug wearing three hats, and 6.2
   has to land first so the fix has one place to live. Settle the
   cross-category identity question before starting.
6. **2.2, 2.4, 2.5, 6.3, 6.4** — independent, batch them.
7. **Phase 4**, starting with 4.1. Don't do this without tests.
8. **Phase 3**, then **Phase 5**.
9. **7.4–7.6** whenever you want the linting; **7.5** only after the `-> None`
   annotations. `7.6` (pre-commit) can jump the queue any time — it's the one
   piece with no prerequisites.

---

## Decisions

**Resolved.**

- **Is a prefix a permanent identifier?** → Yes. Recorded; 6.5 is written
  against that answer, and `AGENTS.md` should state it.
- **Does `test_g.py` violate "it is just the g file"?** → No, an external test
  file is fine. `AGENTS.md` currently says the single-file layout is
  intentional and needs updating to match.

**Still open — this one your "yes" doesn't settle.**

- **What should a cross-category move do to the identifier?** (6.5) "Permanent"
  rules out today's letter-swap, but it doesn't choose between keeping the full
  prefix and allocating fresh digits in the destination. My recommendation is
  keeping the full prefix and making the resolver scan all categories, since
  it's the only option under which permanence is actually true — but it's a
  wider change than it looks and it's your call. Everything in step 5 above is
  blocked on it.
