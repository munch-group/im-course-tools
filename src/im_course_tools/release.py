"""Whether the `im` being run is still the current one, and how to get the new one.

The whole reason these commands live in a package rather than as loose files in
the course folder is that a fix can then reach a hundred students through a
release. That only works if the release actually arrives, and a student has no
reason to think of upgrading a tool that has never once asked them to. So `im`
asks on their behalf, at most once a day, in the background, and says a single
line when there is something newer.

What that line says depends on how `im` got here. The conda package inside a
course environment, a `pixi global install` and a `pip install` are three
different things to upgrade, and telling a student the wrong one costs them an
afternoon. So the question of how this copy was installed is answered first,
from the machine rather than from a guess, and the answer decides both which
index is asked and which command is offered.

Nothing here ever upgrades on its own. `im` cannot replace the files it is
running out of while it is running out of them — on Windows it plainly cannot —
so the upgrade is something a command asks for, and what follows it is always a
request that the student run their command again.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "im-course-tools"
DEFAULT_OWNER = "munch-group"

# How long an answer stays good. A day is long enough that nobody pays for the
# question twice in a sitting, and short enough that a fix released on the
# Monday is in front of them on the Tuesday. A failed question is retried
# sooner, but not so soon that being offline costs a timeout every command.
GOOD_FOR = 24 * 60 * 60
RETRY_AFTER = 60 * 60

CONDA_INDEX = "https://api.anaconda.org/package/{owner}/{package}"
PYPI_INDEX = "https://pypi.org/pypi/{package}/json"

# How this copy of `im` was installed. The first three are conda packages and
# differ only in what upgrades them; the last is a checkout, where the code
# being run is not the code that was installed and upgrading would change
# nothing anybody could see.
CONDA_PROJECT = "conda-project"     # inside a course folder's .pixi/envs
CONDA_GLOBAL = "conda-global"       # pixi global install
CONDA = "conda"                     # some other conda environment
PIPX = "pipx"
PIP = "pip"
SOURCE = "source"

CONDA_KINDS = (CONDA_PROJECT, CONDA_GLOBAL, CONDA)

# The folder names conda hangs off the end of a channel URL, which are not the
# name of the account that published it.
SUBDIRS = ("noarch", "osx-64", "osx-arm64", "win-64", "linux-64", "linux-aarch64")

# Set by anything that has already told the student about a new version, so
# that the notice at the end of the command does not say it twice.
_said = False


@dataclass
class Install:
    """How this copy of `im` got onto the machine."""

    kind: str
    version: str
    prefix: Path
    owner: str = DEFAULT_OWNER
    project: Path | None = None     # the pixi project folder, when there is one

    @property
    def upgradable(self) -> bool:
        return self.kind != SOURCE

    @property
    def described(self) -> str:
        return {
            CONDA_PROJECT: "the conda package in your course environment",
            CONDA_GLOBAL: "a pixi global install",
            CONDA: "a conda package",
            PIPX: "a pipx install",
            PIP: "a pip install",
            SOURCE: "a checkout of the source",
        }[self.kind]


# --- which version is this, and how did it get here ------------------------- #

def installed_now(install: Install) -> str | None:
    """The version on disk now, read afresh rather than out of this process.

    The process running the upgrade read its own version number long before the
    upgrade happened, and would report the old one as the new one without ever
    noticing. A conda install is read back off the record conda just wrote; a
    pip install is asked in a new interpreter, which is the only one that will
    have seen the change.
    """
    if install.kind in CONDA_KINDS:
        return (conda_record(install.prefix) or {}).get("version")
    try:
        finished = subprocess.run(
            [sys.executable, "-c",
             f"import importlib.metadata as m; print(m.version({PACKAGE!r}))"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return finished.stdout.strip() or None if finished.returncode == 0 else None


def installed_version() -> str:
    """The version recorded for the package, which is not the same as the code run."""
    try:
        from importlib.metadata import version
        return version(PACKAGE)
    except Exception:                           # not installed, or no metadata
        return "0.0.0"


def running_from(prefix: Path, code: Path | None = None) -> bool:
    """Whether the code being run is the code installed into this prefix.

    Run from a checkout or an editable install it is not, and upgrading the
    installed copy would leave the running one exactly as it was.
    """
    code = Path(code) if code else Path(__file__)
    try:
        code.resolve().relative_to(prefix.resolve())
    except (ValueError, OSError):
        return False
    return True


def conda_record(prefix: Path) -> dict | None:
    """What conda wrote down when it installed this package here, if it did."""
    try:
        candidates = sorted((prefix / "conda-meta").glob(f"{PACKAGE}-*.json"))
    except OSError:
        return None
    for candidate in candidates:
        try:
            record = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("name") == PACKAGE:
            return record
    return None


def owner_of(record: dict | None) -> str:
    """The anaconda.org account a conda package came from.

    Written down as a channel URL, sometimes with the platform folder on the
    end and sometimes without, so the last segment is not reliably the account.
    """
    channel = (record or {}).get("channel", "").rstrip("/")
    if not channel:
        return DEFAULT_OWNER
    parts = [part for part in channel.split("/") if part]
    if parts and parts[-1] in SUBDIRS:
        parts.pop()
    return parts[-1] if parts else DEFAULT_OWNER


def pixi_project(prefix: Path) -> Path | None:
    """The pixi project a `<project>/.pixi/envs/<name>` prefix belongs to."""
    parents = prefix.parents
    if len(parents) < 3:
        return None
    if parents[0].name != "envs" or parents[1].name != ".pixi":
        return None
    project = parents[2]
    for marker in ("pixi.toml", "pyproject.toml"):
        try:
            if (project / marker).is_file():
                return project
        except OSError:
            continue
    return None


def describe(prefix: Path | None = None, home: Path | None = None,
             code: Path | None = None) -> Install:
    """How this copy of `im` was installed, asked of the machine rather than guessed."""
    prefix = Path(prefix) if prefix else Path(sys.prefix)
    home = Path(home) if home else Path.home()
    version = installed_version()

    if not running_from(prefix, code):
        return Install(SOURCE, version, prefix)

    record = conda_record(prefix)
    if record is not None:
        owner = owner_of(record)
        version = record.get("version") or version
        if prefix.parent == home / ".pixi" / "envs":
            return Install(CONDA_GLOBAL, version, prefix, owner)
        project = pixi_project(prefix)
        if project is not None:
            return Install(CONDA_PROJECT, version, prefix, owner, project)
        return Install(CONDA, version, prefix, owner)

    if "pipx" in prefix.parts:
        return Install(PIPX, version, prefix)
    return Install(PIP, version, prefix)


# --- comparing two versions ------------------------------------------------- #

def as_numbers(version: str) -> tuple[int, ...]:
    """The leading numbers of a version, and nothing after the first that is not.

    So 0.1.4 is (0, 1, 4) and 0.1.4.rc1 is (0, 1, 4) as well, which makes a
    release candidate equal to its release rather than newer than it. Erring
    that way means a student is never told to upgrade to something older.
    """
    numbers = []
    for chunk in re.split(r"[._-]", version.strip().lstrip("vV")):
        if not chunk.isdigit():
            break
        numbers.append(int(chunk))
    return tuple(numbers)


def newer(available: str | None, installed: str) -> bool:
    """Whether `available` is a version worth interrupting a student about."""
    if not available:
        return False
    ours = as_numbers(installed)
    theirs = as_numbers(available)
    return bool(ours) and bool(theirs) and theirs > ours


# --- asking, and remembering the answer ------------------------------------- #

def disabled() -> bool:
    """IM_NO_UPDATE_CHECK, for the tests and for anyone teaching without wifi."""
    return bool(os.environ.get("IM_NO_UPDATE_CHECK"))


def cache_file(home: Path | None = None) -> Path:
    """Where the last answer is kept: in the home folder, not the course folder.

    The course folder gets moved, copied and started over. This question is
    about the machine, so the answer belongs somewhere that survives all three.
    """
    home = Path(home) if home else Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return base / PACKAGE / "latest.json"


def read_cache() -> dict | None:
    try:
        return json.loads(cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_cache(available: str | None) -> None:
    path = cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"asked": time.time(), "available": available}),
                        encoding="utf-8")
    except OSError:
        pass                                    # a cache that will not be written
                                                # is not worth a word to a student


def fresh(cached: dict | None) -> bool:
    """Whether a remembered answer is recent enough to use without asking again."""
    if not cached:
        return False
    age = time.time() - cached.get("asked", 0)
    return age < (GOOD_FOR if cached.get("available") else RETRY_AFTER)


def fetch_json(url: str, timeout: float) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": "instructing-machines"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:                           # offline is not an error here
        return None


def ask_index(install: Install, timeout: float = 5.0) -> str | None:
    """The newest version, from whichever index this copy of `im` came from."""
    if install.kind in CONDA_KINDS:
        data = fetch_json(CONDA_INDEX.format(owner=install.owner, package=PACKAGE), timeout)
        return (data or {}).get("latest_version")
    data = fetch_json(PYPI_INDEX.format(package=PACKAGE), timeout)
    return ((data or {}).get("info") or {}).get("version")


def known_latest() -> str | None:
    """The remembered answer, with no network and no waiting. None if there is none."""
    cached = read_cache()
    return cached.get("available") if fresh(cached) else None


def latest(install: Install | None = None, timeout: float = 5.0,
           use_cache: bool = True) -> str | None:
    """The newest version there is, remembered if it was asked for recently."""
    if disabled():
        return None
    if use_cache:
        cached = read_cache()
        if fresh(cached):
            return cached.get("available")
    available = ask_index(install or describe(), timeout)
    write_cache(available)
    return available


# --- saying so -------------------------------------------------------------- #

def upgrade_command(install: Install) -> tuple[list[str], Path | None] | None:
    """What to run to get the newer one, and the folder to run it in.

    None means this copy cannot be upgraded from here — a checkout, or a conda
    environment with no conda on the PATH to drive it — which is worth saying
    plainly rather than offering a command that will not work.
    """
    pixi = shutil.which("pixi")
    if install.kind == CONDA_GLOBAL and pixi:
        return [pixi, "global", "update", PACKAGE], None
    if install.kind == CONDA_PROJECT and pixi and install.project:
        return [pixi, "update", PACKAGE], install.project
    if install.kind == CONDA:
        conda = shutil.which("conda") or shutil.which("mamba")
        if conda:
            return [conda, "update", "-y", "-c", install.owner,
                    "-c", "conda-forge", PACKAGE], None
        return None
    if install.kind == PIPX:
        pipx = shutil.which("pipx")
        return ([pipx, "upgrade", PACKAGE], None) if pipx else None
    if install.kind == PIP:
        return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE], None
    return None


def as_typed(prepared: tuple[list[str], Path | None]) -> list[str]:
    """The upgrade command as a student would type it, cd included when needed."""
    command, folder = prepared
    lines = [f'cd "{folder}"'] if folder else []
    return lines + [" ".join(command)]


def say_if_newer(echo, available: str | None, install: Install) -> bool:
    """One line about a newer version, and how to get it. True if anything was said."""
    global _said
    if _said or not newer(available, install.version):
        return False
    _said = True
    prepared = upgrade_command(install)
    try:
        echo("")
        echo(f"There is a newer im: {install.version} -> {available}")
        if prepared is None:
            echo(f"This one is {install.described}, which `im` cannot upgrade for you.")
        else:
            echo("Upgrade it with:")
            echo("")
            for line in as_typed(prepared):
                echo(f"    {line}")
        echo("")
    except (OSError, ValueError):               # a closed stream at exit
        pass
    return True


def announce_later(echo) -> None:
    """Ask in the background, and say at the end of the command if there is news.

    Asked up front and waited for, this would put a network round-trip in front
    of every `im get`, and several seconds in front of every one run by a
    student whose connection is the thing that is broken. Asked in the
    background, the first run of the day warms the answer and the rest of that
    day's runs read it off disk in no time at all.
    """
    if disabled():
        return
    install = describe()
    if not install.upgradable:
        return

    cached = read_cache()
    answer: list[str | None] = []
    worker = None

    if fresh(cached):
        answer.append(cached.get("available"))
    else:
        def ask() -> None:
            try:
                answer.append(latest(install, use_cache=False))
            except Exception:                   # never worth an error of its own
                pass

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()

    # Said at the end either way, remembered or freshly asked. Said as soon as
    # it is known, a remembered answer would land above the output of the
    # command that was actually asked for, and a student would see the notice
    # in a different place depending on what time of day it was.
    def at_the_end() -> None:
        if worker is not None:
            worker.join(2.0)
        if answer:
            say_if_newer(echo, answer[0], install)

    atexit.register(at_the_end)


# --- doing it --------------------------------------------------------------- #

def rerun(echo, argv: list[str] | None = None) -> None:
    """Ask for the command to be run again, because `im` cannot re-run itself.

    It could, on a Mac. It cannot on Windows, where the files it is running out
    of are the files being replaced, and a half-swapped install is a far worse
    place to leave a student than one extra line to type.
    """
    argv = sys.argv[1:] if argv is None else argv
    echo("")
    echo("Upgraded. `im` cannot swap itself out while it is running, so please")
    echo("run your command again to use the new one:")
    echo("")
    echo(f"    im {' '.join(argv)}".rstrip())
    echo("")


def upgrade(install: Install, echo) -> int:
    """Run the upgrade for this kind of install, and say plainly what happened."""
    prepared = upgrade_command(install)
    if prepared is None:
        echo(f"There is a newer im, but this one is {install.described},")
        echo("which cannot be upgraded from here.")
        echo(f"It lives in {install.prefix}. Please bring that line to class.")
        return 1

    command, folder = prepared
    echo(f"Upgrading im from {install.version}. This may take a minute.")
    echo("")
    for line in as_typed(prepared):
        echo(f"    {line}")
    echo("")
    try:
        finished = subprocess.run(command, cwd=str(folder) if folder else None)
    except OSError as error:
        echo(f"That could not be started: {error}")
        return 1

    if finished.returncode != 0:
        echo("")
        echo("That did not finish cleanly. Bring the message above to class.")
        if install.kind == CONDA_PROJECT:
            echo("")
            echo("`im update` may get there instead: it takes the course's own")
            echo("pixi.toml, which names the version everyone else is on.")
        return finished.returncode

    # It can finish perfectly happily and change nothing at all: the new version
    # may not have reached this channel yet, or may not install on this Python.
    # Saying "upgraded, run it again" then sends a student round the same loop
    # for as long as they are willing to go.
    after = installed_now(install)
    if after is not None and not newer(after, install.version):
        echo("")
        echo(f"That ran without complaining, but the version here is still {after}.")
        echo("The newer one has not reached this channel yet, or it will not install")
        echo("on this machine. Running the command again will only say the same")
        echo("thing, so please tell your instructor instead.")
        return 1
    return 0


def upgrade_if_newer(echo, confirm=None, timeout: float = 5.0) -> int | None:
    """Upgrade `im` if there is a newer one, and say whether the command should stop.

    None means carry on: there was nothing newer, or the student said no. An
    exit code means stop, because carrying on is pointless either way — after a
    successful upgrade the code that would carry on is the old code, and after
    a failed one the reason would be buried under the rest of the output.
    """
    global _said
    if disabled():
        return None
    install = describe()
    if not install.upgradable:
        return None

    available = latest(install, timeout=timeout)
    if not newer(available, install.version):
        return None

    _said = True                                # the notice at the end is now noise
    echo(f"There is a newer im: {install.version} -> {available}")
    echo(f"This one is {install.described}.")
    echo("")

    # Only worth asking when there is somebody there to answer. Piped into a
    # file or run from a script, the question would hang or answer itself.
    if confirm is not None and sys.stdin.isatty():
        if not confirm("Upgrade it now?"):
            prepared = upgrade_command(install)
            if prepared is not None:
                echo("")
                echo("Left alone. When you want it:")
                echo("")
                for line in as_typed(prepared):
                    echo(f"    {line}")
                echo("")
            return None

    code = upgrade(install, echo)
    if code != 0:
        return code
    rerun(echo)
    return 0
