"""The git server: configuration, and the Gitea (or Forgejo) API.

Layer 2. ``request`` is the one function that speaks HTTP, and it is a SEAM:
callers write ``forge.request(...)``, never a from-import, so the test double
reaches it. Everything above it builds on that one call.

The token comes from ``$GWARCHIVE_GITEA_TOKEN_CMD`` and is never accepted as
an option or an environment value, so it stays out of argv, shell history and
``--json``. It is only ever sent as an ``Authorization`` header, never to git.

Redirects are never followed. Gitea redirects a repository renamed on the
server, and following one would push into whatever holds that name now.
"""

import json
import os
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from gwarchive.output import die, program

DEFAULT_TIMEOUT = 60
TOPIC = "gwarchive"

EXAMPLE = """\
export GWARCHIVE_GITEA_URL=https://git.example.org
export GWARCHIVE_GITEA_OWNER=lab
export GWARCHIVE_GITEA_TOKEN_CMD='security find-generic-password -s g-gitea -w'
# optional:
export GWARCHIVE_GITEA_GIT='ssh://git@git.example.org:2222/{owner}/{repo}.git'
export GWARCHIVE_GITEA_TIMEOUT=60"""


class ForgeError(Exception):
    """The server could not be reached, or answered with something unusable."""


@dataclass(frozen=True)
class GitConfig:
    url: str
    git: str
    owner: str
    token_cmd: str
    timeout: int

    def push_url(self, repo: str) -> str:
        return self.git.replace("{owner}", self.owner).replace("{repo}", repo)

    def hosts(self) -> set[str]:
        """The API host and the push URL's host: they often differ."""
        found = {h for h in (url_host(self.url), url_host(self.push_url("x"))) if h}
        return found


@dataclass(frozen=True)
class Reply:
    status: int
    data: object
    location: str = ""


def url_host(url: str) -> str:
    """The host of an http(s)/ssh/file URL or of scp-like ``git@host:path``."""
    text = url.strip()
    if "://" in text:
        return (urllib.parse.urlsplit(text).hostname or "").lower()
    head, colon, _ = text.partition(":")
    if colon and "/" not in head:
        return head.rpartition("@")[2].lower()
    return ""


def configured() -> bool:
    return bool(os.environ.get("GWARCHIVE_GITEA_URL", "").strip())


def partially_configured() -> list[str]:
    """GWARCHIVE_GITEA_* variables that are set while the URL is not."""
    if configured():
        return []
    return sorted(n for n in os.environ if n.startswith("GWARCHIVE_GITEA_") and os.environ[n].strip())


