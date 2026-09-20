"""verify: the structural audit, its issue collectors, and the two reports.

The six collect_* generators are independent and uniform (Path -> Iterator[Issue]).
collect_naming_issues deliberately does NOT use iter_archive_folders: that helper
filters to folders that already have a valid prefix, which is precisely what this
collector exists to report the absence of.

Findings and the summary go to STDOUT: they are this command's output the way
list's table is, and the exit code carries pass/fail. Under --quiet the exit
code is the whole report.

render_ and write_verify_report each derive their own counts, so they cannot
disagree."""

import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from gwarchive import clock, output
from gwarchive.destination import (
    run_fs,
)
from gwarchive.naming import (
    CATEGORIES,
    CATEGORY_NAMES,
    FOLDER_RE,
    OLD_RE,
    SUBFOLDER_RE,
    TOMBSTONE_NAME,
    _reserved_member,
    folder_prefix,
)
from gwarchive.options import (
    AsJsonReport,
    BasePath,
    app,
)
from gwarchive.output import (
    INDENT,
    STYLES,
    _decorate,
    confirm_destructive,
    console,
    emit_json,
    note,
    ok,
    plural,
)
from gwarchive.paths import (
    category_dirs,
    display,
    ensure_directory,
    iter_archive_folders,
)
from gwarchive.tarball import (
    ARCHIVE_FORMATS,
)
from gwarchive.tombstone import (
    read_tombstone_state,
)

ERROR = "error"
NOTICE = "notice"


# The remedy a finding names when the command can apply it itself.
FIX_HINT = "fixable with --fix"
QUARANTINE_HINT = "movable with --quarantine"


@dataclass
class Issue:
    """One verification finding.

    ``severity`` defaults to ERROR because nearly every finding is one, and the
    two notices say so explicitly. That default is what lets a collector yield
    a finding on one line instead of the seven the formatter needed when every
    call site had to spell ERROR out.

    ``target`` and ``action`` are how a finding tells --fix and --quarantine
    what to operate on, rather than having the path recovered by string-parsing
    ``message``. See AGENTS.md, "verify".
    """

    group: str
    message: str
    fix_hint: str = ""
    severity: str = ERROR
    resolution: str = ""
    target: Path | None = None
    action: str = ""

    def as_dict(self) -> dict[str, str]:
        """The --json and --report view: the text fields, never the Path.

        Spelled out rather than ``dataclasses.asdict``, which would put a
        PosixPath into ``emit_json`` and make json.dumps raise.
        """
        return {
            "severity": self.severity,
            "group": self.group,
            "message": self.message,
            "fix_hint": self.fix_hint,
            "resolution": self.resolution,
        }

    def line(self) -> Text:
        """One finding, symbol first, with its resolution or its remedy dimmed."""
        kind = "ok" if self.resolution else ("error" if self.severity == ERROR else "warn")
        body = Text(self.message)
        if self.resolution:
            body.append(f"  [{self.resolution}]", style=STYLES["caption"])
        elif self.fix_hint:
            body.append(f"  ({self.fix_hint})", style=STYLES["caption"])
        return _decorate(kind, body, indent=INDENT)


def collect_structure_issues(base_path: Path) -> Iterator[Issue]:
    """The archive root and its five category directories.

    No early return when the base is missing -- the categories are missing too,
    and reporting only the base made ``--fix`` exit 0 having created one
    directory. See AGENTS.md, "verify".
    """
    if not base_path.exists():
        yield Issue(
            "Structure",
            f"Base directory does not exist: {base_path}",
            FIX_HINT,
            target=base_path,
            action="create",
        )
    for category in CATEGORIES.values():
        cat_path = base_path / category
        if not cat_path.exists():
            yield Issue(
                "Structure",
                f"Missing category directory: {category}",
                FIX_HINT,
                target=cat_path,
                action="create",
            )


def collect_root_issues(base_path: Path) -> Iterator[Issue]:
    """Unrecognized entries at the archive root; the path rides on ``target``."""
    if not base_path.exists():
        return

    allowed_dirs = CATEGORY_NAMES | {"BACKUP"}
    allowed_files = {".gitignore", "README.md"}

    for item in sorted(base_path.iterdir(), key=lambda p: p.name):
        if item.name.startswith("."):
            continue
        is_dir = item.is_dir()
        if not (is_dir or item.is_file()):
            continue  # a broken symlink or a FIFO: skipped before, skipped now
        if item.name in (allowed_dirs if is_dir else allowed_files):
            continue
        yield Issue(
            "Root contents",
            f"Unrecognized {'directory' if is_dir else 'file'} in root: {item.name}",
            QUARANTINE_HINT,
            target=item,
            action="quarantine",
        )


