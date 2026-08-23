"""Fetching one project folder from the course website.

A project arrives as a zip holding the file the student writes code in, the
test program that checks it, and any data the project reads. Projects are
published one per week, so a student only ever has the ones they have reached.
"""

from __future__ import annotations

import difflib
import io
import zipfile
from pathlib import Path

from .course import fetch, fetch_bytes, url_for

INDEX = "project-files/index.txt"
FOLDER = "projects"


def available() -> list[str]:
    """The project names the website is offering, one per line."""
    return [line.strip() for line in fetch(INDEX).splitlines() if line.strip()]


def normalise(name: str) -> str:
    """Fold away the differences nobody should have to remember.

    So `alignment`, `alignmentproject`, `alignment-project` and
    `alignment_project` all mean the same project.
    """
    name = name.strip().lower()
    if name.endswith(".zip"):
        name = name[: -len(".zip")]
    name = name.replace("-", "_").replace(" ", "_")
    for tail in ("_project", "project"):
        if name.endswith(tail) and len(name) > len(tail):
            return name[: -len(tail)]
    return name


def resolve(wanted: str, projects: list[str]) -> str | None:
    return {normalise(p): p for p in projects}.get(normalise(wanted))


def suggestions(wanted: str, projects: list[str]) -> list[str]:
    return difflib.get_close_matches(
        normalise(wanted), [normalise(p) for p in projects], n=3, cutoff=0.6)


def safe_members(archive: zipfile.ZipFile, name: str) -> list[str] | None:
    """Every member of the zip, checked to live inside a folder called `name`.

    A zip can name its files anything at all, including `../../somewhere-else`,
    and unpacking one without looking writes wherever it says. Nothing that
    comes off the website should be able to put a file outside `projects/`.
    """
    members = []
    root = Path(name)
    for member in archive.namelist():
        if member.endswith("/"):
            continue
        path = Path(member)
        if path.is_absolute() or ".." in path.parts:
            return None
        if path.parts[:1] != root.parts:
            return None
        members.append(member)
    return members or None


def download(folder: Path, name: str, echo) -> int:
    """Fetch project `name` into `folder`/projects, unless it is already there."""
    # A project is a folder you work in for a week, not a file you can be handed
    # a second copy of. If it is already here, stop: unpacking over it would put
    # the empty starting file back on top of the student's own code.
    destination = folder / FOLDER / name
    if destination.exists():
        echo(f"You already have {FOLDER}/{name}, so I have left it alone.")
        echo("")
        echo("If you want to start that project over from scratch, rename or")
        echo(f"move your {FOLDER}/{name} folder first, then ask again.")
        return 1

    echo(f"Fetching {url_for(f'project-files/{name}.zip')}")
    data = fetch_bytes(f"project-files/{name}.zip")

    # Every zip in the world starts with these four bytes. If these are not
    # them, what came back is a "page not found" page wearing a zip's name.
    if not data.startswith(b"PK\x03\x04"):
        echo("\nWhat came back does not look like a project.")
        echo("Nothing has been changed. Please tell your instructor.")
        return 1

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        members = safe_members(archive, name)
    except zipfile.BadZipFile:
        members = None

    if members is None:
        echo("\nThat project download is damaged, or it is not laid out as")
        echo("expected. Nothing has been changed. Please tell your instructor.")
        return 1

    target = folder / FOLDER
    target.mkdir(exist_ok=True)
    archive.extractall(target, members=members)

    echo(f"\nSaved {FOLDER}/{name} ({len(members)} files).")
    echo(f"Open the folder in VS Code and start with {name}.py.")
    return 0