def load_config() -> GitConfig | None:
    """Every variable, checked in one pass. None when no server is configured.

    Every problem goes into one exit-2 message with a block to paste, so a
    user setting this up sees every missing piece at once. A missing ``git``
    is exit 1, worded like a missing rclone.
    """
    env = {k: v.strip() for k, v in os.environ.items() if k.startswith("GWARCHIVE_GITEA_")}
    url = env.get("GWARCHIVE_GITEA_URL", "")
    if not url:
        return None
    problems: list[str] = []
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https") or not urllib.parse.urlsplit(url).hostname:
        problems.append(f"GWARCHIVE_GITEA_URL is not an http(s) URL: {url!r}")
    owner = env.get("GWARCHIVE_GITEA_OWNER", "")
    if not owner:
        problems.append("GWARCHIVE_GITEA_OWNER is not set")
    token_cmd = env.get("GWARCHIVE_GITEA_TOKEN_CMD", "")
    if not token_cmd:
        problems.append("GWARCHIVE_GITEA_TOKEN_CMD is not set")
    template = env.get("GWARCHIVE_GITEA_GIT", "")
    if template and not ("{owner}" in template and "{repo}" in template):
        problems.append("GWARCHIVE_GITEA_GIT must contain {owner} and {repo}")
    raw_timeout = env.get("GWARCHIVE_GITEA_TIMEOUT", "")
    timeout = DEFAULT_TIMEOUT
    if raw_timeout:
        if raw_timeout.isascii() and raw_timeout.isdecimal() and int(raw_timeout) > 0:
            timeout = int(raw_timeout)
        else:
            problems.append(f"GWARCHIVE_GITEA_TIMEOUT is not a positive whole number: {raw_timeout!r}")
    if problems:
        raise die(
            "The git server is not configured correctly:\n"
            + "\n".join(f"  {p}" for p in problems)
            + "\n\nA complete configuration:\n"
            + EXAMPLE,
            code=2,
            fix=f"{program()} push P --no-git   # the rclone copy alone",
        )
    if shutil.which("git") is None:
        raise die("git is not on your PATH.", fix=f"{program()} push P --no-git   # the rclone copy alone")
    base = url.rstrip("/")
    return GitConfig(
        url=base,
        git=template or f"{base}/{{owner}}/{{repo}}.git",
        owner=owner,
        token_cmd=token_cmd,
        timeout=timeout,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def request(method: str, url: str, *, token: str, body: object | None = None, timeout: float) -> Reply:
    """One HTTP call. Raises ForgeError when there is no usable answer.

    Any HTTP status, redirects and errors included, is an answer and comes
    back as a Reply; only an unreachable server, a timeout or a TLS failure
    raises.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Reply(resp.status, _decode(resp.read()), resp.headers.get("Location", ""))
    except urllib.error.HTTPError as exc:
        return Reply(exc.code, _decode(exc.read()), exc.headers.get("Location", "") if exc.headers else "")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            raise ForgeError(
                f"TLS failure talking to the git server: {reason}. "
                "Set SSL_CERT_FILE to a CA bundle if the server uses a private CA."
            ) from exc
        raise ForgeError(f"cannot reach the git server: {reason}") from exc
    except TimeoutError as exc:
        raise ForgeError(f"the git server did not answer within {timeout:g} seconds") from exc
    except OSError as exc:
        raise ForgeError(f"cannot reach the git server: {exc}") from exc


def _decode(raw: bytes) -> object:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _message(reply: Reply) -> str:
    if isinstance(reply.data, dict) and isinstance(reply.data.get("message"), str):
        return f"HTTP {reply.status}: {reply.data['message']}"
    return f"HTTP {reply.status}"


class Forge:
    """The configured server, as this run sees it, after a successful probe."""

    def __init__(self, cfg: GitConfig, token: str) -> None:
        self.cfg = cfg
        self.token = token
        self.is_org = True

    def call(self, method: str, path: str, body: object | None = None) -> Reply:
        return request(
            method, f"{self.cfg.url}/api/v1{path}", token=self.token, body=body, timeout=self.cfg.timeout
        )

    def probe(self) -> None:
        """Resolve the owner. Doubles as the probe: nothing else runs until it passes.

        ForgeError for anything run-wide (unreachable, 401/403, TLS); exit 2
        when the server answers but knows no such owner.
        """
        owner = urllib.parse.quote(self.cfg.owner, safe="")
        reply = self.call("GET", f"/orgs/{owner}")
        if reply.status == 200:
            self.is_org = True
            return
        if reply.status in (401, 403):
            raise ForgeError(f"the git server refused the token ({_message(reply)})")
        if reply.status != 404:
            raise ForgeError(f"the git server answered the probe with {_message(reply)}")
        user = self.call("GET", "/user")
        if user.status in (401, 403):
            raise ForgeError(f"the git server refused the token ({_message(user)})")
        login = user.data.get("login") if isinstance(user.data, dict) else None
        if user.status == 200 and isinstance(login, str) and login.lower() == self.cfg.owner.lower():
            self.is_org = False
            return
        if user.status != 200:
            raise ForgeError(f"the git server answered the probe with {_message(user)}")
        raise die(
            f"The git server knows no organization {self.cfg.owner!r}, and the token belongs to "
            f"{login!r}, not to that user.",
            code=2,
            fix=f"export GWARCHIVE_GITEA_OWNER={login}",
        )

    def _repo_path(self, name: str) -> str:
        return f"/repos/{urllib.parse.quote(self.cfg.owner, safe='')}/{urllib.parse.quote(name, safe='')}"

    def get(self, name: str) -> Reply:
        return self.call("GET", self._repo_path(name))

    def create(self, name: str, description: str) -> None:
        path = f"/orgs/{urllib.parse.quote(self.cfg.owner, safe='')}/repos" if self.is_org else "/user/repos"
        # The API defaults to public; every copy starts private, and g never
        # changes visibility back once the user has.
        reply = self.call("POST", path, {"name": name, "description": description, "private": True})
        if reply.status not in (200, 201):
            raise ForgeError(f"could not create {self.cfg.owner}/{name}: {_message(reply)}")
        topics = self.call("PUT", f"{self._repo_path(name)}/topics", {"topics": [TOPIC]})
        if topics.status not in (200, 204):
            raise ForgeError(f"could not tag {self.cfg.owner}/{name}: {_message(topics)}")

    def edit(self, name: str, **fields: object) -> None:
        reply = self.call("PATCH", self._repo_path(name), fields)
        if reply.status != 200:
            raise ForgeError(f"could not update {self.cfg.owner}/{name}: {_message(reply)}")
