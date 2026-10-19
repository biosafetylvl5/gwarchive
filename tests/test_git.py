"""The git server: push and offload copying a folder's repositories to Gitea.

Real git throughout, isolated from the developer's configuration. The server
is two halves: bare repositories under tmp_path take the pushes (through a
file:// push URL), and a fake ``forge.request`` plays the Gitea API, creating
the bare repository when asked to create one.
"""

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import run

from gwarchive import external, forge, tombstone

OWNER = "lab"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@dataclass
class Server:
    root: Path
    repos: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str, object]] = field(default_factory=list)
    down: bool = False

    def bare(self, name: str) -> Path:
        return self.root / OWNER / f"{name}.git"

    def refs(self, name: str) -> dict[str, str]:
        out = git(self.bare(name), "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ") for line in out.splitlines())

    def archived(self, name: str) -> object:
        """Read fresh each time, so successive asserts are not narrowed together."""
        return self.repos[name]["archived"]

    def writes(self) -> list[tuple[str, str, object]]:
        return [c for c in self.calls if c[0] != "GET"]

    def request(
        self, method: str, url: str, *, token: str, body: object | None = None, timeout: float
    ) -> forge.Reply:
        assert token == "hunter2"
        path = url.split("/api/v1", 1)[1]
        self.calls.append((method, path, body))
        if self.down:
            raise forge.ForgeError("cannot reach the git server: connection refused")
        if method == "GET" and path == f"/orgs/{OWNER}":
            return forge.Reply(200, {"username": OWNER})
        if method == "POST" and path == f"/orgs/{OWNER}/repos":
            assert isinstance(body, dict)
            name = str(body["name"])
            self.repos[name] = {**body, "topics": [], "archived": False}
            self.bare(name).mkdir(parents=True)
            git(self.bare(name), "init", "--bare", "-q")
            return forge.Reply(201, self.repos[name])
        prefix = f"/repos/{OWNER}/"
        if path.startswith(prefix):
            name, _, rest = path[len(prefix) :].partition("/")
            repo = self.repos.get(name)
            if repo is None:
                return forge.Reply(404, {"message": "not found"})
            if method == "GET":
                return forge.Reply(200, repo)
            if method == "PUT" and rest == "topics":
                assert isinstance(body, dict)
                repo["topics"] = body["topics"]
                return forge.Reply(204, None)
            if method == "PATCH":
                assert isinstance(body, dict)
                repo.update(body)
                return forge.Reply(200, repo)
        return forge.Reply(404, {"message": f"unexpected {method} {path}"})


@pytest.fixture(autouse=True)
def gitenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real git, but none of the developer's configuration."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.org",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.org",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "init.defaultBranch",
        "GIT_CONFIG_VALUE_0": "main",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Server:
    srv = Server(tmp_path / "server")
    monkeypatch.setattr(forge, "request", srv.request)
    monkeypatch.setenv("GWARCHIVE_GITEA_URL", "https://git.example.org")
    monkeypatch.setenv("GWARCHIVE_GITEA_OWNER", OWNER)
    monkeypatch.setenv("GWARCHIVE_GITEA_TOKEN_CMD", "printf hunter2")
    monkeypatch.setenv("GWARCHIVE_GITEA_GIT", f"file://{srv.root}/{{owner}}/{{repo}}.git")
    return srv


@pytest.fixture
def remote(tmp_path: Path, rclone_local: list[list[str]]) -> Path:
    return tmp_path / "remote"


