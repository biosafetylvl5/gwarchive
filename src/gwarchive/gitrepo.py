"""A folder's git repositories, and copying them to the git server.

Layer 3. Two halves, kept apart on purpose:

- ``preflight`` is read-only, locally and on the server. It finds the
  repositories, resolves their names, and classifies every ref against one
  ``ls-remote``, without fetching: a server commit that is not in the local
  object database cannot be placed, so it is ``unknown`` and handled like
  ``diverged``. Every git command runs with ``GIT_OPTIONAL_LOCKS=0``.
- ``apply`` does the writes for one folder, and runs BEFORE offload snapshots
  the folder: every local write into ``.git`` (the stored name, the wip
  commit's objects, git-lfs's temporary files) has to land before
  ``snapshot_manifest``, or the delete would leave it behind as a leftover.

The push sends the shas pre-flight recorded, never ref names, so a commit made
during a long batch cannot slip past classification or a --force-git prompt.
"""

import hashlib
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from gwarchive import clock, external
from gwarchive.forge import TOPIC, Forge, ForgeError, GitConfig, url_host
from gwarchive.naming import _reserved_path, folder_prefix

MAX_NAME = 100
DESCRIPTION_HEAD = "gwarchive: "
TRUE_WORDS = ("true", "yes", "on", "1")
FALSE_WORDS = ("false", "no", "off", "0")


@dataclass
class Ref:
    ref: str
    state: str
    local: str | None
    server: str | None
    action: str = "none"

    def as_json(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "state": self.state,
            "action": self.action,
            "local": self.local,
            "server": self.server,
        }


@dataclass
class Repo:
    path: Path
    rel: str
    git_dir: Path
    bare: bool
    config: dict[str, list[str]] = field(default_factory=dict)
    repo: str | None = None
    stored: bool = False
    skipped: str | None = None
    error: str | None = None
    fixes: list[str] = field(default_factory=list)
    exists: bool = False
    created: bool = False
    archived: bool = False
    describe: str | None = None
    dirty: bool = False
    untracked: list[str] = field(default_factory=list)
    changed_bytes: int = 0
    wip: str | None = None
    lfs_used: bool = False
    lfs: str = "none"
    refs: list[Ref] = field(default_factory=list)
    archive_note: str | None = None
    archive_failed: bool = False

    def first(self, key: str) -> str | None:
        values = self.config.get(key)
        return values[-1] if values else None

    def refused(self) -> list[Ref]:
        return [r for r in self.refs if r.action == "refused"]

    def as_json(self, owner: str) -> dict[str, object]:
        return {
            "path": self.rel,
            "repo": f"{owner}/{self.repo}" if self.repo else None,
            "created": self.created,
            "archived": self.archived,
            "dirty": self.dirty,
            "wip": self.wip,
            "skipped": self.skipped,
            "error": self.error,
            "refs": [r.as_json() for r in self.refs],
            "untracked_included": self.untracked if self.wip else [],
            "lfs": self.lfs,
        }


@dataclass
class FolderGit:
    folder: Path
    category: str
    repos: list[Repo] = field(default_factory=list)

    def active(self) -> list[Repo]:
        return [r for r in self.repos if not r.skipped]

    def failed(self) -> bool:
        return any(r.error or r.refused() for r in self.repos)


@dataclass
class GitRun:
    cfg: GitConfig
    forge: Forge | None
    host: str
    wip: bool
    unattended: bool
    error: str | None = None
    folders: dict[Path, FolderGit] = field(default_factory=dict)

    def failed(self) -> bool:
        return bool(self.error) or any(f.failed() for f in self.folders.values())

    def folder_json(self, folder: Path) -> dict[str, object] | None:
        fg = self.folders.get(folder)
        repos = [] if self.error or fg is None else [r.as_json(self.cfg.owner) for r in fg.repos]
        return {"error": self.error, "repos": repos}


# --- small helpers -----------------------------------------------------------


