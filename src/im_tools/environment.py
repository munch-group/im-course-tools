"""Checking the course environment, and refreshing it from the website.

Both of these used to be a loose script in the student's own folder, which
meant a fix could only reach them by asking a hundred people to download a file
again. Here they travel with the package instead: releasing im-tools updates
them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .course import fetch

# What `im check` insists on. The pair is (import name, what to call it), so a
# missing package can be reported by the name a student would recognise. Adding
# a widget to the course means adding it here and releasing im-tools.
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

# Refreshed by `im update`. A downloaded file must contain the matching string
# to be believable: cheap insurance against silently saving a "404 not found"
# page over a working environment.
FILES = ("pixi.toml", "pixi.lock")
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


def update(folder: Path, echo) -> int:
    """Fetch a fresh pixi.toml and pixi.lock into `folder`, then install them."""
    downloaded: dict[str, str] = {}
    for name in FILES:
        echo(f"Fetching {name}")
        text = fetch(name)
        if SANITY[name] not in text:
            echo(f"\nWhat came back for {name} does not look like a {name}.")
            echo("Nothing has been changed. Please tell your instructor.")
            return 1
        downloaded[name] = text

    # Only start writing once both downloads have arrived and look sane, so a
    # failure halfway cannot leave a half-updated environment behind.
    for name, text in downloaded.items():
        target = folder / name
        if target.exists():
            backup = folder / f"{name}.backup"
            shutil.copy2(target, backup)
            echo(f"Kept your old {name} as {backup.name}")
        target.write_text(text, encoding="utf-8")
        echo(f"Updated {name}")

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
