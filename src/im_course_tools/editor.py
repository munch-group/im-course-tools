"""The student's VS Code installation, and the parts of it the course conflicts with.

Two things this package does need to know about the editor rather than about the
course folder. Where its command-line tool is, which is not where a student's
PATH says; and which extensions on this machine break the arrangement the course
folder describes.

The second one is why this module exists. A course folder can say what settings
it wants, and `im update` keeps that file current, so a fault in it reaches
everybody. An extension is not like that: it is installed once, for the whole
editor, by a student who was offered it or who got it inside somebody else's
extension pack, and no file in the course folder can reach it. The first one to
matter was ms-python.vscode-python-envs, which leaves a "Run as Task" entry in
the Run menu whose `when` clause does not check the setting that switches the
extension off. Students who followed the current instructions got a folder that
switches it off, clicked that entry, and were told the command was not found.

What counts as a conflict is published with the course folder rather than
written down here, for the same reason .check_env.py reads pixi.toml: the next
one will be found in a class, and a list that travels with the download is one
Kasper can add to without releasing this package to a hundred machines.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

# The published list, inside the course folder. `im update` keeps it current
# along with everything else there, and reads the freshly downloaded copy rather
# than the one on disk, so a conflict named this morning is acted on this
# morning.
CONFLICTS_FILE = ".im-conflicts.json"

# Where VS Code installs itself, and where inside that its command-line tool
# lives. `code` on PATH is the exception rather than the rule: on macOS it only
# arrives if the student ran "Shell Command: Install 'code' command in PATH"
# from the palette, which nothing in the course asks them to do, and without a
# fallback every one of them would be told to fix this by hand.
APPLICATIONS = (
    ("/Applications/Visual Studio Code.app", "Contents/Resources/app/bin/code"),
    (str(Path.home() / "Applications" / "Visual Studio Code.app"), "Contents/Resources/app/bin/code"),
    (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code"), r"bin\code.cmd"),
    (os.path.expandvars(r"%PROGRAMFILES%\Microsoft VS Code"), r"bin\code.cmd"),
)


class Conflict(NamedTuple):
    """An extension that has to go, and what to tell the student about it."""

    id: str
    why: str


def code_command() -> Path | None:
    """VS Code's command-line tool, wherever this machine keeps it."""
    on_path = shutil.which("code")
    if on_path:
        return Path(on_path)
    for application, relative in APPLICATIONS:
        candidate = Path(application) / relative
        if candidate.is_file():
            return candidate
    return None


def run(command, timeout: float = 60.0) -> str | None:
    """Run something short, and give up quietly rather than raising."""
    try:
        finished = subprocess.run(
            [str(part) for part in command], capture_output=True, text=True,
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if finished.returncode != 0:
        return None
    return finished.stdout


def installed(command: Path) -> set[str] | None:
    """Every extension id this VS Code has, lower-cased for comparison.

    Extension ids are case-insensitive in the marketplace and are not always
    written the same way twice, so the comparison is made on one casing.
    """
    listed = run([command, "--list-extensions"], 60)
    if listed is None:
        return None
    return {line.strip().lower() for line in listed.splitlines() if line.strip()}


def conflicts(published: bytes | None) -> list[Conflict]:
    """The conflicting extensions the course currently names.

    A file that is missing, unreadable or not shaped as expected yields nothing
    at all. This runs in the middle of `im update`, where the environment is the
    thing being repaired: a malformed list is a reason to do nothing about
    extensions, never a reason to fail the repair a student is waiting for.
    """
    if not published:
        return []
    try:
        document = json.loads(published.decode("utf-8"))
        entries = document["extensions"]
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError):
        return []
    if not isinstance(entries, list):
        return []

    found = []
    for entry in entries:
        try:
            identifier = str(entry["id"]).strip()
            why = str(entry.get("why", "")).strip()
        except (TypeError, KeyError, AttributeError):
            continue
        if identifier:
            found.append(Conflict(identifier, why))
    return found


def conflicting_here(published: bytes | None, command: Path | None = None) -> list[Conflict]:
    """The named conflicts that are actually installed on this machine."""
    named = conflicts(published)
    if not named:
        return []
    command = command or code_command()
    if command is None:
        return []
    present = installed(command)
    if not present:
        return []
    return [conflict for conflict in named if conflict.id.lower() in present]


def remove(command: Path, identifier: str) -> bool:
    """Uninstall one extension. True when VS Code says it is gone."""
    return run([command, "--uninstall-extension", identifier], 120) is not None


def repair(published: bytes | None, echo) -> int:
    """Remove the extensions the course conflicts with. Returns how many went.

    Uninstalling is the only lever there is. VS Code can disable an extension
    for one folder, but only a person clicking in the Extensions view can do it;
    there is no setting for it and no command-line switch, so a course folder
    cannot ask for it and neither can this. What can be done from here is
    uninstalling, which is why each one is announced with its reason rather than
    done quietly: it is the student's editor, not ours, and they should be able
    to disagree with it afterwards.
    """
    named = conflicts(published)
    if not named:
        return 0

    command = code_command()
    if command is None:
        return 0

    present = installed(command)
    if present is None:
        return 0

    here = [conflict for conflict in named if conflict.id.lower() in present]
    if not here:
        return 0

    echo("")
    removed = 0
    for conflict in here:
        if remove(command, conflict.id):
            removed += 1
            echo(f"Removed the VS Code extension {conflict.id}.")
        else:
            echo(f"Could not remove the VS Code extension {conflict.id}.")
            echo("  Remove it yourself in VS Code's Extensions view (the blocks in the")
            echo("  sidebar), or bring this message to class.")
        if conflict.why:
            for line in conflict.why.splitlines():
                echo(f"  {line}")
    if removed:
        echo("Reload VS Code (or close and open it) for that to take effect.")
    return removed