def wip_host() -> str:
    """This machine's branch suffix: the short hostname, mapped to [a-z0-9-]."""
    short = socket.gethostname().split(".")[0].lower()
    mapped = re.sub(r"[^a-z0-9-]+", "-", short).strip("-") or "host"
    check = external.run_git(["check-ref-format", f"refs/heads/wip/{mapped}"])
    return mapped if check.returncode == 0 else "host"


def git_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in TRUE_WORDS or lowered == "":
        return True
    if lowered in FALSE_WORDS:
        return False
    return None


def computed_name(prefix: str, rel: str) -> tuple[str, bool]:
    """The first name for a repository, and whether it had to be cut short."""
    raw = prefix if rel == "." else f"{prefix}--{rel.replace('/', '--')}"
    name = re.sub(r"[^a-z0-9._-]", "-", raw.lower())
    changed = True
    while changed:
        changed = False
        for suffix in (".git", ".wiki"):
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                changed = True
    return name[:MAX_NAME], len(name) > MAX_NAME


def hashed_name(name: str, rel: str) -> str:
    """The collision-proof variant: 8 hex of the exact relative path."""
    digest = hashlib.sha256(rel.encode()).hexdigest()[:8]
    return f"{name[: MAX_NAME - 9]}-{digest}"


def holds_prefix(name: str, prefix: str) -> bool:
    """Whether a name was made from ``prefix``: as computed, or hashed.

    A hashed root-level name is ``p0001-1a2b3c4d``, with no ``--`` to split on.
    """
    return (
        name == prefix
        or name.startswith(f"{prefix}--")
        or re.fullmatch(rf"{re.escape(prefix)}-[0-9a-f]{{8}}", name) is not None
    )


def description_for(folder: Path, rel: str) -> str:
    return f"{DESCRIPTION_HEAD}{folder.name} / {rel}"


def recorded_path(description: object) -> str | None:
    """The relative path a server description records, or None if it isn't ours."""
    if not isinstance(description, str) or not description.startswith(DESCRIPTION_HEAD):
        return None
    _, sep, path = description[len(DESCRIPTION_HEAD) :].partition(" / ")
    return path if sep and path else None


def ssh_env(cfg: GitConfig, repo: Repo, unattended: bool) -> dict[str, str]:
    """ssh options for network commands, unless the user brought their own ssh."""
    if os.environ.get("GIT_SSH_COMMAND") or repo.first("core.sshcommand"):
        return {}
    opts = [
        f"ConnectTimeout={cfg.timeout}",
        "ServerAliveInterval=15",
        f"ServerAliveCountMax={max(1, math.ceil(cfg.timeout / 15))}",
    ]
    if unattended:
        opts.insert(0, "BatchMode=yes")
    return {"GIT_SSH_COMMAND": "ssh " + " ".join(f"-o {o}" for o in opts)}


def stall_options(cfg: GitConfig, *, lfs: bool = False) -> list[str]:
    """A stall limit, not a total: a large first push can rightly take minutes."""
    opts = ["-c", "http.lowSpeedLimit=1000", "-c", f"http.lowSpeedTime={cfg.timeout}"]
    if lfs:
        # git-lfs ignores the http.lowSpeed* settings.
        opts += ["-c", f"lfs.dialtimeout={cfg.timeout}", "-c", f"lfs.activitytimeout={cfg.timeout}"]
    return opts


