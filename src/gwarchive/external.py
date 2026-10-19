"""Everything that leaves this process: rclone, zstd, pokeget, git, and remote targets.

One monkeypatchable function per external tool, and `grep -c subprocess` over
this package equals 1. That is the invariant -- three files would be three
chances to spawn a process somewhere else.

This is a SEAM module (with `clock` and `tarball`). Callers import the MODULE
and write `external.run_rclone(...)`; a from-import binds the real function at
import time and the test double then has nothing to reach. Calls WITHIN this
module are exempt and need no change: `rclone_or_die` resolves `run_rclone`
through this module's own globals at call time, so a patch is picked up no
matter who called it.

Remote-target validation lives here too, next to the binary it protects you
from. rclone honours a colon only in the FIRST path segment, so `unraid` and
`backup/nas:` are ordinary local directories to it -- `push --remote unraid`
once copied 48 MB into ./unraid/ beside the shell's cwd and still printed a
green "Pushed".
"""

import os
import shlex
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from gwarchive.naming import TOMBSTONE_NAME
from gwarchive.output import die, program, spinner

# What the per-file rclone mirror must leave behind, so it carries the same set
# as the tar member filter. Without the second pattern a --no-compress push
# uploaded the .gwarchive-* namespace that the compressed path never packs.
RCLONE_EXCLUDES = ["--exclude", TOMBSTONE_NAME, "--exclude", ".gwarchive-*"]


def is_remote_target(value: str) -> bool:
    """True when rclone reads this as remote storage rather than as a directory.

    rclone honours a colon only in the first path segment: ``nas:archive``, a
    bare ``nas:`` and the ``:sftp,host=x:`` connection-string form are remotes,
    while ``backup/nas:`` and ``~/nas:`` are ordinary directory names -- rclone
    does not expand ``~`` either. An absolute path is a remote target for our
    purposes, because ``/Volumes/Backup`` is a local target somebody meant.
    """
    stripped = value.strip()
    head, colon, _ = stripped.partition(":")
    return stripped.startswith("/") or bool(colon and "/" not in head)


def validate_remote_target(target: str, source: str = "--remote", command: str = "push") -> str:
    """Refuse a remote that rclone would quietly read as a local directory.

    Without a colon in its first segment ``unraid`` is an ordinary relative
    directory name, so a push lands in ./unraid/ beside wherever g was run and
    still reports success. That has happened; hence this guard.
    """
    value = target.strip()
    if is_remote_target(value):
        return value
    suggestion = f"{program()} {command} P1 --remote {value}:" if value and "/" not in value else None
    raise die(
        f"{value or '(empty)'} is not a remote target ({source}).\n"
        "rclone only reads an argument as a remote when a colon comes before the first slash,"
        " so this would read or write a local directory instead of remote storage.",
        code=2,
        fix=suggestion or f"{program()} {command} P1 --remote nas:archive",
    )


def get_remotes(remotes: Sequence[str] | None = None, command: str = "push") -> list[str]:
    """Resolve remote target(s) from CLI options or $GWARCHIVE_REMOTE."""
    if remotes:
        return [validate_remote_target(r, command=command) for r in remotes]
    env_remote = os.environ.get("GWARCHIVE_REMOTE")
    if env_remote and env_remote.strip():
        return [validate_remote_target(env_remote, "$GWARCHIVE_REMOTE", command)]
    raise die(
        "No remote specified.\nPass --remote, or set $GWARCHIVE_REMOTE once in your shell profile.",
        code=2,
        fix=f"{program()} {command} P1 --remote nas:archive",
    )


def remote_root_of(target: str) -> str:
    """The rclone remote name a full target path lives on, e.g. ``nas:``."""
    return target.split(":", 1)[0] + ":" if ":" in target else target


def remote_folder_target(remote_root: str, category: str, folder_name: str) -> str:
    clean_root = remote_root.rstrip("/")
    sep = "" if clean_root.endswith(":") else "/"
    return f"{clean_root}{sep}{category}/{folder_name}"


