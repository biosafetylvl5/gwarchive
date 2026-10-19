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
    from gwarchive import gitrepo

    tool = alpha / "src" / "tool"
    first = git(tool, "rev-parse", "HEAD")
    real = gitrepo.preflight

    def then_commit(*args: object, **kwargs: object) -> gitrepo.GitRun:
        out = real(*args, **kwargs)  # type: ignore[arg-type]
        commit(tool, "late")
        return out

    monkeypatch.setattr(gitrepo, "preflight", then_commit)
    assert push_json(archive, remote)[0] == 0
    assert server.refs("p0001--src--tool")["refs/heads/main"] == first


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


def test_skip_config_submodules_and_already_there(
    archive: Path, alpha: Path, server: Server, remote: Path
) -> None:
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
