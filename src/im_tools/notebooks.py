"""Fetching one chapter notebook from the course website.

The website publishes every chapter the book renders as a loose .ipynb next to
an index.txt naming them, so this only has to read that index and pick one.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from .course import fetch, url_for

INDEX = "notebooks/index.txt"


def available() -> list[str]:
    """The chapter names the website is offering, one per line."""
    return [line.strip() for line in fetch(INDEX).splitlines() if line.strip()]


def normalise(name: str) -> str:
    """Fold away the differences nobody should have to remember.

    So `data_structures`, `data-structures`, `Data Structures` and
    `data_structures.ipynb` all mean the same chapter.
    """
    name = name.strip()
    if name.endswith(".ipynb"):
        name = name[: -len(".ipynb")]
    return name.replace("-", "_").replace(" ", "_").lower()


def resolve(wanted: str, chapters: list[str]) -> str | None:
    return {normalise(c): c for c in chapters}.get(normalise(wanted))


def suggestions(wanted: str, chapters: list[str]) -> list[str]:
    return difflib.get_close_matches(wanted, chapters, n=3, cutoff=0.6)


def free_path(folder: Path, stem: str) -> tuple[Path, bool]:
    """Where to save, and whether we had to step aside for an existing file."""
    target = folder / f"{stem}.ipynb"
    if not target.exists():
        return target, False
    number = 2
    while (folder / f"{stem}-{number}.ipynb").exists():
        number += 1
    return folder / f"{stem}-{number}.ipynb", True


def download(folder: Path, stem: str, echo) -> int:
    """Fetch the notebook for chapter `stem` into `folder`."""
    echo(f"Fetching {url_for(f'notebooks/{stem}.ipynb')}")
    text = fetch(f"notebooks/{stem}.ipynb")

    # A notebook is a JSON file, and every notebook has cells. If what came
    # back does not, it is a "page not found" page wearing a notebook's name.
    if '"cells"' not in text:
        echo("\nWhat came back does not look like a notebook.")
        echo("Nothing has been changed. Please tell your instructor.")
        return 1

    target, stepped_aside = free_path(folder, stem)
    target.write_text(text, encoding="utf-8")

    if stepped_aside:
        echo(f"\nYou already had {stem}.ipynb, so I left it exactly as it was.")
        echo(f"The fresh copy is {target.name}.")
    else:
        echo(f"\nSaved {target.name}. Open it in VS Code and pick the .pixi kernel.")
    return 0