def run_rclone(
    args: Sequence[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """The single boundary between g and rclone.

    One monkeypatchable function per external tool, so tests can assert args
    without the binary; ``run_zstd`` and ``run_pokeget`` are the other two.
    """
    cmd = ["rclone", *args]
    try:
        return subprocess.run(
            cmd,
            text=True,
            capture_output=capture_output,
            check=False,
        )
    except FileNotFoundError as exc:
        raise die(
            "rclone is not on your PATH.",
            fix="brew install rclone   # or see https://rclone.org/install/",
        ) from exc


def rclone_tail(proc: subprocess.CompletedProcess[str], limit: int = 4) -> str:
    """The last few things rclone actually said, or failing that its exit code."""
    raw = (proc.stderr or proc.stdout or "").strip()
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return f"rclone exited with code {proc.returncode}"
    return "\n".join(lines[-limit:])


def rclone_or_die(
    args: Sequence[str],
    description: str,
    *,
    capture_output: bool = False,
    status: str | None = None,
    fix: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run rclone and fail with an informative message if nonzero.

    ``status`` draws a spinner for the duration, but only on a real terminal --
    piped output and --json stay byte-identical to a run without one.
    """
    with spinner(status):
        proc = run_rclone(args, capture_output=capture_output)
    if proc.returncode != 0:
        raise die(f"Could not {description}:\n{rclone_tail(proc)}", fix=fix)
    return proc


def run_zstd(
    args: Sequence[str],
    *,
    stdin: int | None = None,
    stdout: int | None = None,
) -> subprocess.Popen[bytes]:
    """The boundary to zstd. A ``Popen``, because the archive is piped
    through it and never buffered whole."""
    cmd = ["zstd", *args]
    try:
        return subprocess.Popen(cmd, stdin=stdin, stdout=stdout)
    except FileNotFoundError as exc:
        raise die(
            "zstd is not on your PATH.",
            fix="brew install zstd   # or GWARCHIVE_CODEC=gzip to use the stdlib codec",
        ) from exc


# Passed to every git command, so nothing writes into a .git outside the plan's
# control: a detached auto-gc still repacking while rclone copies .git (and
# after offload's manifest was taken), an fsmonitor daemon writing cookies for
# as long as it lives, a user hook run by `add -A` against a temporary index.
GIT_SAFETY = [
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
]


def _run_bounded(
    cmd: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str],
    timeout: float | None,
    input_text: str | None,
) -> subprocess.CompletedProcess[str]:
    """Run cmd in its own process group; on a timeout, kill the whole group.

    ``subprocess.run(timeout=)`` kills only the direct child. git's ssh,
    git-remote-https and credential helpers are grandchildren holding the
    captured pipes open, so ``run`` then blocks in its second ``communicate()``
    on exactly the stalled connection the timeout exists for.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        return subprocess.CompletedProcess(
            cmd, -9, out or "", f"{cmd[0]} timed out after {timeout:g} seconds"
        )
    return subprocess.CompletedProcess(cmd, proc.returncode, out or "", err or "")


def run_git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """The boundary to git. Never raises for git's own failures.

    ``timeout`` is a total limit, for commands that talk to a server and have
    no business taking long (``ls-remote``). Pushes pass None and carry a
    stall limit in their own ``-c`` options instead; local commands pass None,
    since they cannot stall on a network and a total limit would only fail
    ``--wip`` on a large working tree. A timeout comes back as an ordinary
    failed result, so callers have one failure path.

    stdin is closed and prompts are off: an unattended run fails with a
    message rather than waiting for a password nobody will type.
    """
    full_env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        # Read-only commands such as `git status` otherwise refresh and
        # rewrite .git/index, which is a write into the user's repository.
        "GIT_OPTIONAL_LOCKS": "0",
        **(env or {}),
    }
    cmd = ["git", *GIT_SAFETY, *args]
    try:
        return _run_bounded(cmd, cwd=cwd, env=full_env, timeout=timeout, input_text=input_text)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "git is not on your PATH")


def git_tail(proc: subprocess.CompletedProcess[str], limit: int = 4) -> str:
    """The last few things git said, or failing that its exit code."""
    raw = (proc.stderr or proc.stdout or "").strip()
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return f"git exited with code {proc.returncode}"
    return "\n".join(lines[-limit:])


def read_gitea_token(command: str, timeout: float) -> str | None:
    """Run $GWARCHIVE_GITEA_TOKEN_CMD and return its stdout, or None.

    Split with shlex and run without a shell. Its output is never shown:
    on failure the caller names the variable, not what the command printed.
    Not a seam -- tests use a real ``printf``.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    try:
        proc = _run_bounded(argv, cwd=None, env=os.environ, timeout=timeout, input_text=None)
    except OSError:
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


def run_pokeget(args: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """The boundary to pokeget.

    None when it is not installed: the greeting runs at shell startup, so a
    missing binary has to degrade silently rather than die.
    """
    try:
        return subprocess.run(["pokeget", *args], text=True, check=False)
    except FileNotFoundError:
        return None
