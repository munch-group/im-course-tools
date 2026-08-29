"""Checking the course environment, and refreshing it from the website.

Refreshing travels with the package: it used to be a loose script in the
student's own folder, which meant a fix could only reach them by asking a
hundred people to download a file again, and releasing im-course-tools updates
it instead.

Checking went the other way and left this module entirely, and the day it is
used says why. `pixi run check` is what a student types straight after `pixi
install`, to be told whether that worked. A check that arrives inside the
environment cannot answer that question: when the install went wrong, the check
went wrong with it, and the answer is `im: command not found`. So checking is
.check_env.py in the course folder, run by `pixi run check`, needing nothing but
a Python. There is no `im check` wrapping it, because `pixi global install` puts
`im` in the same folder as the pixi binary itself: the lost PATH that stops
`pixi run check` stops any `im` command with it, so a wrapper could never answer
in a case the folder's own script could not. The objection that put checking
here in the first place is answered by `im update`, which keeps that file
current along with everything else in the folder.

Refreshing means the whole of the course folder's own setup and not just the
environment: the tasks `pixi run` offers, the script that tells VS Code where
pixi is, and VS Code's settings are all fixed the same way an environment is,
and all of them used to reach a student only by downloading the folder again
and moving their work across by hand. A fix to any of them is worth as little
as a fix nobody receives.

Refreshing them is also what breaks them: the notebook kernel and the paths
telling VS Code where pixi is are made by `pixi run` tasks rather than by
packages, and both are undone by the very files this replaces. So the last
thing `im update` does is hand back to the course folder's own `pixi run check`
to put them there again.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from .course import fetch_bytes, url_for
from .security import pixi_locations

# Where pixi puts the environment it builds from pixi.toml.
ENV_PATH = (".pixi", "envs", "default")

# The course folder's own answer to "is this environment complete", and the only
# thing that answers it. `pixi run check` runs it; it reads the folder's
# pixi.toml and reports what is not there, so the list of what the course needs
# is the manifest itself and cannot drift from it. Named here because `im
# update` is what keeps it current.
CHECK_SCRIPT = ".check_env.py"

# What `im doctor` asks for when a course folder's pixi.toml cannot be read at
# all. The pair is (import name, what to call it), so a missing package can be
# reported by the name a student would recognise.
#
# This is a copy of a list that lives somewhere else, and the whole reason the
# script above reads the manifest is that such a copy goes stale: this one had
# drifted far enough from pixi.toml to report a complete environment on a
# machine with no matplotlib and no biopython. Nothing new should be added here.
# Add it to the course's pixi.toml, which is the list that is actually read.
REQUIRED = [
    ("ipykernel", "ipykernel"),
    ("ipywidgets", "ipywidgets"),
    ("anywidget", "anywidget"),
    ("jupyterlab", "jupyterlab"),
    ("pandas", "pandas"),
    ("seaborn", "seaborn"),
    ("matplotlib", "matplotlib"),
    ("pytest", "pytest"),
    ("steps_widget", "steps-widget"),
    ("sandbox_widget", "sandbox-widget"),
    ("snippet_cast", "snippet-cast"),
    ("iplot_widget", "iplot-widget"),
    ("turtle_widget", "turtle-widget"),
    ("puzzle_widget", "puzzle-widget"),
    ("codelens_widget", "codelens-widget"),
    ("im_pytest", "im-pytest"),
    ("Bio", "biopython"),
    ("cryptography", "cryptography"),
]

# What `im update` reads the current versions out of: the course folder itself,
# exactly as the website publishes it for a student starting today. Taking the
# one download rather than each file loose means the answer cannot be half of
# one version and half of another, and a file added to the course folder next
# term arrives without anything here having to be told about it.
ARCHIVE = "instructing-machines.zip"

# Named on its own because it is the one file here that this machine writes into
# as well as the website, which the comparison further down has to know about.
SETTINGS = ".vscode/settings.json"

# The files inside it that `im update` keeps current. Every one of them is
# course plumbing rather than anybody's work: the environment pixi builds from,
# the tasks `pixi run` offers, the scripts that tell VS Code and the terminal
# where pixi is and that say whether the environment is complete, and the
# editor's own settings.
#
# .check_env.py is in this list for the reason it exists at all. It sits in the
# folder rather than in this package so that it still runs when the environment
# is the thing that is broken, and everything that lives in the folder is a copy
# a fix cannot reach on its own. This is how a fix reaches it.
#
# The same download also holds the week-one notebooks, the data the chapters
# read, and nothing else a student has written. Those are deliberately not in
# this list. A notebook is worked in, and a command that refreshes work is a
# command that eventually destroys some.
FILES = (
    "pixi.toml",
    "pixi.lock",
    CHECK_SCRIPT,
    ".pin_pixi_path.py",
    ".pin_shell_path.py",
    ".pin_shell_path.sh",
    ".gitignore",
    SETTINGS,
    ".vscode/extensions.json",
)

# The two without which there is no environment at all. A download missing one
# of them is a broken build and worth stopping for. The others are taken
# when they are there and passed over when they are not, so that dropping one
# from the course folder does not stop `im update` working on the day it goes.
ESSENTIAL = ("pixi.toml", "pixi.lock")

# A downloaded file must contain the matching string to be believable: cheap
# insurance against a build that produced something empty or truncated. What
# this used to guard against — a "404 not found" page saved over a working
# environment — cannot happen now that the files arrive inside an archive that
# is itself checked before anything is read out of it.
SANITY = {"pixi.toml": "[workspace]", "pixi.lock": "version:"}

# The two settings .pin_pixi_path.py writes into .vscode/settings.json, and the
# comment block it puts above each of them.
#
# Both hold an absolute path belonging to one machine — where pixi is, and where
# this folder's Python is — so the published settings.json cannot carry them and
# never will. Compared as they stand, a student's pinned copy is therefore
# different from the published one on every run for ever, and would be replaced,
# stripped of its pins and left with a fresh .backup every single time. Taken
# out of both sides first, what gets compared is the part the website is
# actually publishing.
#
# The pixi setting is named twice because the extension that reads it was
# replaced: the course now recommends munch-group.im-pixi-vscode, and settings
# are namespaced by the extension that owns them. A student who set the folder
# up under the old one still has the old key sitting in their settings.json, and
# leaving it out here would do to them exactly what naming only the old one did
# to everybody — a file permanently out of date, replaced and backed up on every
# single run. Both come out, so both are forgiven.
LOCAL_SETTINGS = ("im-pixi-vscode.pixiExecutable", "pixi-code.pixiExecutable",
                  "python.defaultInterpreterPath")

PINNED = re.compile(
    r"(?:^[ \t]*//[^\n]*\n)*"                          # the comment above it
    r"^[ \t]*\"(?:"
    + "|".join(re.escape(name) for name in LOCAL_SETTINGS)
    + r")\"[ \t]*:[ \t]*\"(?:[^\"\\]|\\.)*\",?[ \t]*\n"  # the setting
    r"(?:[ \t]*\n)?",                                   # the blank line after
    re.MULTILINE,
)

# The course folder's own name for "put this machine's setup right": it installs
# the notebook kernel, writes the two settings above, and finishes by running
# the folder's own .check_env.py.
SETUP_TASK = "check"

# Whether the manifest still defines that task. Looked for rather than assumed,
# so that dropping it from the course folder does not stop `im update` working
# on the day it goes — the same tolerance ESSENTIAL buys for the files.
DEFINES_SETUP = re.compile(rf"^[ \t]*{SETUP_TASK}[ \t]*=", re.MULTILINE)


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


def normalise(name: str, content: bytes) -> bytes:
    """What is actually compared: the file, less what is not the website's business.

    Line endings are folded together because an editor on Windows can rewrite
    every line of a file without changing a word of it, and a student whose
    editor did that should not be handed a fresh copy — and a fresh .backup
    beside it — every single time they run this.

    The pinned paths in .vscode/settings.json come out for the same reason: they
    are written by this machine and cannot be in the published file, so leaving
    them in would make that one file permanently and unfixably out of date. Both
    sides go through here, so whatever is removed is removed from both and the
    two are still judged on the same thing.
    """
    content = content.replace(b"\r\n", b"\n")
    if name == SETTINGS:
        try:
            content = PINNED.sub("", content.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:          # not text at all; compare it as it is
            pass
    return content


def differs(mine: Path, theirs: bytes, name: str) -> bool:
    """Whether what is on disk is something other than what was published."""
    try:
        return normalise(name, mine.read_bytes()) != normalise(name, theirs)
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
             and differs(folder.joinpath(*name.split("/")), theirs[name], name)]

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

    # PATH first, and then where the installer puts it whether or not this
    # terminal has been told. "pixi is not on PATH" is one of the faults this
    # command exists to repair, and it used to be the one fault that stopped the
    # repair: giving up here sent a student off to type `pixi install` in a
    # terminal that had just proved it cannot find pixi. Running it by its full
    # path builds the environment anyway, and the `pixi run check` at the end
    # runs .pin_shell_path.sh, which is what puts pixi on PATH for every
    # terminal opened afterwards.
    found = shutil.which("pixi")
    pixi = Path(found) if found else next(iter(pixi_locations()), None)
    if pixi is None:
        echo("Could not find pixi, on PATH or where its installer puts it.")
        echo("Install it again from https://pixi.sh, then run `im update` again.")
        return 1
    if found is None:
        echo(f"This terminal cannot see pixi, so using the one at {pixi}.\n")

    result = subprocess.run([pixi, "install"], cwd=folder)
    if result.returncode != 0:
        echo("\n`pixi install` did not finish cleanly. Bring the message above to class.")
        return result.returncode

    # `pixi install` builds the environment and stops there. Two of the things
    # that make it usable are tasks rather than packages, and a task only
    # happens when something runs it — and the refresh above has just undone
    # both of them.
    #
    # The kernel a student picks in a notebook was installed into the
    # environment prefix, which pixi may have rebuilt a moment ago from the lock
    # file this command replaced. The paths telling VS Code where pixi and that
    # environment are live in .vscode/settings.json, which is replaced with the
    # published copy whenever the website changes it, and no published copy can
    # carry a path belonging to one machine. Left here, a student who ran this
    # to fix their setup would be handed back the two faults .pin_pixi_path.py
    # exists to prevent.
    #
    # `pixi run check` is the course folder's own name for putting both back,
    # and it ends by checking the environment — which is what this used to
    # finish by asking the student to go and do themselves.
    # No `check` task in the manifest that was just downloaded, so there is
    # nothing to hand back to and nothing to send the student to either: the
    # command that would put the kernel and the paths back is the one that is
    # missing. `im doctor` is the one that always exists.
    if not DEFINES_SETUP.search(theirs["pixi.toml"].decode("utf-8", "replace")):
        echo("\nDone. If something still does not work, run `im doctor`.")
        return 0

    echo("\nSetting up the kernel and VS Code.\n")
    result = subprocess.run([pixi, "run", SETUP_TASK], cwd=folder)
    if result.returncode != 0:
        echo("\nYour files and your environment are up to date, but "
             f"`pixi run {SETUP_TASK}` did not finish cleanly.")
        echo("Bring the message above to class.")
        return result.returncode

    echo("\nDone.")
    return 0
