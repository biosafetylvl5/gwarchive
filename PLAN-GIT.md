# Plan: copy an archive's git repositories to a Gitea server

Draft. Nothing is built yet.

## Goal

`push` and `offload` copy folders to rclone storage. Git repositories
inside those folders deserve their own copy, as repositories rather than
archived bytes. That copy keeps their history browsable, and anyone with access
can clone it. It also lets other tools find a project's code by its prefix.

This adds a `git` command group that copies every repository in an
archive folder to a Gitea (or Forgejo) server. Only that server is
written to. `origin` and every other remote are left as they are.

## Commands

The `git` group is registered after `push`/`offload` in
`commands/__init__.py`, so that is its `--help` position. It takes the
same selectors as `push`: a category letter, a prefix, or a path.

- `g git status [SEL]` is read-only. For every repository found, it shows:

  - its server repository and whether that exists;
  - each branch and tag compared with the server (`git ls-remote`, by
    commit);
  - uncommitted changes;
  - why a repository is skipped, if it is.

  It supports `--json`. It is also how a push is verified: success is
  read back from the server, not taken from git's exit code.

- `g git push [SEL]` creates missing server repositories and pushes. It
  supports `--dry-run` (with the same output shape), `--json`, `--force`
  and `--wip`.

- Both take `--recent N`: only the N repositories with the latest
  activity, meaning the newer of their last commit and the newest file in
  the working tree (`.git` and ignored files not counted). This is the
  rollout path: `g git status --recent 10`, then `g git push --recent 10`,
  then push others one at a time (`g git push PATH`) as they are worked
  on. `status` sorts by that time and shows it.

- Nothing is skipped by default. Every repository found is pushed unless
  it was excluded.

- `g git exclude PATH` / `--undo` marks a repository to skip
  (`git config gwarchive.git.skip true` in that repository, so the mark
  travels with it).

Exit codes follow the rest of `g`: 0 when everything is on the server, 1
for a runtime failure or a diverged branch, 2 for invalid input.

## Configuration

| Variable                    |                                                                                                                   |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `GWARCHIVE_GITEA_URL`       | API base, e.g. `https://git.example.org`                                                                          |
| `GWARCHIVE_GITEA_GIT`       | push URL template, e.g. `ssh://git@git.example.org:2222/{owner}/{repo}.git`; defaults to the API host over HTTPS  |
| `GWARCHIVE_GITEA_OWNER`     | the organization (or user) the copies go in                                                                       |
| `GWARCHIVE_GITEA_TOKEN_CMD` | a command whose stdout is the API token, e.g. `security find-generic-password -s g-gitea -w` or `pass show gitea` |

The token is never accepted as an option or an environment value, so it
stays out of argv, shell history and `--json`. `g` runs the command, keeps
the result in memory, and sends it only as an `Authorization` header.
`git` itself authenticates as the user already does (SSH key or credential
helper).

## Naming

- Server repositories are named by **prefix**, not by folder name, so
  `rename` changes nothing on the server:
  - a folder that is itself a repository: `p0001`;
  - a nested repository: `p0001--src--tool`, the relative path with `/`
    as `--`, lowercased;
  - a subfolder: `p0001.02--notes`.
- Names are cut to Gitea's 100 characters, and an 8-character hash is
  added when shortened. Characters outside `[a-z0-9._-]` become `-`.
- The description holds the folder name and relative path. The topic
  `gwarchive` marks every copy.
- The local repository gets a remote named `gitea` and `git config gwarchive.git.repo <owner>/<name>`. A remote already named `gitea` that
  points elsewhere is an error (exit 2), never overwritten.
- A repository whose push URL is already on this server is reported as
  "already there" and left alone.

## What a push sends

- Every local branch and tag, to the same names. **No prune:** branches
  that exist only on the server (other people's, or tools') are left
  alone.
- **Never forced by default.** A branch that has diverged is reported,
  and the run exits 1. `--force` overwrites diverged branches after the
  same confirmation as other destructive commands (`--yes` to skip it).
- **`--wip`** is opt-in, chosen repository by repository, and never on by
  default or with `--recent`. Uncommitted work is saved without touching the working tree
  or index. `git stash create` makes the commit (and `git add -A` into a
  temporary index covers untracked files), which is pushed to
  `wip/<hostname>`, overwritten each run.
- Repositories are found by walking the folder for `.git` directories and
  `.git` files (worktrees, submodules). The walk doesn't descend into a
  found repository except through its submodules, and skips the same
  names `push` skips. A tombstoned (offloaded) folder has nothing to walk
  and is skipped.

## Other commands

- `mv` to another category changes the prefix. It renames the server
  repositories (`PATCH /repos/{owner}/{repo}`) and updates the `gitea`
  remote, with the same `--dry-run`.
- `oldify` archives the server repositories (read-only, kept).
- `offload` refuses a folder whose repositories have commits the server
  lacks, unless `--force`. The tombstone records the server copies, so
  `restore` can mention them.
- `verify` warns when a repository's `gwarchive.git.repo` doesn't match its
  folder's current prefix.

## Code

Following AGENTS.md's two rules:

- `external.py` (layer 2) gains `run_git`, a seam, added to the patched
  table and to `test_the_patched_set_is_complete`.
- A new `forge.py` (layer 2) holds the Gitea API: get, create, rename,
  archive. It uses `urllib`, with no new dependency, and its one HTTP
  function, `forge.request`, is a seam too.
- A new `gitrepo.py` (layer 3) handles finding repositories, naming,
  comparing refs, and wip commits. It imports `external` and `forge` as
  modules.
- `commands/git.py` (layer 5) is the Typer sub-app.

## Tests

`tests/test_git.py` uses the autouse `isolate` fixture, and asserts on
`--json`. It uses real git with bare repositories as the server
(`GWARCHIVE_GITEA_GIT=file://…/{owner}/{repo}.git`) and a fake `forge`
that records calls. Cases:

- naming: root, nested, subfolder, long and odd paths;
- `--recent`: ordering by commit time and by working-tree time, ties
  broken by path, and N larger than the count;
- skipping: excluded, already-there, tombstoned;
- create once (idempotent);
- a diverged branch is refused without `--force`;
- no prune;
- `--wip` leaves the working tree and index byte-identical;
- the token never appears in any `run_git` argv or in `--json`;
- `origin` is unchanged;
- dry-run output has the same shape as a real run;
- `mv` renames, `oldify` archives;
- the `offload` guard.

Each test is shown able to fail: break the code, and the test must fail
for the right reason. Then a brassy fragment in `changelog/`, a
commitizen `feat:` commit, and the AGENTS.md module map and layer table
updated.

## Order

1. `git status` with tests (read-only).
1. `git push` and `exclude`.
1. The `mv`, `oldify`, `offload` and `verify` hooks.
1. Release through the normal `cz bump` path.

## Not in scope

- Pull mirrors of upstream projects: the server can do that itself.
- Pushing anywhere but the configured server.
- Scheduling: a user's cron or launchd job runs `g git push P --json`.