def make_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for name, body in (files or {"README": "hello\n"}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


def commit(path: Path, name: str = "more", body: str = "more\n") -> str:
    (path / name).write_text(body)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", name)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def alpha(archive: Path) -> Path:
    """P0001 Alpha [draft], holding a repository at src/tool."""
    assert run("create", "P", "Alpha [draft]", "--path", archive).exit_code == 0
    folder = archive / "Project" / "P0001 Alpha [draft]"
    make_repo(folder / "src" / "tool")
    return folder


def push_json(archive: Path, remote: Path, *args: object) -> tuple[int, list[dict[str, object]], str]:
    res = run("push", "P1", "--remote", remote, "--json", "--path", archive, *args)
    return res.exit_code, json.loads(res.stdout) if res.stdout.strip() else [], res.stderr


def repos_of(entry: dict[str, object]) -> list[dict[str, object]]:
    git_ = entry["git"]
    assert isinstance(git_, dict)
    return git_["repos"]  # type: ignore[no-any-return]


def refs_of(repo: dict[str, object]) -> list[dict[str, object]]:
    refs = repo["refs"]
    assert isinstance(refs, list)
    return refs


# --- the basic round ---------------------------------------------------------------


def test_push_creates_a_private_tagged_repository_and_pushes_every_ref(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    tool = alpha / "src" / "tool"
    git(tool, "tag", "v1")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    (repo,) = repos_of(entry)
    assert repo["repo"] == "lab/p0001--src--tool"
    assert repo["created"] is True
    assert server.repos["p0001--src--tool"]["private"] is True
    assert server.repos["p0001--src--tool"]["topics"] == ["gwarchive"]
    assert server.repos["p0001--src--tool"]["description"] == "gwarchive: P0001 Alpha [draft] / src/tool"
    head = git(tool, "rev-parse", "HEAD")
    assert server.refs("p0001--src--tool")["refs/heads/main"] == head
    assert "refs/tags/v1" in server.refs("p0001--src--tool")
    assert git(tool, "config", "gwarchive.git.repo") == "lab/p0001--src--tool"
    record = (tombstone.read_tombstone(alpha) or {})["git"]
    assert isinstance(record, dict)
    assert record["category"] == "Project"
    assert record["repos"][0]["refs"]["refs/heads/main"] == head


def test_a_second_push_is_up_to_date_and_creates_nothing(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    assert push_json(archive, remote)[0] == 0
    server.calls.clear()
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    (repo,) = repos_of(entry)
    assert repo["created"] is False
    assert {r["action"] for r in refs_of(repo)} == {"none"}
    assert server.writes() == []


def test_the_receipt_has_a_git_row_and_survives_markup(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 0, res.stderr
    assert "P0001 Alpha [draft]" in res.stdout
    assert "git   git.example.org/lab  1 repository: 1 pushed, 1 created" in res.stdout
    assert "P0001 src/tool: created lab/p0001--src--tool" in res.stdout


def test_without_a_server_push_output_is_unchanged(archive: Path, alpha: Path, remote: Path) -> None:
    res = run("push", "P1", "--remote", remote, "--json", "--path", archive)
    assert res.exit_code == 0
    (entry,) = json.loads(res.stdout)
    assert entry["git"] is None
    assert "git" not in (tombstone.read_tombstone(alpha) or {})


# --- setup ---------------------------------------------------------------------------


def test_every_setup_problem_is_reported_at_once_before_any_transfer(
    archive: Path, alpha: Path, monkeypatch: pytest.MonkeyPatch, rclone_local: list[list[str]], remote: Path
) -> None:
    monkeypatch.setenv("GWARCHIVE_GITEA_URL", "git.example.org")
    monkeypatch.setenv("GWARCHIVE_GITEA_GIT", "ssh://git@host/x.git")
    monkeypatch.setenv("GWARCHIVE_GITEA_TIMEOUT", "0")
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 2
    for needle in ("GWARCHIVE_GITEA_URL", "OWNER", "TOKEN_CMD", "{owner}", "GWARCHIVE_GITEA_TIMEOUT"):
        assert needle in res.stderr
    assert rclone_local == []


@pytest.mark.parametrize("value", ["0", "abc", "-5", "1.5"])
def test_a_bad_timeout_is_a_setup_error(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GWARCHIVE_GITEA_TIMEOUT", value)
    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 2


def test_force_git_json_needs_yes_even_when_nothing_is_refused(
    archive: Path, alpha: Path, server: Server, rclone_local: list[list[str]], remote: Path
) -> None:
    res = run("push", "P1", "--remote", remote, "--force-git", "--json", "--path", archive)
    assert res.exit_code == 2
    assert "--force-git --json --yes" in res.stderr
    assert rclone_local == [] and server.calls == []


def test_an_unknown_owner_is_a_setup_error(
    archive: Path, alpha: Path, server: Server, monkeypatch: pytest.MonkeyPatch, remote: Path
) -> None:
    monkeypatch.setenv("GWARCHIVE_GITEA_OWNER", "nobody")

    def answer(
        method: str, url: str, *, token: str, body: object | None = None, timeout: float
    ) -> forge.Reply:
        if url.endswith("/user"):
            return forge.Reply(200, {"login": "me"})
        return forge.Reply(404, {"message": "not found"})

    monkeypatch.setattr(forge, "request", answer)
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 2
    assert "GWARCHIVE_GITEA_OWNER=me" in res.stderr


def test_a_missing_git_is_exit_1_and_no_git_still_copies(
    archive: Path,
    alpha: Path,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    rclone_local: list[list[str]],
    remote: Path,
) -> None:
    monkeypatch.setattr(forge.shutil, "which", lambda _name: None)
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 1
    assert "--no-git" in res.stderr
    monkeypatch.setenv("GWARCHIVE_GITEA_TIMEOUT", "nope")
    assert run("push", "P1", "--remote", remote, "--no-git", "--path", archive).exit_code == 0
    assert rclone_local
    assert server.calls == []


# --- soft failures in push ---------------------------------------------------------------


def test_a_server_that_is_down_still_gets_the_rclone_copy_then_exit_1(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    server.down = True
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    assert entry["remotes"]
    assert entry["git"] == {"error": "cannot reach the git server: connection refused", "repos": []}
    assert len(server.calls) == 1
    assert "connection refused" in err
    meta = tombstone.read_tombstone(alpha) or {}
    assert meta["remotes"] and "git" not in meta


def test_a_failing_token_command_is_run_wide_and_soft(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GWARCHIVE_GITEA_TOKEN_CMD", "false")
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    assert entry["remotes"]
    assert "GWARCHIVE_GITEA_TOKEN_CMD" in entry["git"]["error"]  # type: ignore[index]
    assert server.calls == []
    assert "hunter2" not in err


def test_the_git_record_survives_an_rclone_failure(
    archive: Path, alpha: Path, server: Server, rclone: Callable[..., list[list[str]]], tmp_path: Path
) -> None:
    rclone(returncode=1)
    res = run("push", "P1", "--remote", "nas:x", "--no-compress", "--path", archive)
    assert res.exit_code == 1
    meta = tombstone.read_tombstone(alpha) or {}
    assert meta["git"]["repos"][0]["repo"] == "lab/p0001--src--tool"  # type: ignore[index]
    assert "remotes" not in meta


def test_a_no_compress_failure_keeps_the_previous_archive_block(
    archive: Path, alpha: Path, server: Server, rclone: Callable[..., list[list[str]]]
) -> None:
    previous: dict[str, object] = {"archive": {"versions": [{"name": "old"}]}, "prefix": "P0001"}
    tombstone.write_tombstone(alpha, previous)
    rclone(returncode=1)
    assert run("push", "P1", "--remote", "nas:x", "--no-compress", "--path", archive).exit_code == 1
    meta = tombstone.read_tombstone(alpha) or {}
    assert meta["archive"] == previous["archive"]
    assert set(meta) == {"archive", "prefix", "git"}


def test_a_timeout_on_one_repository_stays_with_that_repository(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_repo(alpha / "other")
    real = external.run_git

    def slow(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in args and "other" in str(args[-1]):
            return subprocess.CompletedProcess(args, -9, "", "git timed out after 60 seconds")
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", slow)
    code, (entry,), _ = push_json(archive, remote)
    assert code == 1
    by_path = {r["path"]: r for r in repos_of(entry)}
    assert "timed out" in str(by_path["other"]["error"])
    assert by_path["src/tool"]["error"] is None
    assert "refs/heads/main" in server.refs("p0001--src--tool")


def test_a_timeout_kills_grandchildren_holding_the_pipes(tmp_path: Path) -> None:
    script = tmp_path / "fake"
    script.write_text("#!/bin/sh\nsleep 30 &\nsleep 30\n")
    script.chmod(0o755)
    import time

    start = time.monotonic()
    proc = external._run_bounded([str(script)], cwd=None, env=os.environ, timeout=0.5, input_text=None)
    assert time.monotonic() - start < 10
    assert proc.returncode == -9
    assert "timed out" in proc.stderr


# --- refs ---------------------------------------------------------------------------------


def _diverge(server: Server, tool: Path, tmp_path: Path, branch: str = "main") -> str:
    """Put a commit on the server that the local repository has never fetched."""
    other = tmp_path / "elsewhere"
    git(tmp_path, "clone", "-q", str(server.bare("p0001--src--tool")), str(other))
    sha = commit(other, "theirs")
    git(other, "push", "-q", "origin", f"HEAD:{branch}")
    return sha


def test_an_unknown_server_commit_is_refused_with_the_fetch_hint_first(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    theirs = _diverge(server, tool, tmp_path)
    commit(tool, "mine")
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 1
    assert "unknown main (the server has commits this repository never fetched)" in res.stderr
    tries = [line for line in res.stderr.splitlines() if "try:" in line]
    assert " fetch " in tries[0]
    assert "--force-git" in tries[1] and "never fetched" in tries[1]
    assert "gwarchive.git.skip true" in tries[2]
    assert server.refs("p0001--src--tool")["refs/heads/main"] == theirs


def test_force_git_overwrites_after_the_prompt_and_never_touches_local_files(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    _diverge(server, tool, tmp_path)
    mine = commit(tool, "mine")
    before = git(tool, "for-each-ref")
    res = run("push", "P1", "--remote", remote, "--force-git", "--path", archive, input="n\n")
    assert res.exit_code == 1
    assert server.refs("p0001--src--tool")["refs/heads/main"] != mine
    res = run("push", "P1", "--remote", remote, "--force-git", "--path", archive, input="y\n")
    assert res.exit_code == 0, res.stderr
    assert server.refs("p0001--src--tool")["refs/heads/main"] == mine
    assert git(tool, "for-each-ref") == before


def test_dry_run_force_git_never_prompts_and_lists_what_it_would_overwrite(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    _diverge(server, tool, tmp_path)
    commit(tool, "mine")
    res = run("push", "P1", "--remote", remote, "--force-git", "--dry-run", "--path", archive)
    assert res.exit_code == 0
    assert "would overwrite main on the server" in res.stdout


def test_behind_exits_0(archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    theirs = _diverge(server, tool, tmp_path)
    git(tool, "fetch", "-q", str(server.bare("p0001--src--tool")), "main")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    (ref,) = [r for r in refs_of(repos_of(entry)[0]) if r["ref"] == "refs/heads/main"]
    assert ref["state"] == "behind" and ref["server"] == theirs


def test_a_commit_made_after_preflight_is_not_pushed(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = alpha / "src" / "tool"
    first = git(tool, "rev-parse", "HEAD")
    real = external.run_git

    def commit_then_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        # The last moment: pre-flight is done and `git push` is about to run.
        if "push" in args and git(tool, "log", "--format=%s", "-1") != "late":
            commit(tool, "late")
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", commit_then_push)
    assert push_json(archive, remote)[0] == 0
    assert server.refs("p0001--src--tool")["refs/heads/main"] == first


def test_a_server_moved_after_preflight_turns_a_fast_forward_into_refused(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    commit(tool, "mine")
    real = external.run_git
    theirs: list[str] = []

    def move_then_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "push" in args and not theirs:
            theirs.append(_diverge(server, tool, tmp_path))
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", move_then_push)
    code, (entry,), _ = push_json(archive, remote)
    assert code == 1
    (ref,) = [r for r in refs_of(repos_of(entry)[0]) if r["ref"] == "refs/heads/main"]
    assert ref["action"] == "refused"
    assert server.refs("p0001--src--tool")["refs/heads/main"] == theirs[0]


def test_force_git_loses_its_lease_to_a_server_moved_after_preflight(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    _diverge(server, tool, tmp_path)
    commit(tool, "mine")
    real = external.run_git
    later: list[str] = []

    def move_then_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "push" in args and not later:
            other = tmp_path / "elsewhere"
            later.append(commit(other, "theirs again"))
            git(other, "push", "-q", "origin", "HEAD:main")
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", move_then_push)
    code, (entry,), _ = push_json(archive, remote, "--force-git", "--yes")
    assert code == 1
    (ref,) = [r for r in refs_of(repos_of(entry)[0]) if r["ref"] == "refs/heads/main"]
    assert ref["action"] == "refused"
    assert server.refs("p0001--src--tool")["refs/heads/main"] == later[0]


def test_a_failed_lfs_push_sends_no_refs(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gwarchive import gitrepo

    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    before = server.refs("p0001--src--tool")
    (tool / ".git" / "lfs" / "objects").mkdir(parents=True)
    commit(tool, "big")
    monkeypatch.setattr(gitrepo.shutil, "which", lambda name: f"/usr/bin/{name}")
    real = external.run_git
    order: list[str] = []

    def failing_lfs(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "lfs" in args and "push" in args:
            order.append("lfs")
            return subprocess.CompletedProcess(args, 1, "", "batch request: 413")
        if "push" in args:
            order.append("refs")
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", failing_lfs)
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    assert order == ["lfs"]
    assert "git lfs push failed, so no refs were pushed" in err
    assert server.refs("p0001--src--tool") == before
    assert entry["remotes"]


def test_a_run_wide_failure_keeps_the_previous_git_record(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    assert push_json(archive, remote)[0] == 0
    previous = (tombstone.read_tombstone(alpha) or {})["git"]
    commit(alpha / "src" / "tool", "later")
    server.down = True
    assert push_json(archive, remote)[0] == 1
    assert (tombstone.read_tombstone(alpha) or {})["git"] == previous


def test_offload_dry_run_refuses_like_the_real_run(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path, rclone_local: list[list[str]]
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    _diverge(server, tool, tmp_path)
    commit(tool, "mine")
    rclone_local.clear()
    res = run("offload", "P1", "--remote", remote, "--dry-run", "--path", archive)
    assert res.exit_code == 1
    assert "diverged main" in res.stderr or "unknown main" in res.stderr
    assert rclone_local == []
    assert (tool / "README").exists()


def test_leaving_old_unarchives_what_g_archived(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    assert run("oldify", "P1", "--path", archive).exit_code == 0
    assert run("push", "O", "--remote", remote, "--path", archive).exit_code == 0
    assert server.archived("p0001--src--tool") is True
    (old,) = (archive / "Old").iterdir()
    old.rename(alpha)
    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 0
    assert server.archived("p0001--src--tool") is False


# --- dry runs ---------------------------------------------------------------------------------


def _git_state(repo: Path) -> tuple[str, str, bytes]:
    index = repo / ".git" / "index"
    return git(repo, "for-each-ref"), (repo / ".git" / "config").read_text(), index.read_bytes()


def test_dry_run_writes_nothing_and_reports_the_same_shape(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    tool = alpha / "src" / "tool"
    (tool / "README").write_text("dirty\n")
    os.utime(tool / "README", (1, 1))
    before = _git_state(tool)
    index_mtime = (tool / ".git" / "index").stat().st_mtime_ns
    code, (dry,), _ = push_json(archive, remote, "--dry-run")
    assert code == 0
    assert server.writes() == []
    assert _git_state(tool) == before
    assert (tool / ".git" / "index").stat().st_mtime_ns == index_mtime
    code, (real,), _ = push_json(archive, remote)
    assert set(dry) == set(real)
    assert set(repos_of(dry)[0]) == set(repos_of(real)[0])


def test_dry_run_no_git_makes_no_call(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("no git call expected")

    monkeypatch.setattr(external, "run_git", boom)
    assert run("push", "P1", "--remote", remote, "--dry-run", "--no-git", "--path", archive).exit_code == 0
    assert server.calls == []


def test_push_dry_run_exits_1_on_a_failed_probe(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    server.down = True
    assert run("push", "P1", "--remote", remote, "--dry-run", "--path", archive).exit_code == 1


def test_no_hook_runs(archive: Path, alpha: Path, server: Server, remote: Path) -> None:
    tool = alpha / "src" / "tool"
    marker = alpha.parent.parent / "hook-ran"
    for hook in ("pre-push", "post-index-change", "reference-transaction"):
        path = tool / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        path.chmod(0o755)
    (tool / "new.txt").write_text("x\n")
    assert push_json(archive, remote, "--wip")[0] == 0
    assert not marker.exists()


# --- --wip --------------------------------------------------------------------------------


def test_wip_pushes_the_working_tree_and_leaves_it_untouched(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    tool = alpha / "src" / "tool"
    (tool / "README").write_text("edited\n")
    (tool / ".env").write_text("SECRET=1\n")
    git(tool, "add", "README")
    status = git(tool, "status", "--porcelain")
    index = (tool / ".git" / "index").read_bytes()
    code, (entry,), _ = push_json(archive, remote, "--wip")
    assert code == 0
    (repo,) = repos_of(entry)
    assert str(repo["wip"]).startswith("wip/")
    assert repo["untracked_included"] == [".env"]
    ref = f"refs/heads/{repo['wip']}"
    sha = server.refs("p0001--src--tool")[ref]
    assert git(server.bare("p0001--src--tool"), "show", f"{sha}:.env") == "SECRET=1"
    assert git(tool, "status", "--porcelain") == status
    assert (tool / ".git" / "index").read_bytes() == index
    # The tombstone counts what is on disk now, objects included.
    from gwarchive import paths

    assert (tombstone.read_tombstone(alpha) or {})["file_count"] == paths.compute_folder_stats(alpha)[1]
    assert entry["file_count"] == paths.compute_folder_stats(alpha)[1]
    # A second run replaces the wip branch without --force-git.
    (tool / ".env").write_text("SECRET=2\n")
    assert push_json(archive, remote, "--wip")[0] == 0
    assert server.refs("p0001--src--tool")[ref] != sha


def test_wip_works_without_a_head(archive: Path, server: Server, remote: Path) -> None:
    run("create", "P", "Empty", "--path", archive)
    folder = archive / "Project" / "P0001 Empty"
    folder.joinpath("repo").mkdir()
    git(folder / "repo", "init", "-q", "-b", "main")
    (folder / "repo" / "a").write_text("a\n")
    code, (entry,), _ = push_json(archive, remote, "--wip")
    assert code == 0, entry
    assert repos_of(entry)[0]["wip"]


# --- naming -------------------------------------------------------------------------------


def test_names_root_bare_and_odd(archive: Path, server: Server, remote: Path) -> None:
    run("create", "P", "Names", "--path", archive)
    folder = archive / "Project" / "P0001 Names"
    make_repo(folder)
    make_repo(folder / "Odd Name!")
    bare = folder / "mirror.git"
    git(folder, "clone", "-q", "--bare", str(folder), str(bare))
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    names = sorted(str(r["repo"]) for r in repos_of(entry))
    assert names == ["lab/p0001", "lab/p0001--mirror", "lab/p0001--odd-name-"]


def test_a_taken_name_gets_a_hash(archive: Path, alpha: Path, server: Server, remote: Path) -> None:
    server.repos["p0001--src--tool"] = {"description": "someone else's", "topics": [], "archived": False}
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    name = str(repos_of(entry)[0]["repo"])
    assert name.startswith("lab/p0001--src--tool-") and len(name) == len("lab/p0001--src--tool-") + 8


def test_a_copied_repository_is_an_error_and_the_rest_still_push(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    assert push_json(archive, remote)[0] == 0
    run("create", "P", "Beta", "--path", archive)
    beta = archive / "Project" / "P0002 Beta"
    subprocess.run(["cp", "-R", str(alpha / "src"), str(beta / "src")], check=True)
    make_repo(beta / "fresh")
    res = run("push", "P2", "--remote", remote, "--json", "--path", archive)
    assert res.exit_code == 1
    by_path = {r["path"]: r for r in json.loads(res.stdout)[0]["git"]["repos"]}
    assert "copy" in by_path["src/tool"]["error"]
    assert by_path["fresh"]["error"] is None
    assert "config --unset gwarchive.git.repo" in res.stderr


def test_a_foreign_repository_under_the_stored_name_is_refused(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    assert push_json(archive, remote)[0] == 0
    server.repos["p0001--src--tool"]["topics"] = []
    commit(alpha / "src" / "tool")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 1
    assert "not a gwarchive copy" in str(repos_of(entry)[0]["error"])
    assert not [c for c in server.writes() if c[0] == "PATCH"]


def test_a_stored_name_whose_repository_is_gone_is_created_again(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    assert push_json(archive, remote)[0] == 0
    del server.repos["p0001--src--tool"]
    import shutil

    shutil.rmtree(server.bare("p0001--src--tool"))
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    assert repos_of(entry)[0]["created"] is True


# --- skipping -----------------------------------------------------------------------------


def test_skip_config_and_already_there(archive: Path, alpha: Path, server: Server, remote: Path) -> None:
    skipped = make_repo(alpha / "skipped")
    git(skipped, "config", "gwarchive.git.skip", "true")
    there = make_repo(alpha / "there")
    git(there, "remote", "add", "origin", "git@git.example.org:lab/there.git")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    by_path = {r["path"]: r for r in repos_of(entry)}
    assert by_path["skipped"]["skipped"] == "gwarchive.git.skip is set"
    assert str(by_path["there"]["skipped"]).startswith("already there")
    assert by_path["src/tool"]["skipped"] is None
    res = run("--verbose", "push", "P1", "--remote", remote, "--dry-run", "--path", archive)
    assert f'undo: git -C "{skipped}" config --unset gwarchive.git.skip' in res.stdout


def test_a_submodule_is_skipped_until_it_opts_in(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path
) -> None:
    lib = make_repo(tmp_path / "lib")
    top = make_repo(alpha / "top")
    git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", f"file://{lib}", "sub")
    git(top, "commit", "-q", "-m", "sub")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    by_path = {r["path"]: r for r in repos_of(entry)}
    assert str(by_path["top/sub"]["skipped"]).startswith("submodule")
    assert "p0001--top--sub" not in server.repos
    git(top / "sub", "config", "gwarchive.git.skip", "false")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    assert "p0001--top--sub" in server.repos


def test_a_hashed_root_name_is_accepted_on_every_later_run(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    make_repo(alpha)
    server.repos["p0001"] = {"name": "p0001", "topics": [], "description": "someone else's"}
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    (root,) = [r for r in repos_of(entry) if r["path"] == "."]
    name = str(root["repo"])
    assert name.startswith("lab/p0001-") and "--" not in name
    assert git(alpha, "config", "gwarchive.git.repo") == name
    code, (entry,), err = push_json(archive, remote)
    assert code == 0, err
    assert run("verify", "--path", archive).exit_code == 0


# --- offload ------------------------------------------------------------------------------


def test_offload_deletes_everything_and_leaves_no_leftover(
    archive: Path, alpha: Path, server: Server, remote: Path, gzip_codec: None
) -> None:
    (alpha / "src" / "tool" / "loose.txt").write_text("wip\n")
    res = run("offload", "P1", "--remote", remote, "--yes", "--wip", "--json", "--path", archive)
    assert res.exit_code == 0, res.stderr
    (entry,) = json.loads(res.stdout)
    assert entry["offloaded"] is True
    assert [p.name for p in alpha.iterdir()] == [".gwarchive-offload.json"]
    meta = tombstone.read_tombstone(alpha) or {}
    assert meta["offloaded_at"] and meta["git"]["repos"]  # type: ignore[index]
    # The rclone copy's .git carries the stored name, so restore brings it back.
    assert run("restore", "P1", "--yes", "--path", archive).exit_code == 0
    assert git(alpha / "src" / "tool", "config", "gwarchive.git.repo") == "lab/p0001--src--tool"


def test_offload_refuses_the_whole_batch_on_a_refused_ref(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path, rclone_local: list[list[str]]
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    _diverge(server, tool, tmp_path)
    commit(tool, "mine")
    run("create", "P", "Beta", "--path", archive)
    (archive / "Project" / "P0002 Beta" / "f").write_text("x")
    rclone_local.clear()
    res = run("offload", "P", "--remote", remote, "--yes", "--path", archive)
    assert res.exit_code == 1
    assert "Cannot offload" in res.stderr
    tries = [line for line in res.stderr.splitlines() if "try:" in line]
    assert " fetch " in tries[0]
    assert "offload P --no-git" in tries[1]
    assert "offload P --force-git" in tries[2]
    assert rclone_local == []
    assert (archive / "Project" / "P0002 Beta" / "f").exists()


def test_offload_whose_git_step_fails_keeps_the_folder_live(
    archive: Path,
    alpha: Path,
    server: Server,
    remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    gzip_codec: None,
) -> None:
    real = external.run_git

    def failing_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "push" in args:
            return subprocess.CompletedProcess(args, 1, "", "fatal: the server hung up")
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", failing_push)
    res = run("offload", "P1", "--remote", remote, "--yes", "--json", "--path", archive)
    assert res.exit_code == 1
    (entry,) = json.loads(res.stdout)
    assert entry["offloaded"] is False
    assert (alpha / "src" / "tool" / "README").exists()
    meta = tombstone.read_tombstone(alpha) or {}
    assert "offloaded_at" not in meta and meta["remotes"]
    monkeypatch.setattr(external, "run_git", real)
    assert run("push", "P1", "--remote", remote, "--path", archive).exit_code == 0


def test_offload_summary_shows_the_git_row_and_dirty_repositories(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    (alpha / "src" / "tool" / "README").write_text("dirty\n")
    res = run("offload", "P1", "--remote", remote, "--path", archive, input="n\n")
    assert res.exit_code == 1
    assert "git.example.org/lab  1 repository" in res.stderr
    assert "P0001 src/tool has uncommitted changes" in res.stderr
    assert server.writes() == []
    assert "gwarchive.git.repo" not in (alpha / "src" / "tool" / ".git" / "config").read_text()


# --- archived state -----------------------------------------------------------------------


def test_a_new_repository_in_old_is_created_pushed_then_archived(
    archive: Path, alpha: Path, server: Server, tmp_path: Path, rclone_local: list[list[str]]
) -> None:
    assert run("oldify", "P1", "--path", archive).exit_code == 0
    remote = tmp_path / "remote"
    res = run("push", "O", "--remote", remote, "--json", "--path", archive)
    assert res.exit_code == 0, res.stderr
    (name,) = server.repos
    assert server.repos[name]["archived"] is True
    assert "refs/heads/main" in server.refs(name)
    patches = [c for c in server.calls if c[0] == "PATCH"]
    assert patches == [("PATCH", f"/repos/{OWNER}/{name}", {"archived": True})]


def test_entering_old_archives_and_a_manual_unarchive_is_respected(
    archive: Path, alpha: Path, server: Server, tmp_path: Path, rclone_local: list[list[str]]
) -> None:
    remote = tmp_path / "remote"
    assert push_json(archive, remote)[0] == 0
    assert server.archived("p0001--src--tool") is False
    assert run("oldify", "P1", "--path", archive).exit_code == 0
    assert run("push", "O", "--remote", remote, "--path", archive).exit_code == 0
    assert server.archived("p0001--src--tool") is True
    # The user un-archives it on the server: g leaves that choice alone.
    server.repos["p0001--src--tool"]["archived"] = False
    server.calls.clear()
    assert run("push", "O", "--remote", remote, "--path", archive).exit_code == 0
    assert server.archived("p0001--src--tool") is False
    assert not [c for c in server.calls if c[0] == "PATCH"]


def test_new_commits_to_an_archived_copy_re_archive_it_even_when_the_push_fails(
    archive: Path,
    alpha: Path,
    server: Server,
    tmp_path: Path,
    rclone_local: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = tmp_path / "remote"
    assert run("oldify", "P1", "--path", archive).exit_code == 0
    assert run("push", "O", "--remote", remote, "--path", archive).exit_code == 0
    (old,) = (archive / "Old").iterdir()
    commit(old / "src" / "tool", "late")
    real = external.run_git

    def failing_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "push" in args:
            assert server.archived("p0001--src--tool") is False
            return subprocess.CompletedProcess(args, 1, "", "fatal: hung up")
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", failing_push)
    assert run("push", "O", "--remote", remote, "--path", archive).exit_code == 1
    assert server.archived("p0001--src--tool") is True


# --- invariants -----------------------------------------------------------------------------


def test_the_token_never_reaches_git_json_or_stderr(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = external.run_git

    def spy(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(" ".join(args) + json.dumps(kwargs.get("env") or {}))
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", spy)
    res = run("push", "P1", "--remote", remote, "--json", "--path", archive)
    assert res.exit_code == 0
    assert seen
    assert not [s for s in seen if "hunter2" in s]
    assert "hunter2" not in res.stdout + res.stderr


def test_origin_and_the_rest_of_the_config_are_unchanged(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    tool = alpha / "src" / "tool"
    git(tool, "remote", "add", "origin", "https://example.com/tool.git")
    before = git(tool, "config", "--local", "--list")
    assert push_json(archive, remote)[0] == 0
    after = git(tool, "config", "--local", "--list")
    assert after.replace("\ngwarchive.git.repo=lab/p0001--src--tool", "") == before


def test_offload_refuses_a_repository_using_lfs_without_git_lfs(
    archive: Path,
    alpha: Path,
    server: Server,
    remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    rclone_local: list[list[str]],
) -> None:
    from gwarchive import gitrepo

    tool = alpha / "src" / "tool"
    (tool / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    git(tool, "add", ".gitattributes")
    git(tool, "commit", "-q", "-m", "lfs")
    real_which = gitrepo.shutil.which
    monkeypatch.setattr(gitrepo.shutil, "which", lambda name: None if name == "git-lfs" else real_which(name))
    res = run("offload", "P1", "--remote", remote, "--yes", "--path", archive)
    assert res.exit_code == 1
    assert "git-lfs is not installed" in res.stderr
    assert rclone_local == []
    code, (entry,), err = push_json(archive, remote)
    assert code == 0
    assert repos_of(entry)[0]["lfs"] == "missing"
    assert "git-lfs is not installed" in err


# --- verify ---------------------------------------------------------------------------------


def test_verify_notices_a_partial_configuration(archive: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GWARCHIVE_GITEA_OWNER", OWNER)
    res = run("verify", "--json", "--path", archive)
    assert res.exit_code == 0
    issues = json.loads(res.stdout)["issues"]
    assert any("GWARCHIVE_GITEA_OWNER set" in i["message"] for i in issues)


def test_verify_flags_a_copied_top_level_repository(archive: Path) -> None:
    run("create", "P", "Beta", "--path", archive)
    beta = archive / "Project" / "P0001 Beta"
    make_repo(beta)
    git(beta, "config", "gwarchive.git.repo", "lab/p0007")
    res = run("verify", "--json", "--path", archive)
    assert res.exit_code == 1
    assert "copied by cp" in res.stdout
    git(beta, "config", "gwarchive.git.repo", "lab/p0001")
    assert run("verify", "--path", archive).exit_code == 0


def test_restore_names_the_server_copies(
    archive: Path, alpha: Path, server: Server, remote: Path, gzip_codec: None
) -> None:
    assert run("offload", "P1", "--remote", remote, "--yes", "--path", archive).exit_code == 0
    res = run("restore", "P1", "--yes", "--path", archive)
    assert res.exit_code == 0
    assert "Also on https://git.example.org as of" in res.stdout
    assert "lab/p0001--src--tool" in res.stdout


# --- naming -------------------------------------------------------------------------------


def by_path(entry: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(r["path"]): r for r in repos_of(entry)}


def test_a_name_past_the_limit_is_cut_and_hashed(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    make_repo(alpha / ("x" * 120))
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    name = str(by_path(entry)["x" * 120]["repo"]).removeprefix("lab/")
    assert len(name) <= 100
    assert name.startswith("p0001--xxx") and name[-9] == "-" and int(name[-8:], 16) >= 0
    assert name in server.repos


def test_two_paths_with_one_computed_name_get_two_repositories(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    make_repo(alpha / "a" / "b")
    make_repo(alpha / "a--b")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    names = {str(by_path(entry)[p]["repo"]) for p in ("a/b", "a--b")}
    assert len(names) == 2
    assert "lab/p0001--a--b" in names
    assert all(n.removeprefix("lab/") in server.repos for n in names)


def test_a_name_taken_by_a_foreign_repository_is_hashed(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    server.repos["p0001--src--tool"] = {"name": "p0001--src--tool", "topics": [], "description": "theirs"}
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    name = str(by_path(entry)["src/tool"]["repo"])
    assert name.startswith("lab/p0001--src--tool-") and name != "lab/p0001--src--tool"
    assert server.repos["p0001--src--tool"]["description"] == "theirs"


def test_moving_a_repository_inside_the_folder_keeps_its_name_and_updates_the_description(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    assert push_json(archive, remote)[0] == 0
    (alpha / "lib").mkdir()
    (alpha / "src" / "tool").rename(alpha / "lib" / "tool")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    assert by_path(entry)["lib/tool"]["repo"] == "lab/p0001--src--tool"
    assert str(server.repos["p0001--src--tool"]["description"]).endswith(" / lib/tool")
    assert set(server.repos) == {"p0001--src--tool"}


def test_renaming_the_folder_keeps_every_name(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    assert push_json(archive, remote)[0] == 0
    beta = alpha.with_name("P0001 Beta")
    alpha.rename(beta)
    code, (entry,), err = push_json(archive, remote)
    assert code == 0, err
    assert by_path(entry)["src/tool"]["repo"] == "lab/p0001--src--tool"
    assert set(server.repos) == {"p0001--src--tool"}
    assert server.repos["p0001--src--tool"]["description"] == "gwarchive: P0001 Beta / src/tool"


def test_two_repositories_with_one_stored_name_are_both_errors(
    archive: Path, alpha: Path, server: Server, remote: Path, rclone_local: list[list[str]]
) -> None:
    import shutil

    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    first = commit(tool, "after")
    shutil.copytree(tool, alpha / "src" / "copy")
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    repos = by_path(entry)
    assert repos["src/tool"]["error"] and repos["src/copy"]["error"]
    assert "share the stored name lab/p0001--src--tool" in err
    assert server.refs("p0001--src--tool")["refs/heads/main"] != first
    res = run("offload", "P1", "--remote", remote, "--yes", "--path", archive)
    assert res.exit_code == 1
    assert "config --unset gwarchive.git.repo" in res.stderr
    assert (tool / "README").exists()
    git(alpha / "src" / "copy", "config", "--unset", "gwarchive.git.repo")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    assert by_path(entry)["src/copy"]["repo"] == "lab/p0001--src--copy"
    assert server.refs("p0001--src--tool")["refs/heads/main"] == first


def test_a_stored_name_from_another_prefix_refuses_only_that_repository(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    other = make_repo(alpha / "other")
    git(other, "config", "gwarchive.git.repo", "lab/p0009--other")
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    assert "belongs to another folder's prefix" in err
    assert by_path(entry)["src/tool"]["error"] is None
    assert "p0001--src--tool" in server.repos and "p0009--other" not in server.repos


@pytest.mark.parametrize("root_repo", [False, True], ids=["plain", "into-a-repository"])
def test_a_folder_moved_inside_another_keeps_its_repository(
    archive: Path, alpha: Path, server: Server, remote: Path, root_repo: bool
) -> None:
    """``mv P0002 P0001``: the stored name keeps P0002's prefix, and an ancestor still has it.

    Its recorded path ``.`` now names P0001's own root, which always exists; when
    that root is a repository too, its stored name is P0001's, so it is no copy.
    """
    if root_repo:
        make_repo(alpha)
    assert run("create", "P", "Beta", "--path", archive).exit_code == 0
    beta = make_repo(archive / "Project" / "P0002 Beta")
    assert run("push", "P", "--remote", remote, "--path", archive).exit_code == 0
    assert git(beta, "config", "gwarchive.git.repo") == "lab/p0002"
    first = commit(beta, "before the move")
    assert run("mv", "P2", "P1", "--path", archive).exit_code == 0
    moved = alpha / "P0002 Beta"
    assert moved.is_dir()
    later = commit(moved, "after the move")
    code, (entry,), err = push_json(archive, remote)
    assert code == 0, err
    repo = by_path(entry)["P0002 Beta"]
    assert (repo["repo"], repo["error"], repo["created"]) == ("lab/p0002", None, False)
    assert server.refs("p0002")["refs/heads/main"] == later != first
    assert server.repos["p0002"]["description"] == "gwarchive: P0001 Alpha [draft] / P0002 Beta"
    assert set(server.repos) == {"p0001--src--tool", "p0002"} | ({"p0001"} if root_repo else set())
    assert run("verify", "--path", archive).exit_code == 0


def test_a_redirected_name_is_an_error_naming_the_new_one(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert push_json(archive, remote)[0] == 0
    real = server.request

    def renamed(method: str, url: str, **kwargs: object) -> forge.Reply:
        if method == "GET" and url.endswith("/repos/lab/p0001--src--tool"):
            return forge.Reply(301, None, "https://git.example.org/api/v1/repos/lab/renamed")
        return real(method, url, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(forge, "request", renamed)
    code, _, err = push_json(archive, remote)
    assert code == 1
    assert "was renamed on the server to lab/renamed" in err
    assert "config gwarchive.git.repo lab/renamed" in err


# --- skipping, continued ------------------------------------------------------------------


def test_a_stored_name_is_pushed_even_with_a_remote_on_the_server(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    git(tool, "remote", "add", "copy", "https://git.example.org/lab/p0001--src--tool.git")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    assert by_path(entry)["src/tool"]["skipped"] is None


def test_worktrees_are_skipped_inside_and_outside_the_folder(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path
) -> None:
    tool = alpha / "src" / "tool"
    git(tool, "worktree", "add", "-q", str(alpha / "wt"))
    outside = make_repo(tmp_path / "outside")
    git(outside, "worktree", "add", "-q", str(alpha / "wt2"))
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    repos = by_path(entry)
    assert repos["wt"]["skipped"] == "worktree of src/tool"
    assert repos["wt2"]["skipped"] == "worktree whose repository is outside this folder"
    assert set(server.repos) == {"p0001--src--tool"}


def test_skipped_repositories_do_not_block_offload(
    archive: Path, alpha: Path, server: Server, remote: Path, gzip_codec: None
) -> None:
    git(make_repo(alpha / "skipped"), "config", "gwarchive.git.skip", "true")
    res = run("offload", "P1", "--remote", remote, "--yes", "--json", "--path", archive)
    assert res.exit_code == 0, res.stderr
    assert json.loads(res.stdout)[0]["offloaded"] is True


# --- refs, continued ----------------------------------------------------------------------


def test_a_diverged_branch_is_refused_and_named(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    theirs = _diverge(server, tool, tmp_path)
    git(tool, "fetch", "-q", str(server.bare("p0001--src--tool")), "main")
    commit(tool, "mine")
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    (ref,) = [r for r in refs_of(by_path(entry)["src/tool"]) if r["ref"] == "refs/heads/main"]
    assert (ref["state"], ref["action"]) == ("diverged", "refused")
    assert "diverged main" in err and " fetch " not in err
    assert server.refs("p0001--src--tool")["refs/heads/main"] == theirs


def test_a_moved_tag_is_refused(archive: Path, alpha: Path, server: Server, remote: Path) -> None:
    tool = alpha / "src" / "tool"
    git(tool, "tag", "v1")
    assert push_json(archive, remote)[0] == 0
    old = server.refs("p0001--src--tool")["refs/tags/v1"]
    commit(tool, "later")
    git(tool, "tag", "-f", "v1")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 1
    (ref,) = [r for r in refs_of(by_path(entry)["src/tool"]) if r["ref"] == "refs/tags/v1"]
    assert (ref["state"], ref["action"]) == ("tag moved", "refused")
    assert server.refs("p0001--src--tool")["refs/tags/v1"] == old


def test_a_branch_deleted_locally_stays_on_the_server(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    tool = alpha / "src" / "tool"
    git(tool, "branch", "feature")
    assert push_json(archive, remote)[0] == 0
    git(tool, "branch", "-D", "feature")
    code, (entry,), _ = push_json(archive, remote)
    assert code == 0
    (ref,) = [r for r in refs_of(by_path(entry)["src/tool"]) if r["ref"] == "refs/heads/feature"]
    assert ref["state"] == "server-only"
    assert "refs/heads/feature" in server.refs("p0001--src--tool")


def test_force_git_with_nothing_refused_never_prompts(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    res = run("push", "P1", "--remote", remote, "--force-git", "--path", archive, input="")
    assert res.exit_code == 0, res.stderr
    assert "?" not in res.stdout


def test_https_auth_failure_suggests_ssh_through_gwarchive_gitea_git(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = external.run_git

    def denied(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: Authentication failed for 'https://…'")
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", denied)
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    assert "export GWARCHIVE_GITEA_GIT='ssh://" in err
    assert entry["remotes"]


# --- --wip, continued ---------------------------------------------------------------------


def test_wip_works_from_a_detached_head(archive: Path, alpha: Path, server: Server, remote: Path) -> None:
    tool = alpha / "src" / "tool"
    git(tool, "checkout", "-q", "--detach")
    (tool / "README").write_text("dirty\n")
    code, (entry,), _ = push_json(archive, remote, "--wip")
    assert code == 0
    wip = str(by_path(entry)["src/tool"]["wip"])
    sha = server.refs("p0001--src--tool")[f"refs/heads/{wip}"]
    assert git(server.bare("p0001--src--tool"), "show", f"{sha}:README") == "dirty"


def test_wip_leaves_another_hosts_wip_branch_alone(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    other = tmp_path / "elsewhere"
    git(tmp_path, "clone", "-q", str(server.bare("p0001--src--tool")), str(other))
    theirs = commit(other, "their wip")
    git(other, "push", "-q", "origin", "HEAD:refs/heads/wip/other-host")
    (tool / "README").write_text("dirty\n")
    code, (entry,), _ = push_json(archive, remote, "--wip")
    assert code == 0
    assert server.refs("p0001--src--tool")["refs/heads/wip/other-host"] == theirs


def test_the_wip_index_is_removed_when_commit_tree_fails(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    tool = alpha / "src" / "tool"
    (tool / "README").write_text("dirty\n")
    real = external.run_git
    seen: list[bool] = []

    def failing_commit_tree(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "commit-tree" in args:
            seen.append(any(scratch in Path(p).parents for p in scratch.glob("gwarchive-index-*/index")))
            return subprocess.CompletedProcess(args, 128, "", "fatal: nope")
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", failing_commit_tree)
    code, _, err = push_json(archive, remote, "--wip")
    assert code == 1
    assert seen == [True]
    assert "could not build the --wip commit" in err
    assert list(scratch.iterdir()) == []
    assert [p.name for p in (tool / ".git").glob("*index*")] == ["index"]


# --- output, continued --------------------------------------------------------------------


def test_refused_lines_survive_quiet(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    _diverge(server, tool, tmp_path)
    commit(tool, "mine")
    res = run("--quiet", "push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 1
    assert "unknown main" in res.stderr
    assert "try:" in res.stderr


def test_a_batch_ends_with_one_summary_line(archive: Path, alpha: Path, server: Server, remote: Path) -> None:
    assert run("create", "P", "Beta", "--path", archive).exit_code == 0
    make_repo(archive / "Project" / "P0002 Beta")
    res = run("push", "P", "--remote", remote, "--path", archive)
    assert res.exit_code == 0, res.stderr
    assert "git: 2 repositories in 2 folders: 2 pushed, 2 created" in res.stdout
    assert res.stdout.count("git: 2 repositories") == 1


def test_push_on_offloaded_leftovers_dies_before_any_git_write(
    archive: Path, alpha: Path, server: Server, remote: Path, gzip_codec: None
) -> None:
    assert run("offload", "P1", "--remote", remote, "--yes", "--path", archive).exit_code == 0
    make_repo(alpha / "late")
    server.calls.clear()
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 1
    assert server.writes() == []
    assert "p0001--late" not in server.repos
    stored = subprocess.run(["git", "config", "--get", "gwarchive.git.repo"], cwd=alpha / "late", check=False)
    assert stored.returncode == 1


# --- --wip, the rest ----------------------------------------------------------------------


def test_untracked_files_are_capped_at_10_in_the_receipt_and_complete_in_json(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    tool = alpha / "src" / "tool"
    names = [f"new{i:02}.txt" for i in range(12)]
    for name in names:
        (tool / name).write_text("x\n")
    code, (entry,), _ = push_json(archive, remote, "--dry-run", "--wip")
    assert code == 0
    assert by_path(entry)["src/tool"]["untracked_included"] == names
    res = run("push", "P1", "--remote", remote, "--wip", "--path", archive)
    assert res.exit_code == 0, res.stderr
    assert "new09.txt" in res.stdout and "new10.txt" not in res.stdout
    assert "(+2 more)" in res.stdout


def test_a_second_wip_run_replaces_the_branch_without_a_prompt(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
    tool = alpha / "src" / "tool"
    (tool / "README").write_text("first\n")
    code, (entry,), _ = push_json(archive, remote, "--wip")
    assert code == 0
    ref = f"refs/heads/{by_path(entry)['src/tool']['wip']}"
    first = server.refs("p0001--src--tool")[ref]
    (tool / "README").write_text("second\n")
    res = run("push", "P1", "--remote", remote, "--wip", "--path", archive, input="")
    assert res.exit_code == 0, res.stderr
    second = server.refs("p0001--src--tool")[ref]
    assert second != first
    assert git(server.bare("p0001--src--tool"), "show", f"{second}:README") == "second"


def test_without_wip_a_dirty_repository_is_named_in_the_offload_log(
    archive: Path, alpha: Path, server: Server, remote: Path, gzip_codec: None
) -> None:
    (alpha / "src" / "tool" / "README").write_text("dirty\n")
    res = run("offload", "P1", "--remote", remote, "--yes", "--path", archive)
    assert res.exit_code == 0, res.stderr
    assert "has uncommitted changes" in res.stderr and "add --wip" in res.stderr


# --- timeouts and ssh ---------------------------------------------------------------------


def spy_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, object]]]:
    calls: list[tuple[list[str], dict[str, object]]] = []
    real = external.run_git

    def spy(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), kwargs))
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(external, "run_git", spy)
    return calls


def test_only_network_commands_are_bounded_and_pushes_get_a_stall_limit(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GWARCHIVE_GITEA_TIMEOUT", "7")
    calls = spy_git(monkeypatch)
    assert push_json(archive, remote)[0] == 0
    for args, kwargs in calls:
        if "ls-remote" in args:
            assert kwargs.get("timeout") == 7
        else:
            assert kwargs.get("timeout") is None, args
    (push,) = [args for args, _ in calls if "push" in args]
    assert "http.lowSpeedTime=7" in push


def test_an_unattended_run_never_waits_for_an_ssh_prompt(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = spy_git(monkeypatch)
    assert push_json(archive, remote)[0] == 0
    network = [kw for args, kw in calls if "ls-remote" in args or "push" in args]
    assert network
    for kwargs in network:
        env = kwargs.get("env")
        assert isinstance(env, dict) and "-o BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_a_users_own_ssh_command_is_left_alone(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i ~/.ssh/archive")
    calls = spy_git(monkeypatch)
    assert push_json(archive, remote)[0] == 0
    assert all(not kw.get("env") for args, kw in calls if "ls-remote" in args or "push" in args)


# --- soft failures, continued -------------------------------------------------------------


def test_an_api_timeout_on_one_repository_leaves_the_others_pushed(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_repo(alpha / "lib")
    real = server.request

    def slow(method: str, url: str, **kwargs: object) -> forge.Reply:
        if url.endswith("/repos/lab/p0001--lib"):
            raise forge.ForgeError("the git server did not answer within 60 seconds")
        return real(method, url, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(forge, "request", slow)
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    assert "did not answer within 60 seconds" in err
    assert by_path(entry)["lib"]["error"]
    assert by_path(entry)["src/tool"]["error"] is None
    assert "refs/heads/main" in server.refs("p0001--src--tool")
    assert entry["remotes"]


def test_a_ref_missing_on_read_back_is_an_error(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert push_json(archive, remote)[0] == 0
    commit(alpha / "src" / "tool", "later")
    real = external.run_git
    pushed: list[bool] = []

    def forgetful(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = real(args, **kwargs)  # type: ignore[arg-type]
        if "push" in args:
            pushed.append(True)
        elif "ls-remote" in args and pushed:
            # The read-back: the server reports main as never updated.
            kept = [line for line in proc.stdout.splitlines() if not line.endswith("refs/heads/main")]
            return subprocess.CompletedProcess(args, 0, "\n".join(kept), "")
        return proc

    monkeypatch.setattr(external, "run_git", forgetful)
    code, (entry,), err = push_json(archive, remote)
    assert code == 1
    assert pushed
    assert "read-back: the server does not hold what was pushed for refs/heads/main" in err
    assert by_path(entry)["src/tool"]["error"]


def test_no_git_and_no_server_both_keep_the_previous_record(
    archive: Path, alpha: Path, server: Server, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert push_json(archive, remote)[0] == 0
    previous = (tombstone.read_tombstone(alpha) or {})["git"]
    assert push_json(archive, remote, "--no-git")[0] == 0
    assert (tombstone.read_tombstone(alpha) or {})["git"] == previous
    monkeypatch.delenv("GWARCHIVE_GITEA_URL")
    assert push_json(archive, remote)[0] == 0
    assert (tombstone.read_tombstone(alpha) or {})["git"] == previous


def test_an_offloaded_folder_is_skipped_by_push_without_a_git_call(
    archive: Path,
    alpha: Path,
    server: Server,
    remote: Path,
    gzip_codec: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run("offload", "P1", "--remote", remote, "--yes", "--path", archive).exit_code == 0
    calls = spy_git(monkeypatch)
    server.calls.clear()
    res = run("push", "P1", "--remote", remote, "--path", archive)
    assert res.exit_code == 0, res.stderr
    assert [args for args, _ in calls if "ls-remote" in args or "push" in args] == []
    assert server.writes() == []


# --- offload, continued -------------------------------------------------------------------


def test_offload_force_git_yes_logs_every_overwrite(
    archive: Path, alpha: Path, server: Server, remote: Path, tmp_path: Path, gzip_codec: None
) -> None:
    tool = alpha / "src" / "tool"
    assert push_json(archive, remote)[0] == 0
    _diverge(server, tool, tmp_path)
    mine = commit(tool, "mine")
    res = run("offload", "P1", "--remote", remote, "--force-git", "--yes", "--path", archive)
    assert res.exit_code == 0, res.stderr
    assert "overwrites lab/p0001--src--tool: main (unknown: never fetched)" in res.stderr
    assert server.refs("p0001--src--tool")["refs/heads/main"] == mine


def test_offload_refusal_after_a_run_wide_error_offers_no_git(
    archive: Path, alpha: Path, server: Server, remote: Path, rclone_local: list[list[str]]
) -> None:
    server.down = True
    res = run("offload", "P1", "--remote", remote, "--yes", "--path", archive)
    assert res.exit_code == 1
    assert "nothing was transferred" in res.stderr
    assert "--no-git" in res.stderr
    assert rclone_local == []
    assert (alpha / "src" / "tool" / "README").exists()
