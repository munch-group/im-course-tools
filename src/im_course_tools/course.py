"""Where the course lives on the web, and where the student's copy of it lives.

Every command needs one or both of those two answers, and they are the only
things in this package that know anything about the outside world.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

# Where the course folder is published. Change this if the website moves.
DEFAULT_BASE_URL = "https://munch-group.org/instructing-machines"


def base_url() -> str:
    """The site to fetch from, read afresh every time rather than at import.

    IM_COURSE_URL overrides the default, which is how the site is tested against
    a local preview before it is published; students never set it. Reading it
    per call rather than once at import is what lets a test point the command
    somewhere else after the module is already loaded — captured at import, an
    override set later is ignored and the test quietly talks to the real site.
    """
    return os.environ.get("IM_COURSE_URL", DEFAULT_BASE_URL).rstrip("/")

# The file that marks a folder as *the* course folder. It is the pixi manifest,
# so the marker is the same thing that makes the folder work at all.
MARKER = "pixi.toml"


class CourseFolderNotFound(Exception):
    """Raised when a command that writes files cannot tell where to write them."""


def url_for(path: str) -> str:
    return f"{base_url()}/{path.lstrip('/')}"


def fetch_bytes(path: str, timeout: int = 60) -> bytes:
    """Download one path from the course site and return it as it arrived.

    Raises urllib.error.URLError if the website cannot be reached, which the
    calling command is expected to catch and turn into a friendly message.
    """
    request = urllib.request.Request(
        url_for(path), headers={"User-Agent": "instructing-machines"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch(path: str, timeout: int = 60) -> str:
    """The same, for the files that are text: notebooks, manifests, pixi.toml."""
    return fetch_bytes(path, timeout).decode("utf-8")


def course_folder(start: Path | None = None) -> Path:
    """The course folder: the nearest folder at or above `start` holding pixi.toml.

    Walking up rather than taking the working directory means `im get` puts a
    notebook in the course folder whether it is run there or two subfolders
    down, which is where a student will actually be by week five. IM_COURSE_FOLDER
    overrides the search, for tests and for anyone with an unusual layout.
    """
    override = os.environ.get("IM_COURSE_FOLDER")
    if override:
        return Path(override).expanduser().resolve()

    here = (start or Path.cwd()).resolve()
    for folder in [here, *here.parents]:
        if (folder / MARKER).is_file():
            return folder
    raise CourseFolderNotFound(
        "This does not look like your course folder: nothing here, or in any\n"
        f"folder above it, has a {MARKER} in it.\n\n"
        "Open the course folder in VS Code and try again from its terminal."
    )
