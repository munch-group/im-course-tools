"""Checking the course environment, and refreshing it from the website.

Both of these used to be a loose script in the student's own folder, which
meant a fix could only reach them by asking a hundred people to download a file
again. Here they travel with the package instead: releasing im-course-tools updates
them.

Refreshing means the whole of the course folder's own setup and not just the
environment: the tasks `pixi run` offers, the script that tells VS Code where
pixi is, and VS Code's settings are all fixed the same way an environment is,
and all of them used to reach a student only by downloading the folder again
and moving their work across by hand. A fix to any of them is worth as little
as a fix nobody receives.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from .course import fetch_bytes, url_for

# What `im check` insists on. The pair is (import name, what to call it), so a
# missing package can be reported by the name a student would recognise. Adding
# a widget to the course means adding it here and releasing im-course-tools.
REQUIRED = [
    ("steps_widget", "steps-widget"),
    ("puzzle_widget", "puzzle-widget"),
    ("codelens_widget", "codelens-widget"),
    ("turtle_widget", "turtle-widget"),
    ("sandbox_widget", "sandbox-widget"),
    ("iplot_widget", "iplot-widget"),
    ("im_pytest", "im-pytest"),
    ("pandas", "pandas"),
    ("seaborn", "seaborn"),
]

# What `im update` reads the current versions out of: the course folder itself,
# exactly as the website publishes it for a student starting today. Taking the
# one download rather than each file loose means the answer cannot be half of
# one version and half of another, and a file added to the course folder next
# term arrives without anything here having to be told about it.
ARCHIVE = "instructing-machines.zip"

# The files inside it that `im update` keeps current. Every one of them is
# course plumbing rather than anybody's work: the environment pixi builds from,
# the tasks `pixi run` offers, the script that tells VS Code where pixi is, and
# the editor's own settings.
#
# The same download also holds the week-one notebooks, the data the chapters
# read, and nothing else a student has written. Those are deliberately not in
# this list. A notebook is worked in, and a command that refreshes work is a
# command that eventually destroys some.
FILES = (
    "pixi.toml",
    "pixi.lock",
    ".pin_pixi_path.py",
    ".gitignore",
    ".vscode/settings.json",
    ".vscode/extensions.json",
)

# The two without which there is no environment at all. A download missing one
# of them is a broken build and worth stopping for. The other four are taken
# when they are there and passed over when they are not, so that dropping one
# from the course folder does not stop `im update` working on the day it goes.
ESSENTIAL = ("pixi.toml", "pixi.lock")

# A downloaded file must contain the matching string to be believable: cheap
# insurance against a build that produced something empty or truncated. What
# this used to guard against — a "404 not found" page saved over a working
# environment — cannot happen now that the files arrive inside an archive that
# is itself checked before anything is read out of it.
SANITY = {"pixi.toml": "[workspace]", "pixi.lock": "version:"}


def check(echo) -> int:
    """Import everything the course needs, and say plainly what is missing."""
    missing = []
    for module, name in REQUIRED:
        try:
            __import__(module)
        except ImportError:
            missing.append(name)

    if not missing:
        echo(f"Everything is installed. Python {sys.version.split()[0]}")
        return 0

    echo("Your environment is missing:")
    echo("")
    for name in missing:
        echo(f"    {name}")
    echo("")
    echo("Run `im update` to refresh the environment. If that does not fix it,")
    echo("bring this message to class.")
    return 1


def single_root(archive: zipfile.ZipFile) -> str | None:
    """The one folder every entry in the archive lives in, if there is one.

    The course folder is published as a zip that unpacks into a folder of its
    own, and reading the name off the archive rather than knowing it here means
    renaming that folder is the website's business and stays the website's.
    """
    roots = {member.split("/")[0] for member in archive.namelist()
             if member and not member.endswith("/")}
    return roots.pop() if len(roots) == 1 else None


def published(data: bytes) -> dict[str, bytes] | None:
    """The files this command keeps current, read out of the published folder.

    Only the names in FILES are read, and each is later written to the path
    this module names rather than to the one the archive gives it, so nothing
    inside a zip has any say in where a file lands. A name that is not in there
    is left out rather than invented, and the caller decides what is missed.
    """
    # Every zip in the world starts with these four bytes. If these are not
    # them, what came back is a "page not found" page wearing a zip's name.
    if not data.startswith(b"PK\x03\x04"):
        return None
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        root = single_root(archive)
        if root is None:
            return None
        found = {}
        for name in FILES:
            try:
                found[name] = archive.read(f"{root}/{name}")
            except KeyError:                # not in this term's course folder
                continue
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    return found


def differs(mine: Path, theirs: bytes) -> bool:
    """Whether what is on disk is something other than what was published.

    Compared with the line endings folded together, because an editor on
    Windows can rewrite every line of a file without changing a word of it, and
    a student whose editor did that should not be handed a fresh copy — and a
    fresh .backup beside it — every single time they run this.
    """
    try:
        return mine.read_bytes().replace(b"\r\n", b"\n") != theirs.replace(b"\r\n", b"\n")
    except OSError:
        return True


def put(folder: Path, name: str, content: bytes) -> bool:
    """Write one file, keeping whatever was there. True if something was kept.

    Written as it was published, byte for byte, so that the next run compares
    equal and leaves it alone.
    """
    target = folder.joinpath(*name.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    kept = target.exists()
    if kept:
        shutil.copy2(target, target.with_name(target.name + ".backup"))
    target.write_bytes(content)
    return kept


def update(folder: Path, echo) -> int:
    """Bring the course folder's own files up to date, then install."""
    echo(f"Fetching {url_for(ARCHIVE)}")
    theirs = published(fetch_bytes(ARCHIVE, timeout=180))
    if theirs is None:
        echo("\nWhat came back does not look like the course folder.")
        echo("Nothing has been changed. Please tell your instructor.")
        return 1

    for name in ESSENTIAL:
        if name not in theirs:
            echo(f"\nThat download has no {name} in it.")
            echo("Nothing has been changed. Please tell your instructor.")
            return 1
    for name, marker in SANITY.items():
        if name in theirs and marker.encode("utf-8") not in theirs[name]:
            echo(f"\nWhat came back for {name} does not look like a {name}.")
            echo("Nothing has been changed. Please tell your instructor.")
            return 1

    # Which files are actually out of date is settled before a single one is
    # written, so a download that goes wrong halfway cannot leave the folder
    # half updated — and a file that already matches is left alone entirely,
    # rather than being replaced by an identical copy of itself and leaving a
    # .backup behind to say so.
    stale = [name for name in FILES if name in theirs
             and differs(folder.joinpath(*name.split("/")), theirs[name])]

    echo("")
    for name in stale:
        if put(folder, name, theirs[name]):
            echo(f"Updated {name}, keeping your old one as {name}.backup")
        else:
            echo(f"Added {name}")

    current = len(theirs) - len(stale)
    if current and stale:
        echo(f"The other {current} {'file was' if current == 1 else 'files were'} "
             f"already up to date.")
    elif current:
        echo(f"All {current} of your course folder's files were already up to date.")

    # Always, even when nothing above changed. `im update` is the command a
    # student is sent to when the environment is broken, and an install that is
    # skipped because the files were already right would turn it away at the
    # door in exactly the case it exists for.
    echo("\nInstalling. This may take a few minutes.\n")
    pixi = shutil.which("pixi")
    if pixi is None:
        echo("Could not find pixi. Open a new terminal and run `pixi install` yourself.")
        return 1

    result = subprocess.run([pixi, "install"], cwd=folder)
    if result.returncode != 0:
        echo("\n`pixi install` did not finish cleanly. Bring the message above to class.")
        return result.returncode

    echo("\nDone. Run `im check` to confirm.")
    return 0