def collect_naming_issues(base_path: Path) -> Iterator[Issue]:
    """Top-level folder names, per category.

    Deliberately not built on ``iter_archive_folders``: that helper keeps only
    folders that already have a valid prefix, which is exactly what this
    collector exists to report the absence of. It needs the category letter too.
    """
    for letter, category in CATEGORIES.items():
        cat_path = base_path / category
        if not cat_path.exists():
            continue
        for item in sorted(cat_path.iterdir(), key=lambda p: p.name):
            if not item.is_dir() or item.name == "BACKUP":
                continue
            if letter == "O":
                if not OLD_RE.match(item.name):
                    yield Issue("Naming", f"Old/{item.name} is not named YYYY-MM-DD-P0000-Descriptor")
                continue
            if not FOLDER_RE.match(item.name):
                yield Issue("Naming", f"{category}/{item.name} has no valid prefix")
                continue
            prefix = folder_prefix(item.name) or ""
            if prefix[0] != letter:
                # Legal: prefixes are permanent and survive a category move.
                # Worth surfacing so a misfile is still visible.
                yield Issue(
                    "Naming",
                    f"{category}/{item.name} was created in {CATEGORIES[prefix[0]]}",
                    severity=NOTICE,
                )


def _subfolder_issues(parent: Path, parent_prefix: str) -> Iterator[Issue]:
    """The numbered children of one folder. Unnumbered ones are allowed."""
    seen: dict[str, str] = {}
    for child in sorted(parent.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or "." not in child.name:
            continue
        match = SUBFOLDER_RE.match(child.name)
        if not match:
            if FOLDER_RE.match(child.name.split(".")[0] + " x"):
                yield Issue("Subfolders", f"{parent.name}/{child.name} is not named {parent_prefix}.NN Name")
            continue
        if match.group(1) != parent_prefix:
            yield Issue("Subfolders", f"{parent.name}/{child.name} carries another folder's prefix")
            continue
        number = match.group(2)
        if number in seen:
            yield Issue(
                "Subfolders",
                f"{parent.name}/ has two .{number} subfolders: {seen[number]} and {child.name}",
            )
        seen[number] = child.name


def collect_subfolder_issues(base_path: Path) -> Iterator[Issue]:
    """The P0001.01 convention that mksub enforces but verify never did."""
    for category, parent in iter_archive_folders(base_path):
        if category == CATEGORIES["O"]:
            continue
        yield from _subfolder_issues(parent, folder_prefix(parent.name) or "")


def collect_uniqueness_issues(base_path: Path) -> Iterator[Issue]:
    """A prefix is a permanent identifier, so exactly one folder may hold it."""
    by_prefix: dict[str, list[str]] = {}
    for _, item in iter_archive_folders(base_path):
        by_prefix.setdefault(folder_prefix(item.name) or "", []).append(display(item, base_path))

    for prefix, holders in sorted(by_prefix.items()):
        if len(holders) > 1:
            yield Issue(
                "Uniqueness",
                f"{prefix} is claimed by {len(holders)} folders: " + ", ".join(sorted(holders)),
                "not autofixable -- decide which one keeps the number",
            )


def collect_tombstone_issues(base_path: Path) -> Iterator[Issue]:
    for _, cat_path in category_dirs(base_path):
        for folder in sorted(cat_path.iterdir(), key=lambda p: p.name):
            if not folder.is_dir():
                continue
            state, data = read_tombstone_state(folder)
            if state == "absent":
                continue
            shown = display(folder, base_path)
            if state == "corrupt":
                yield Issue("Offload", f"Corrupted offload metadata in {shown}: {TOMBSTONE_NAME}")
                continue
            if data is None:
                yield Issue("Offload", f"Invalid metadata format in {shown}: expected JSON object")
                continue

            arch = data.get("archive")
            if arch is not None and not isinstance(arch, dict):
                yield Issue("Offload", f"Invalid archive block in {shown}: expected JSON object")
            elif isinstance(arch, dict):
                if arch.get("format") not in ARCHIVE_FORMATS:
                    # Reported here rather than left for restore, which would
                    # only discover it at extraction time with a worse message.
                    yield Issue(
                        "Offload",
                        f"{shown} records an unreadable archive format: {arch.get('format')!r}",
                    )
                if not isinstance(arch.get("versions"), list):
                    # The next push would rebuild `versions` from scratch, and
                    # pruning only ever deletes recorded names -- so every
                    # object the old list named becomes unreclaimable.
                    yield Issue("Offload", f"{shown} records an archive with no readable version list")

            if data.get("offloaded_at"):
                left = [p for p in folder.iterdir() if not _reserved_member(p.name)]
                if left:
                    yield Issue(
                        "Offload",
                        f"{shown} is marked offloaded but has {plural(len(left), 'file')}"
                        " (offload interrupted, or contents pulled back for local reading)",
                        severity=NOTICE,
                    )


def split_issues(issues: Sequence[Issue]) -> tuple[list[Issue], list[Issue]]:
    """Unresolved errors and unresolved notices. A resolved finding is neither."""
    live = [issue for issue in issues if not issue.resolution]
    return (
        [issue for issue in live if issue.severity == ERROR],
        [issue for issue in live if issue.severity == NOTICE],
    )


@app.command()
def verify(
    fix: Annotated[
        bool, typer.Option("--fix", help="Create missing category directories (safe, idempotent)")
    ] = False,
    quarantine: Annotated[
        bool, typer.Option("--quarantine", help="Also MOVE unrecognized root entries into BACKUP/<date>/")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt for --quarantine")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what --fix/--quarantine would do")] = False,
    as_json: AsJsonReport = False,
    report: Annotated[Path | None, typer.Option("--report", help="Save report to file")] = None,
    *,
    base_path: BasePath,
) -> None:
    """Verify that the structure follows GWArchive standards.

    Exits 1 when errors remain, so it can gate a script. Notices do not fail.

    --fix only creates missing category directories. Relocating unrecognized
    files is a separate, confirmed opt-in via --quarantine.
    """

    issues: list[Issue] = [
        *collect_structure_issues(base_path),
        *collect_root_issues(base_path),
        *collect_naming_issues(base_path),
        *collect_subfolder_issues(base_path),
        *collect_uniqueness_issues(base_path),
        *collect_tombstone_issues(base_path),
    ]

    if fix:
        for issue in issues:
            if issue.action != "create" or issue.target is None:
                continue
            if not dry_run:
                ensure_directory(issue.target)
            issue.resolution = "would create" if dry_run else "created"

    strays = [(i, i.target) for i in issues if i.action == "quarantine" and i.target is not None]
    if quarantine and strays:
        listing = Text(
            f"Move {plural(len(strays), 'unrecognized entry', 'unrecognized entries')} into BACKUP/\n\n",
            style="bold",
        )
        for _, item in strays:
            listing.append(f"{item.name}\n")
        if dry_run or confirm_destructive(listing, "Move them?", yes=yes):
            # Timestamped, so repeated runs never nest one quarantine inside
            # another.
            backup_dir = base_path / "BACKUP" / clock.today()
            for issue, item in strays:
                if dry_run:
                    issue.resolution = f"would move to BACKUP/{backup_dir.name}/{item.name}"
                    continue
                ensure_directory(backup_dir)
                run_fs(f"quarantine {item.name}", shutil.move, str(item), str(backup_dir / item.name))
                issue.resolution = f"moved to BACKUP/{backup_dir.name}/{item.name}"

    errors, notices = split_issues(issues)

    if as_json:
        emit_json(
            {
                "ok": not errors,
                "errors": len(errors),
                "notices": len(notices),
                "issues": [issue.as_dict() for issue in issues],
            }
        )
    else:
        render_verify_report(issues)

    if report:
        write_verify_report(report, base_path, issues)
        if not as_json:
            # Stdout is carrying a JSON document; a receipt would break it.
            note(f"Report saved to {report}")

    if errors:
        raise typer.Exit(1)