def _git(
    repo: Repo, *args: str, env: dict[str, str] | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """A local command in the repository: no timeout, since it cannot stall on a network."""
    return external.run_git(list(args), cwd=repo.path, env=env, input_text=input_text)


# --- discovery -----------------------------------------------------------------


def _candidates(folder: Path) -> list[Path]:
    """Every directory that looks like a repository, without following symlinks.

    The walk descends into found repositories too, so clones nested inside one
    (ignored directories, vendored checkouts) are found.
    """
    found: list[Path] = []
    for root, dirs, files in os.walk(folder, followlinks=False):
        here = Path(root)
        dirs[:] = sorted(d for d in dirs if not _reserved_path(here / d, folder))
        if ".git" in dirs or ".git" in files:
            found.append(here)
            if ".git" in dirs:
                dirs.remove(".git")
        elif "HEAD" in files and "objects" in dirs and "refs" in dirs:
            found.append(here)
            dirs[:] = []
    return found


def _read_config(path: Path) -> dict[str, list[str]]:
    proc = external.run_git(["config", "--list", "-z"], cwd=path)
    config: dict[str, list[str]] = {}
    if proc.returncode != 0:
        return config
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        config.setdefault(key.lower(), []).append(value)
    return config


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def discover(folder: Path, cfg: GitConfig) -> list[Repo]:
    """The folder's repositories, each either active or carrying a skip reason."""
    repos: list[Repo] = []
    by_common: dict[Path, Repo] = {}
    worktrees: list[tuple[Repo, Path]] = []
    hosts = cfg.hosts()
    for path in _candidates(folder):
        rel = path.relative_to(folder).as_posix() if path != folder else "."
        proc = external.run_git(
            [
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                "--absolute-git-dir",
                "--is-bare-repository",
            ],
            cwd=path,
        )
        lines = proc.stdout.splitlines()
        if proc.returncode != 0 or len(lines) < 3:
            repos.append(Repo(path, rel, path, False, skipped="not a readable git repository"))
            continue
        common, git_dir, bare = Path(lines[0]).resolve(), Path(lines[1]).resolve(), lines[2] == "true"
        repo = Repo(path, rel, common, bare, config=_read_config(path))
        repos.append(repo)
        skip = git_bool(repo.first("gwarchive.git.skip"))
        # A submodule's git dir and common dir are one and the same, both under
        # an ancestor's .git/modules/; only a worktree has the two differ.
        parts = git_dir.parts
        is_submodule = ".git" in parts and "modules" in parts[parts.index(".git") :]
        if is_submodule:
            if skip is not False:
                repo.skipped = "submodule (opt in with gwarchive.git.skip=false)"
        elif git_dir != common:
            worktrees.append((repo, common))
            continue
        if skip and not repo.skipped:
            repo.skipped = "gwarchive.git.skip is set"
        stored = repo.first("gwarchive.git.repo")
        if not repo.skipped and not stored:
            for key, values in repo.config.items():
                if (
                    key.startswith("remote.")
                    and key.endswith(".url")
                    and any(url_host(v) in hosts for v in values)
                ):
                    repo.skipped = f"already there (remote {key[7:-4]} is on the server)"
                    break
        if not repo.skipped:
            by_common[common] = repo
    for repo, common in worktrees:
        main = by_common.get(common)
        if main is not None:
            repo.skipped = f"worktree of {main.rel}"
        elif not _within(common, folder):
            repo.skipped = "worktree whose repository is outside this folder"
        else:
            repo.skipped = "worktree"
    return repos


# --- naming --------------------------------------------------------------------


def _ancestor_prefixes(repo: Repo, folder: Path) -> set[str]:
    found = set()
    current = repo.path
    while True:
        pfx = folder_prefix(current.name)
        if pfx:
            found.add(pfx.lower())
        if current == folder or current.parent == current:
            break
        current = current.parent
    return found


def _unset_fix(repo: Repo) -> str:
    return f'git -C "{repo.path}" config --unset gwarchive.git.repo'


def _stored_name(repo: Repo, folder: Path, owner: str) -> None:
    """Check a stored name against the prefix rule; set repo.repo or repo.error."""
    stored = repo.first("gwarchive.git.repo") or ""
    stored_owner, sep, name = stored.partition("/")
    if not sep or not name or "/" in name:
        repo.error = f"gwarchive.git.repo is not <owner>/<name>: {stored!r}"
        repo.fixes.append(_unset_fix(repo))
        return
    if stored_owner.lower() != owner.lower():
        repo.error = f"stored name {stored} is under another owner than {owner}"
        repo.fixes.append(_unset_fix(repo))
        return
    if not any(holds_prefix(name, pfx) for pfx in _ancestor_prefixes(repo, folder)):
        repo.error = (
            f"stored name {stored} belongs to another folder's prefix: this looks like a copy "
            "(cp keeps .git/config but allocates a fresh prefix)"
        )
        repo.fixes.append(_unset_fix(repo))
        return
    repo.repo = name
    repo.stored = True


def resolve_names(fg: FolderGit, forge_: Forge, taken: dict[str, list[Repo]]) -> None:
    """Names for every active repository: stored ones checked, new ones computed.

    ``taken`` maps every name in this run to its holders, across folders, so
    two copies with one stored name are both caught.
    """
    prefix = (folder_prefix(fg.folder.name) or fg.folder.name).lower()
    owner = forge_.cfg.owner
    for repo in fg.active():
        if repo.first("gwarchive.git.repo"):
            _stored_name(repo, fg.folder, owner)
            if repo.repo:
                taken.setdefault(repo.repo, []).append(repo)
    for repo in fg.active():
        if repo.stored or repo.error:
            continue
        name, cut = computed_name(prefix, repo.rel)
        if cut or name in taken:
            name = hashed_name(name, repo.rel)
        repo.repo = name
        taken.setdefault(name, []).append(repo)


def check_server(repo: Repo, fg: FolderGit, forge_: Forge) -> bool:
    """One GET per repository: collision check, foreign-repository guard, archived flag.

    False when the repository has an error and goes no further.
    """
    assert repo.repo
    try:
        reply = forge_.get(repo.repo)
        if not repo.stored and reply.status == 200 and not _ours(reply.data, repo.rel):
            repo.repo = hashed_name(repo.repo, repo.rel)
            reply = forge_.get(repo.repo)
            if reply.status == 200 and not _ours(reply.data, repo.rel):
                repo.error = f"{forge_.cfg.owner}/{repo.repo} is taken on the server by another repository"
                return False
    except ForgeError as exc:
        repo.error = str(exc)
        return False
    if reply.status == 404:
        repo.exists = False
        return True
    if 300 <= reply.status < 400:
        new = "/".join(reply.location.rstrip("/").split("/")[-2:]) or "(unknown)"
        if repo.stored:
            repo.error = f"{forge_.cfg.owner}/{repo.repo} was renamed on the server to {new}"
            repo.fixes.append(f'git -C "{repo.path}" config gwarchive.git.repo {new}')
        else:
            repo.error = f"{forge_.cfg.owner}/{repo.repo} redirects to {new} on the server"
        return False
    if reply.status != 200 or not isinstance(reply.data, dict):
        repo.error = f"the git server answered HTTP {reply.status} for {forge_.cfg.owner}/{repo.repo}"
        return False
    data = reply.data
    topics = data.get("topics")
    recorded = recorded_path(data.get("description"))
    if not (isinstance(topics, list) and TOPIC in topics) or recorded is None:
        repo.error = (
            f"{forge_.cfg.owner}/{repo.repo} on the server is not a gwarchive copy "
            f"(no {TOPIC!r} topic or description); refusing to push into it"
        )
        repo.fixes.append(_unset_fix(repo))
        return False
    repo.exists = True
    repo.archived = bool(data.get("archived"))
    if recorded != repo.rel:
        other = fg.folder / recorded
        if (
            repo.stored
            and other.is_dir()
            and _read_config(other).get("gwarchive.git.repo", [""])[-1].endswith(f"/{repo.repo}")
        ):
            repo.error = (
                f"{repo.rel} has the stored name of {recorded}, which still exists: this looks like a copy"
            )
            repo.fixes.append(_unset_fix(repo))
            return False
    if data.get("description") != description_for(fg.folder, repo.rel):
        repo.describe = description_for(fg.folder, repo.rel)
    return True


def _ours(data: object, rel: str) -> bool:
    if not isinstance(data, dict):
        return False
    topics = data.get("topics")
    return isinstance(topics, list) and TOPIC in topics and recorded_path(data.get("description")) == rel


# --- classification --------------------------------------------------------------


def _ls_remote(run: GitRun, repo: Repo) -> dict[str, str] | None:
    assert repo.repo
    proc = external.run_git(
        ["ls-remote", run.cfg.push_url(repo.repo)],
        cwd=repo.path,
        timeout=run.cfg.timeout,
        env=ssh_env(run.cfg, repo, run.unattended),
    )
    if proc.returncode != 0:
        repo.error = f"ls-remote failed:\n{external.git_tail(proc)}"
        if "Authentication failed" in proc.stderr or "could not read Username" in proc.stderr:
            repo.fixes.append(
                "export GWARCHIVE_GITEA_GIT='ssh://git@HOST:PORT/{owner}/{repo}.git'   # push over ssh"
            )
        return None
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith(("refs/heads/", "refs/tags/")) and not ref.endswith("^{}"):
            refs[ref] = sha
    return refs


def _local_refs(repo: Repo) -> dict[str, str]:
    proc = _git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags")
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition(" ")
        refs[ref] = sha
    return refs


def _present(repo: Repo, shas: list[str]) -> set[str]:
    if not shas:
        return set()
    proc = _git(
        repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", input_text="\n".join(shas) + "\n"
    )
    return {line.split()[0] for line in proc.stdout.splitlines() if line.endswith(" commit")}


def _is_ancestor(repo: Repo, older: str, newer: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0


def classify(repo: Repo, server: dict[str, str], host: str) -> list[Ref]:
    local = _local_refs(repo)
    own_wip = f"refs/heads/wip/{host}"
    candidates = [
        server[r] for r in local if r.startswith("refs/heads/") and r in server and server[r] != local[r]
    ]
    present = _present(repo, candidates)
    out: list[Ref] = []
    for ref in sorted(set(local) | set(server)):
        if ref == own_wip:
            continue
        mine, theirs = local.get(ref), server.get(ref)
        if theirs is None:
            out.append(Ref(ref, "new", mine, None, "pushed"))
        elif mine is None:
            out.append(Ref(ref, "server-only", None, theirs))
        elif mine == theirs:
            out.append(Ref(ref, "equal", mine, theirs))
        elif ref.startswith("refs/tags/"):
            out.append(Ref(ref, "tag moved", mine, theirs, "refused"))
        elif theirs not in present:
            out.append(Ref(ref, "unknown", mine, theirs, "refused"))
        elif _is_ancestor(repo, theirs, mine):
            out.append(Ref(ref, "ahead", mine, theirs, "pushed"))
        elif _is_ancestor(repo, mine, theirs):
            out.append(Ref(ref, "behind", mine, theirs))
        else:
            out.append(Ref(ref, "diverged", mine, theirs, "refused"))
    return out


def _status(repo: Repo) -> None:
    """Dirty state and the untracked files --wip would include. Writes nothing."""
    if repo.bare:
        return
    proc = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = proc.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # the rename's source path follows
        repo.dirty = True
        if code == "??":
            repo.untracked.append(path)
        target = repo.path / path
        try:
            if target.is_file() and not target.is_symlink():
                repo.changed_bytes += target.stat().st_size
        except OSError:
            continue


def _lfs_used(repo: Repo) -> bool:
    if (repo.git_dir / "lfs" / "objects").is_dir():
        return True
    if repo.bare:
        return False
    proc = _git(repo, "ls-files", "-z", "--", ".gitattributes", "*/.gitattributes")
    for name in proc.stdout.split("\0"):
        if name:
            try:
                if "filter=lfs" in (repo.path / name).read_text(errors="replace"):
                    return True
            except OSError:
                continue
    return False


# --- pre-flight --------------------------------------------------------------------


def preflight(
    cfg: GitConfig,
    folders: list[tuple[str, Path]],
    *,
    wip: bool,
    force_git: bool,
    unattended: bool,
    offload: bool,
) -> GitRun:
    """Everything the git step needs to know, for the whole batch. Read-only."""
    token = external.read_gitea_token(cfg.token_cmd, cfg.timeout)
    run = GitRun(cfg, None, wip_host() if wip else "", wip, unattended)
    if token is None:
        run.error = "GWARCHIVE_GITEA_TOKEN_CMD failed or printed nothing"
        return run
    forge_ = Forge(cfg, token)
    try:
        forge_.probe()
    except ForgeError as exc:
        run.error = str(exc)
        return run
    run.forge = forge_
    lfs_installed = shutil.which("git-lfs") is not None
    taken: dict[str, list[Repo]] = {}
    for category, folder in folders:
        fg = FolderGit(folder, category, discover(folder, cfg))
        run.folders[folder] = fg
        resolve_names(fg, forge_, taken)
    for name, holders in taken.items():
        if len(holders) > 1:
            for repo in holders:
                if repo.stored:
                    repo.error = f"{len(holders)} repositories in this run share the stored name {name}"
                    repo.fixes.append(_unset_fix(repo))
    for fg in run.folders.values():
        for repo in fg.active():
            if repo.error:
                continue
            if not check_server(repo, fg, forge_):
                continue
            server = _ls_remote(run, repo) if repo.exists else {}
            if server is None:
                continue
            repo.refs = classify(repo, server, run.host)
            if force_git:
                for ref in repo.refs:
                    if ref.action == "refused":
                        ref.action = "forced"
            _status(repo)
            if wip and repo.dirty:
                repo.wip = f"wip/{run.host}"
            repo.lfs_used = _lfs_used(repo)
            if repo.lfs_used and not lfs_installed:
                repo.lfs = "missing"
                if offload:
                    repo.error = "uses Git LFS, but git-lfs is not installed"
                    repo.fixes.append("brew install git-lfs   # or see https://git-lfs.com")
            if not repo.exists:
                repo.created = True
    return run


def declined(run: GitRun) -> None:
    """The user said no to --force-git's prompt: forced refs go back to refused."""
    for fg in run.folders.values():
        for repo in fg.repos:
            for ref in repo.refs:
                if ref.action == "forced":
                    ref.action = "refused"


# --- the writes ----------------------------------------------------------------------


def _wip_commit(repo: Repo) -> str | None:
    """A commit of the working tree, made through a temporary index outside .git."""
    with tempfile.TemporaryDirectory(prefix="gwarchive-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        head = _git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}").stdout.strip()
        if head and _git(repo, "read-tree", head, env=env).returncode != 0:
            return None
        if _git(repo, "add", "-A", env=env).returncode != 0:
            return None
        tree = _git(repo, "write-tree", env=env)
        if tree.returncode != 0:
            return None
        ident_env: dict[str, str] = {}
        if _git(repo, "var", "GIT_COMMITTER_IDENT").returncode != 0:
            host = socket.gethostname() or "localhost"
            ident_env = {
                "GIT_AUTHOR_NAME": "gwarchive",
                "GIT_AUTHOR_EMAIL": f"gwarchive@{host}",
                "GIT_COMMITTER_NAME": "gwarchive",
                "GIT_COMMITTER_EMAIL": f"gwarchive@{host}",
            }
        args = ["commit-tree", tree.stdout.strip(), "-m", f"gwarchive wip {clock.now_stamp()}"]
        if head:
            args += ["-p", head]
        commit = _git(repo, *args, env=ident_env)
        return commit.stdout.strip() if commit.returncode == 0 else None


def _parse_porcelain(stdout: str) -> dict[str, tuple[str, str]]:
    """``to-ref -> (flag, summary)`` from ``git push --porcelain``."""
    results: dict[str, tuple[str, str]] = {}
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and len(parts[0]) == 1:
            _, _, dst = parts[1].partition(":")
            results[dst] = (parts[0], parts[2])
    return results


def _push(run: GitRun, repo: Repo, wip_sha: str | None) -> None:
    assert repo.repo
    url = run.cfg.push_url(repo.repo)
    env = ssh_env(run.cfg, repo, run.unattended)
    sending = [r for r in repo.refs if r.action in ("pushed", "forced")]
    specs = [f"{r.local}:{r.ref}" for r in sending]
    leases = [f"--force-with-lease={r.ref}:{r.server}" for r in sending if r.action == "forced"]
    wip_ref = f"refs/heads/{repo.wip}" if repo.wip else None
    if wip_sha and wip_ref:
        specs.append(f"+{wip_sha}:{wip_ref}")
    if not specs:
        return
    proc = external.run_git(
        [*stall_options(run.cfg), "push", "--porcelain", "--no-verify", *leases, url, *specs],
        cwd=repo.path,
        env=env,
    )
    results = _parse_porcelain(proc.stdout)
    problems = []
    for ref in sending:
        flag, summary = results.get(ref.ref, ("?", ""))
        if flag == "!":
            if any(k in summary for k in ("stale info", "non-fast-forward", "fetch first", "already exists")):
                ref.action = "refused"
            else:
                problems.append(f"{ref.ref}: {summary or 'rejected'}")
        elif flag == "?":
            problems.append(f"{ref.ref}: no result")
    if wip_ref and results.get(wip_ref, ("?", ""))[0] in ("!", "?"):
        problems.append(f"{wip_ref}: {results.get(wip_ref, ('', 'no result'))[1] or 'no result'}")
    if problems:
        repo.error = "push failed:\n" + ("\n".join(problems) if results else external.git_tail(proc))


def _read_back(run: GitRun, repo: Repo, wip_sha: str | None) -> None:
    """Success is what the server reports, not git's exit code."""
    server = _ls_remote(run, repo)
    if server is None:
        return
    mismatched = []
    for ref in repo.refs:
        if ref.action in ("pushed", "forced") and server.get(ref.ref) != ref.local:
            mismatched.append(ref.ref)
        if ref.ref in server:
            ref.server = server[ref.ref]
    if wip_sha and repo.wip and server.get(f"refs/heads/{repo.wip}") != wip_sha:
        mismatched.append(f"refs/heads/{repo.wip}")
    if mismatched and not repo.error:
        repo.error = "read-back: the server does not hold what was pushed for " + ", ".join(mismatched)


def _lfs_push(run: GitRun, repo: Repo, wip_sha: str | None) -> bool:
    assert repo.repo
    if not repo.lfs_used or repo.lfs == "missing":
        return True
    url = run.cfg.push_url(repo.repo)
    env = ssh_env(run.cfg, repo, run.unattended)
    proc = external.run_git(
        [*stall_options(run.cfg, lfs=True), "lfs", "push", "--all", url], cwd=repo.path, env=env
    )
    if proc.returncode == 0 and wip_sha:
        proc = external.run_git(
            [*stall_options(run.cfg, lfs=True), "lfs", "push", url, wip_sha], cwd=repo.path, env=env
        )
    if proc.returncode != 0:
        repo.error = f"git lfs push failed, so no refs were pushed:\n{external.git_tail(proc)}"
        return False
    repo.lfs = "pushed"
    return True


def _target_archived(repo: Repo, category: str, prev_category: object) -> bool | None:
    """What the archived flag should end as, or None to leave the server's choice."""
    desired = category == "Old"
    if repo.created:
        return desired
    if prev_category is None:
        return True if desired else None
    if (prev_category == "Old") != desired:
        return desired
    return None


def _set_archived(run: GitRun, repo: Repo, value: bool) -> bool:
    assert run.forge and repo.repo
    try:
        run.forge.edit(repo.repo, archived=value)
    except ForgeError as exc:
        repo.error = repo.error or str(exc)
        repo.archive_failed = True
        return False
    repo.archived = value
    return True


def apply(run: GitRun, fg: FolderGit, prev_record: dict[str, object] | None) -> None:
    """Every write for one folder. Call before snapshot_manifest; never raises for git."""
    assert run.forge
    prev_category = (prev_record or {}).get("category")
    for repo in fg.active():
        if repo.error:
            continue
        assert repo.repo
        if not repo.stored:
            store = _git(repo, "config", "gwarchive.git.repo", f"{run.cfg.owner}/{repo.repo}")
            if store.returncode != 0:
                repo.error = f"could not store the name:\n{external.git_tail(store)}"
                continue
            repo.stored = True
        wip_sha = None
        if repo.wip:
            wip_sha = _wip_commit(repo)
            if wip_sha is None:
                repo.error = "could not build the --wip commit"
                continue
        try:
            if repo.created:
                run.forge.create(repo.repo, description_for(fg.folder, repo.rel))
                repo.exists = True
            elif repo.describe:
                run.forge.edit(repo.repo, description=repo.describe)
        except ForgeError as exc:
            repo.error = str(exc)
            continue
        target = _target_archived(repo, fg.category, prev_category)
        sending = wip_sha is not None or any(r.action in ("pushed", "forced") for r in repo.refs)
        was_archived = repo.archived
        try:
            if sending:
                if repo.archived and not _set_archived(run, repo, False):
                    continue
                if _lfs_push(run, repo, wip_sha):
                    _push(run, repo, wip_sha)
                    _read_back(run, repo, wip_sha)
        finally:
            final = target if target is not None else was_archived
            changed = repo.archived != final and _set_archived(run, repo, final)
            if changed and (final != was_archived or repo.created):
                repo.archive_note = "archived" if final else "unarchived"


def record(run: GitRun, fg: FolderGit, prev_record: dict[str, object] | None) -> dict[str, object]:
    """The tombstone's ``git`` key after this run. What the server has, not what was intended."""
    prev = prev_record or {}
    prev_repos = prev.get("repos")
    by_path = {
        r.get("path"): r for r in (prev_repos if isinstance(prev_repos, list) else []) if isinstance(r, dict)
    }
    repos: list[object] = []
    for repo in fg.active():
        if repo.error and repo.rel in by_path:
            repos.append(by_path[repo.rel])
        elif repo.repo and not repo.error or repo.refused():
            repos.append(
                {
                    "path": repo.rel,
                    "repo": f"{run.cfg.owner}/{repo.repo}",
                    "refs": {r.ref: r.server for r in repo.refs if r.server},
                    "lfs": repo.lfs,
                }
            )
    out: dict[str, object] = {"server": run.cfg.url, "pushed_at": clock.now_stamp(), "repos": repos}
    # A failed archive drops the category, so the next run's archive-on-first-sight
    # rule retries it; an unconfirmed result keeps the previous one.
    if any(r.archive_failed for r in fg.active()):
        return out
    if any(r.error for r in fg.active()):
        if "category" in prev:
            out["category"] = prev["category"]
    else:
        out["category"] = fg.category
    return out


def hint_lines(repo: Repo, run: GitRun, retry: str) -> list[str]:
    """The ways forward for one repository, safest first."""
    lines: list[str] = []
    refused = repo.refused() or [r for r in repo.refs if r.action == "forced"]
    unknown = [r for r in refused if r.state == "unknown"]
    if repo.repo:
        url = run.cfg.push_url(repo.repo)
        for ref in unknown:
            short = ref.ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
            lines.append(f'git -C "{repo.path}" fetch {url} {short}')
    if repo.refused():
        note = "   # overwrites the refused refs" + (", including commits never fetched" if unknown else "")
        lines.append(f"{retry} --force-git{note}")
    lines.extend(repo.fixes)
    if repo.refused() or repo.error:
        lines.append(f'git -C "{repo.path}" config gwarchive.git.skip true')
    return lines