def render_verify_report(issues: Sequence[Issue]) -> None:
    """The human report, on stdout.

    These findings are the command's output the way ``list``'s table is, and
    the exit code carries pass/fail -- so they belong on stdout, and under
    --quiet the exit code is the whole report. They used to go to stderr while
    the clean-archive line went to stdout, which meant the primary output
    changed stream depending on the result, and a fully --fixed run wrote a
    green success summary to stderr.
    """
    if output.QUIET:
        return
    if not issues:
        ok("Verification passed. Every prefix unique, every folder where it says it is.")
        return

    errors, notices = split_issues(issues)
    grouped: dict[str, list[Issue]] = {}
    for issue in issues:
        grouped.setdefault(issue.group, []).append(issue)

    for group, group_issues in grouped.items():
        unresolved = sum(1 for i in group_issues if i.severity == ERROR and not i.resolution)
        heading = Text(f"{group}  ", style="bold")
        tail = f", {unresolved} unresolved" if unresolved else ""
        heading.append(plural(len(group_issues), "finding") + tail, style=STYLES["caption"])
        console.print(heading)
        for issue in group_issues:
            console.print(issue.line(), soft_wrap=True)

    summary = Text()
    summary.append(plural(len(errors), "error"), style="red" if errors else "green")
    summary.append(", ")
    summary.append(plural(len(notices), "notice"), style="yellow" if notices else "dim")
    resolved = len(issues) - len(errors) - len(notices)
    if resolved:
        summary.append(f", {resolved} resolved", style="green")
    console.print(_decorate("error" if errors else "ok", summary))


def write_verify_report(report: Path, base_path: Path, issues: Sequence[Issue]) -> None:
    """The --report file. Derives its own counts, so they cannot disagree."""
    errors, notices = split_issues(issues)
    lines = [
        f"GWArchive Verification Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Archive: {base_path}",
        "",
    ]
    if not issues:
        lines.append("Verification passed. The GWArchive structure is valid.")
    else:
        lines.append(f"{len(errors)} errors, {len(notices)} notices, {len(issues)} findings total")
        lines.append("")
        for issue in issues:
            resolution = f"  -> {issue.resolution}" if issue.resolution else ""
            lines.append(f"[{issue.severity}] {issue.group}: {issue.message}{resolution}")
    report.write_text("\n".join(lines) + "\n")
