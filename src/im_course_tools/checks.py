"""Everything `im doctor` looks at, and the plain sentence each answer deserves.

A check reads one thing about the machine and returns a Finding: what it saw,
and, when what it saw is a problem, what to do about it. Nothing here changes
anything. That is deliberate. `im doctor` is what a stuck student runs before
anyone knows what is wrong, often with an instructor reading over their
shoulder, and a command that repairs things while it is looking at them is a
command nobody can safely be told to run.

The advice is written out in full rather than pointing at a page, because a
student reading it is by definition having trouble reaching the website.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import environment, probe, release, security
from .course import MARKER, CourseFolderNotFound, base_url, course_folder

OK, WARN, FAIL = "ok", "warn", "fail"

# The headings a student reads down. A finding names the one it belongs under,
# and the doctor prints them in the order the checks produce them.
MACHINE = "This machine"
TOOL = "The im command"
FOLDER = "Your course folder"
PIXI = "pixi"
ENVIRONMENT = "The course environment"
SECURITY = "Security software"
INTERNET = "Internet access"
EDITOR = "VS Code"

# Where pixi puts the environment it builds from pixi.toml.
ENV_PATH = (".pixi", "envs", "default")

# The file pixi writes inside that environment naming the manifest it was built
# from. It is written at install time and never afterwards, so it still names
# the old folder once the folder has been moved.
ENV_RECORD = ("conda-meta", "pixi")

# Everything `im check` insists on, plus the one package that is not needed to
# import anything but without which a notebook cannot run at all.
PACKAGES = list(environment.REQUIRED) + [("ipykernel", "ipykernel")]

# Run inside the course environment's own Python, so the answer is about that
# environment and not about whichever Python happens to be running `im`.
PACKAGE_PROBE = """
import sys
for module, name in %r:
    try:
        __import__(module)
    except Exception:
        sys.stdout.write(name + "\\n")
"""

# Variables that quietly send downloads somewhere else, or point TLS at a
# different set of certificate authorities. A student has almost never set one
# of these on purpose, and any of them can explain a failure on its own.
PROXY_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "PIP_INDEX_URL", "CONDA_CHANNELS", "CONDA_SSL_VERIFY",
)
CERTIFICATE_VARIABLES = (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "PIP_CERT",
)

# Folders that sync. A pixi environment is tens of thousands of small files and
# belongs in none of them.
CLOUD_FOLDERS = (
    ("OneDrive", ("onedrive",)),
    ("iCloud Drive", ("mobile documents", "com~apple~clouddocs")),
    ("Dropbox", ("dropbox",)),
    ("Google Drive", ("google drive", "googledrive", "cloudstorage")),
)

# Not worth walking into when looking for a lost course folder: large, slow, or
# certain not to hold one.
SKIP_WHEN_LOOKING = frozenset({
    "Library", "Applications", "AppData", "Music", "Pictures", "Movies",
    "node_modules", "Public", "Windows", "Program Files", "Program Files (x86)",
    "Creative Cloud Files", "envs",
})

EXTENSIONS = (("ms-python.python", "Python"), ("ms-toolsai.jupyter", "Jupyter"))

DRIVE_REMOVABLE, DRIVE_REMOTE = 2, 4


@dataclass
class Finding:
    """One thing looked at: what it is, what was seen, and what to do."""

    status: str
    group: str
    title: str
    detail: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)


@dataclass
class Context:
    """What the checks know about the run, and what they learn for each other."""

    system: str
    cwd: Path
    offline: bool = False
    folder: Path | None = None
    env_python: Path | None = None
    pixi: str | None = None
    # The look at the network, kept because two checks read it. None until it
    # has been taken, which is not the same as taken and found nothing.
    probes: list[probe.Probe] | None = None
    download: probe.Download | None = None
    # The title of the finding that has already explained inspected traffic, so
    # that the next one to want that paragraph points at it instead.
    explained: str | None = None


# --- small things the checks need ------------------------------------------ #

def run_briefly(command, timeout: float = 20.0) -> str | None:
    """Run something that should answer at once, and give up quietly if it does not."""
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


def is_dir(path: Path) -> bool:
    """Whether this is a folder, with "the question timed out" counting as no.

    A path inside OneDrive, iCloud or a network share can answer a question as
    ordinary as this one with an error, or after half a minute, because
    answering it means asking a server. Every look at an unfamiliar folder here
    goes through this.
    """
    try:
        return path.is_dir()
    except OSError:
        return False


def is_file(path: Path) -> bool:
    """The same, for files."""
    try:
        return path.is_file()
    except OSError:
        return False


def under_rosetta() -> bool:
    """Whether this terminal is the Intel one, emulated on an Apple Silicon Mac."""
    if sys.platform != "darwin" or platform.machine() != "x86_64":
        return False
    return (run_briefly(["sysctl", "-n", "sysctl.proc_translated"], 5) or "").strip() == "1"


def long_paths_enabled() -> bool:
    """Whether Windows has been told to allow paths longer than 260 characters."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    except OSError:
        return False
    return bool(value)


def drive_type(path: Path) -> int | None:
    """What kind of drive a Windows path is on: local, removable or network."""
    try:
        import ctypes
    except ImportError:
        return None
    drive = path.drive
    if not drive:
        return None
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\"))
    except (AttributeError, OSError, ValueError):
        return None


def syncing_desktop(path: Path, home: Path | None = None) -> bool:
    """Whether this path is under a Desktop or Documents that iCloud is syncing.

    macOS does not put those two inside the iCloud folder when it syncs them;
    it leaves them where they were and moves the storage underneath. So the
    path gives nothing away, and the only way to tell is to look for the copy
    iCloud keeps.
    """
    home = home or Path.home()
    cloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    for name in ("Desktop", "Documents"):
        try:
            path.relative_to(home / name)
        except ValueError:
            continue
        if is_dir(cloud / name):
            return True
    return False


def likely_course_folders(home: Path | None = None, depth: int = 2,
                          budget: int = 800, seconds: float = 5.0,
                          limit: int = 4) -> list[Path]:
    """Folders that look like the course folder, for a student who has lost theirs.

    Told "this is not your course folder", a student's next question is always
    "where is it, then". A bounded look through the few places a download
    actually lands answers it far more often than not, and costs a fraction of
    a second when it does not.

    Bounded twice over, by folders looked at and by the clock, because the
    folders most worth looking in are the synced ones, and a synced folder can
    take a server's worth of time to answer. Coming back with nothing after a
    few seconds is a much better failure than appearing to hang.
    """
    home = Path(home) if home else Path.home()
    deadline = time.monotonic() + seconds

    roots = [home]
    try:
        roots += [p for p in sorted(home.glob("OneDrive*")) if is_dir(p)]
    except OSError:
        pass
    for base in list(roots):
        for name in ("Desktop", "Documents", "Downloads"):
            if is_dir(base / name):
                roots.append(base / name)

    found: list[Path] = []
    seen: set[Path] = set()
    queue = [(root, 0) for root in roots]
    while queue and budget > 0 and time.monotonic() < deadline:
        place, level = queue.pop(0)
        try:
            resolved = place.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        budget -= 1
        try:
            entries = sorted(place.iterdir())
        except OSError:
            continue
        if any(e.name == MARKER and is_file(e) for e in entries):
            found.append(place)
            continue                    # a course folder does not hold another
        if level < depth:
            for entry in entries:
                if entry.name.startswith(".") or entry.name in SKIP_WHEN_LOOKING:
                    continue
                if is_dir(entry):
                    queue.append((entry, level + 1))

    def looks_like_it(place: Path) -> int:
        lowered = place.name.lower()
        return 0 if any(w in lowered for w in ("instructing", "machines", "course")) else 1

    found.sort(key=lambda p: (looks_like_it(p), len(str(p))))
    return found[:limit]


def search_briefly(seconds: float = 4.0) -> list[Path]:
    """`likely_course_folders`, abandoned if it takes longer than it is worth.

    The folders most worth looking in are the synced ones, and a single
    question about a synced folder can block for a long time while it asks a
    server. A deadline the search checks between folders cannot help with
    that, because the search is not running while it waits. So the search goes
    on a thread of its own and the answer is collected when it is ready, or
    given up on when it is not. Coming back with nothing after four seconds is
    a far better failure than appearing to hang.
    """
    answer: list[Path] = []

    def look() -> None:
        try:
            answer.extend(likely_course_folders(seconds=seconds))
        except Exception:               # a guess is never worth an exception
            pass

    worker = threading.Thread(target=look, daemon=True)
    worker.start()
    worker.join(seconds + 1)
    return list(answer)


def missing_packages(python: Path, timeout: float = 120.0) -> list[str] | None:
    """The course packages that this Python cannot import, or None if it would not say."""
    output = run_briefly([python, "-c", PACKAGE_PROBE % (PACKAGES,)], timeout)
    if output is None:
        return None
    return [line.strip() for line in output.splitlines() if line.strip()]


def built_for(env: Path) -> Path | None:
    """The folder pixi built this environment for, if the environment says.

    pixi stamps the manifest's full path into the environment when it builds
    it, and that stamp is the only thing on disk that still remembers where the
    folder was standing at the time. Older environments, and any built by conda
    rather than pixi, carry no stamp; those get None and no opinion.
    """
    try:
        record = json.loads(env.joinpath(*ENV_RECORD).read_text(encoding="utf-8"))
        manifest = record["manifest_path"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return Path(manifest).parent


def same_folder(one: Path, other: Path) -> bool:
    """Whether two paths are the same folder, however differently they are written.

    Asking the filesystem is the only answer that survives a symlinked home
    folder or a drive letter in the other case; it only works while both paths
    exist, and when one of them no longer does, the folder has moved, which is
    the answer anyway.
    """
    try:
        return os.path.samefile(one, other)
    except OSError:
        return os.path.normcase(str(one)) == os.path.normcase(str(other))


# What the two checks that care about the network make of it between them.
CLEAR = "clear"       # pixi's downloads arrive, signed by authorities pixi knows
TROUBLE = "trouble"   # something is intercepting them, blocking them, or losing them
UNSEEN = "unseen"     # nobody looked, so neither of those can be claimed


def looked_at_the_network(ctx: Context):
    """The one look at the network, taken once and read by two checks.

    The security check cannot say whether what is installed is in the way until
    somebody has looked, and the internet check has to look anyway. Asking
    twice would both double the slowest part of the command and leave two
    answers free to disagree with each other.
    """
    if ctx.probes is None:
        if ctx.offline:
            ctx.probes, ctx.download = [], probe.Download(probe.NOT_TRIED)
        else:
            hosts = list(probe.HOSTS)
            site = urllib.parse.urlsplit(base_url()).hostname
            if site:
                hosts.append(site)
            ctx.probes = probe.probe_all(hosts)
            # If not one host answered there is nothing for pixi to download
            # from either, and asking it anyway costs three retries and most of
            # a minute to be told what is already on the screen.
            silent = all(result.outcome in (probe.UNRESOLVED, probe.UNREACHABLE)
                         for result in ctx.probes)
            ctx.download = (probe.Download(probe.NOT_TRIED) if silent
                            else probe.pixi_download(ctx.pixi))
    return ctx.probes, ctx.download


def network_verdict(probes, download) -> str:
    """Whether anything is standing between pixi and the internet.

    Being installed is not evidence of anything. Most laptops in a lecture
    theatre carry security software and most of them install the course
    environment without it ever mattering, so what turns "you have Bitdefender"
    into something worth doing is a certificate pixi refuses or a download that
    does not arrive.
    """
    # A machine with no connection at all says nothing about what would happen
    # to a connection it had, and blaming the antivirus for switched-off wifi
    # is exactly the kind of confident wrong answer this is trying to avoid.
    if probes and all(result.outcome in (probe.UNRESOLVED, probe.UNREACHABLE)
                      for result in probes):
        return UNSEEN

    intercepted = any(result.outcome == probe.INTERCEPTED for result in probes)
    blocked = any(result.outcome in (probe.UNRESOLVED, probe.UNREACHABLE)
                  for result in probes)
    if download.ok:
        # pixi got through, which settles the certificate question in pixi's
        # own terms: the probes above are opened by Python, and a Python that
        # cannot find its own list of authorities makes every host look
        # tampered with on a machine where nothing is. A certificate signed by
        # a product by name, and a host that never answered, are still worth
        # asking about; a certificate this Python could not check is not.
        return TROUBLE if intercepted or blocked else CLEAR
    if download.failed or any(not result.ok for result in probes):
        return TROUBLE
    return CLEAR if probes else UNSEEN


def interception_title(vendors) -> str:
    """The line the internet check prints when a certificate names a product.

    Known here as well as there because a check that decides not to explain
    something has to be able to say where the explanation went instead.
    """
    return f"{' and '.join(vendors)} is opening pixi's downloads on the way in"


def scanning_advice(ctx: Context, products, title: str) -> list[str]:
    """The paragraph that explains inspected traffic, named after what is here.

    Written out once per run and pointed at after that. The security check and
    the internet checks reach this advice from different evidence and are
    frequently both right at the same time, and a student who meets the same
    twenty lines twice in one numbered list learns nothing the second time.

    Which of them writes it out is settled by `ctx.explained`, holding the
    title of the finding it belongs to. A check that knows another finding will
    make the point better can name that one there before asking for its own
    advice, and gets the pointer instead.
    """
    named = " and ".join(products) if products else "your antivirus"
    if ctx.explained and ctx.explained != title:
        return textwrap.wrap(f'This is the same thing as "{ctx.explained}" in this '
                             "list, and what to change is listed there.", 70)
    ctx.explained = title
    return [
        "Security software that inspects encrypted traffic is the most common",
        "reason `pixi install` fails on a laptop whose browser works perfectly.",
        "It opens pixi's connections to read what is inside and signs them again",
        "with a certificate of its own. A browser accepts that, because the",
        "product added itself to the list of authorities the operating system",
        "trusts. pixi carries its own list, which nothing can be added to, so it",
        "refuses the connection and reports a network error instead. The same",
        "software also locks files while pixi is unpacking them, which is why a",
        "failing install can fail somewhere different every time.",
        "",
        f"If pixi is failing, in {named}:",
        "",
        "  - add your course folder to whatever it calls exclusions, exceptions",
        "    or allowed folders",
        "  - turn off HTTPS or SSL scanning (also called encrypted connection",
        "    scanning, web shield, web protection or network protection)",
        "  - or pause protection for ten minutes, run `pixi install` in the course",
        "    folder, and turn it back on afterwards",
    ]


# --- the checks themselves -------------------------------------------------- #

def machine_check(ctx: Context) -> Finding:
    """Which operating system this is, and which Python is running the command."""
    detail = [f"Python {platform.python_version()} at {sys.executable}"]

    if ctx.system == "Darwin":
        title = f"macOS {platform.mac_ver()[0] or 'unknown'} on {platform.machine()}"
        if under_rosetta():
            return Finding(WARN, MACHINE, title, detail, [
                "This terminal is the Intel one, being emulated on an Apple Silicon",
                "Mac. pixi believes it is on an Intel Mac and installs the Intel",
                "build of everything, which is slower and occasionally incomplete.",
                "",
                "Find Terminal (or VS Code) in Applications, right-click it, choose",
                "Get Info and untick 'Open using Rosetta'. Quit it, open it again,",
                "and run `pixi install` in your course folder.",
            ])
        return Finding(OK, MACHINE, title, detail)

    if ctx.system == "Windows":
        release, version = platform.win32_ver()[:2]
        title = f"Windows {release} {version} on {platform.machine()}".replace("  ", " ")
        return Finding(OK, MACHINE, title, detail)

    return Finding(WARN, MACHINE,
                   f"{platform.system()} {platform.release()} on {platform.machine()}",
                   detail, [
                       "The course is taught on macOS and Windows, and this is neither,",
                       "so the checks below know less about this machine than they",
                       "would about one of those. Everything may well be fine. Bring",
                       "anything that looks odd to class.",
                   ])


def version_check(ctx: Context) -> Finding:
    """Which `im` this is, how it got onto the machine, and whether it is current.

    Only ever the answer already remembered from an earlier run, never a fresh
    question: the command has one deliberate reason to touch the network and
    this is not it, and by the time the checks run `im doctor` has asked
    already.
    """
    install = release.describe()
    detail = [f"{install.described}, in {install.prefix}"]
    available = release.known_latest()

    if release.newer(available, install.version):
        advice = ["A fix to `im` itself reaches you through a release, and this",
                  "one has not arrived here yet."]
        prepared = release.upgrade_command(install)
        if prepared is None:
            advice.append(f"This copy is {install.described}, which cannot be upgraded")
            advice.append("from here. Please bring that to class.")
        else:
            advice += ["", "Upgrade it with:", ""]
            advice += [f"    {line}" for line in release.as_typed(prepared)]
        return Finding(WARN, TOOL, f"im {install.version}, and {available} is out",
                       detail, advice)

    if install.kind == release.SOURCE:
        return Finding(WARN, TOOL, f"im {install.version}, run from a checkout", detail, [
            "The code being run is not the code that was installed, so nothing",
            "would change if it were upgraded. That is right for whoever is",
            "working on `im` and wrong for a student.",
        ])

    return Finding(OK, TOOL, f"im {install.version}", detail)


def folder_check(ctx: Context) -> Finding:
    """Whether this is the course folder, and where it is instead if it is not."""
    try:
        ctx.folder = course_folder(ctx.cwd)
    except CourseFolderNotFound:
        ctx.folder = None

    if ctx.folder is not None:
        return Finding(OK, FOLDER, str(ctx.folder))

    advice = [
        f"Nothing here, and nothing in any folder above it, has a {MARKER} in it.",
        "`im get` and `im update` have nowhere to put anything from here, and",
        "most of the checks below have nothing to look at.",
        "",
    ]
    guesses = search_briefly()
    if guesses:
        advice.append("Your course folder looks like it is one of these. Change into")
        advice.append("it and run `im doctor` again:")
        advice.append("")
        advice.extend(f'    cd "{guess}"' for guess in guesses)
    else:
        advice.append("Open your course folder in VS Code and use the terminal there")
        advice.append("(Terminal -> New Terminal). It always starts in the right place.")
    return Finding(FAIL, FOLDER, "You are not in your course folder",
                   [f"You are in {ctx.cwd}"], advice)


def cloud_finding(ctx: Context, path: Path) -> Finding | None:
    """Whether the course folder is inside something that syncs it to the cloud."""
    parts = [part.lower() for part in path.parts]
    service = None
    for name, fragments in CLOUD_FOLDERS:
        if any(fragment in part for part in parts for fragment in fragments):
            service = name
            break
    if service is None and ctx.system == "Darwin" and syncing_desktop(path):
        service = "iCloud Drive"
    if service is None:
        return None

    elsewhere = Path.home() / path.name
    move = f'move "{path}" "{elsewhere}"' if ctx.system == "Windows" \
        else f'mv "{path}" "{elsewhere}"'
    return Finding(WARN, FOLDER, f"Your course folder is inside {service}", [str(path)], [
        "A pixi environment is tens of thousands of small files, and a folder",
        f"that syncs will try to upload every one of them. That fills up {service},",
        "makes everything slow, and now and then holds a file open at the moment",
        "pixi is writing it — which is why an install can fail differently each",
        "time it is run.",
        "",
        "Move the whole course folder somewhere that does not sync, then install",
        "again in its new home:",
        "",
        f"    {move}",
        f'    cd "{elsewhere}"',
        "    pixi install",
    ])


def letters_finding(ctx: Context, path: Path) -> Finding | None:
    """Whether the path has letters in it that the tools underneath pixi mishandle."""
    odd = sorted({character for character in str(path) if ord(character) > 127})
    if not odd:
        return None
    elsewhere = "C:\\im-course" if ctx.system == "Windows" else "/Users/Shared/im-course"
    move = f'move "{path}" {elsewhere}' if ctx.system == "Windows" \
        else f'mv "{path}" {elsewhere}'
    return Finding(WARN, FOLDER, "The path has letters in it that some tools mishandle",
                   [str(path), "The letters are: " + " ".join(odd)], [
                       "Some of the tools underneath pixi still assume plain English",
                       "letters in a path. This one has " + " ".join(odd) + " in it, usually",
                       "because the user name does. Most of the time it works; when it",
                       "does not, the error says nothing about letters at all.",
                       "",
                       "If pixi keeps failing, move the course folder somewhere plainer:",
                       "",
                       f"    {move}",
                   ])


def length_finding(ctx: Context, path: Path) -> Finding | None:
    """Whether Windows will run out of path before pixi runs out of folders."""
    if ctx.system != "Windows":
        return None
    length = len(str(path))
    enabled = long_paths_enabled()
    if enabled:
        if length < 160:
            return None
        status = WARN
    elif length > 120:
        status = FAIL
    elif length >= 80:
        status = WARN
    else:
        return None

    return Finding(status, FOLDER, "The path to your course folder is long",
                   [f"{length} characters: {path}",
                    f"Long path support is {'on' if enabled else 'off'}"], [
                       "Unless it is told otherwise, Windows refuses to open a file whose",
                       "whole path is longer than 260 characters. A pixi environment",
                       "buries files well over a hundred characters deep inside the folder",
                       f"it lives in, and this folder is already {length} characters in.",
                       "",
                       "Move the course folder near the top of the drive:",
                       "",
                       f'    move "{path}" C:\\im-course',
                       "    cd C:\\im-course",
                       "    pixi install",
                   ])


def drive_finding(ctx: Context, path: Path) -> Finding | None:
    """Whether the course folder is on a drive pixi should not be building on."""
    if ctx.system != "Windows":
        return None
    if str(path).startswith("\\\\"):
        kind = "a network share"
    else:
        types = {DRIVE_REMOTE: "a network drive", DRIVE_REMOVABLE: "a removable drive"}
        kind = types.get(drive_type(path))
    if kind is None:
        return None
    return Finding(WARN, FOLDER, f"Your course folder is on {kind}", [str(path)], [
        f"pixi builds an environment out of very many small files, and {kind}",
        "is slow enough at that to look like a hang, and it disappears when the",
        "drive does. Copy the course folder onto this computer's own disk, for",
        "example into your user folder, and run `pixi install` there.",
    ])


def path_checks(ctx: Context) -> list[Finding]:
    """The four ways a perfectly ordinary-looking folder path breaks pixi."""
    if ctx.folder is None:
        return []
    found = [finding for finding in (
        cloud_finding(ctx, ctx.folder),
        letters_finding(ctx, ctx.folder),
        length_finding(ctx, ctx.folder),
        drive_finding(ctx, ctx.folder),
    ) if finding is not None]
    if found:
        return found
    return [Finding(OK, FOLDER, "Its path has nothing in it that trips pixi up")]


def writable_check(ctx: Context) -> Finding | None:
    """Whether a file can actually be created in the course folder."""
    if ctx.folder is None:
        return None
    probe_file = ctx.folder / ".im-doctor-write-test"
    try:
        probe_file.write_text("checking", encoding="utf-8")
    except OSError as error:
        if ctx.system == "Windows":
            advice = [
                "Something is refusing writes into this folder. On Windows that is",
                "almost always one of two things:",
                "",
                "  - Controlled folder access, which protects Documents and Desktop.",
                "    Windows Security -> Virus & threat protection -> Ransomware",
                "    protection -> Allow an app through controlled folder access.",
                "  - Third-party antivirus with the folder under protection.",
                "",
                "Failing that, move the course folder into your own user folder,",
                "where you certainly may write.",
            ]
        else:
            advice = [
                "Something is refusing writes into this folder. On a Mac that is",
                "usually the privacy permission for the app you are typing in:",
                "System Settings -> Privacy & Security -> Files and Folders, and",
                "give Terminal (or VS Code) access to the folder it is in.",
                "",
                "Failing that, move the course folder into your home folder.",
            ]
        return Finding(FAIL, FOLDER, "Nothing can be written into your course folder",
                       [str(error)], advice)
    finally:
        try:
            probe_file.unlink()
        except OSError:
            pass
    return Finding(OK, FOLDER, "It can be written to")


def disk_check(ctx: Context) -> Finding | None:
    """Whether there is room for an environment that is measured in gigabytes."""
    place = ctx.folder or ctx.cwd
    try:
        usage = shutil.disk_usage(place)
    except OSError as error:
        return Finding(WARN, FOLDER, "Could not work out how much disk space is left",
                       [str(error)])
    free = usage.free / 1_000_000_000
    detail = [f"{free:.1f} GB free of {usage.total / 1_000_000_000:.0f} GB"]
    room = [
        "The course environment is a few gigabytes on its own, and pixi keeps a",
        "download cache in your home folder that is a few more. Empty the",
        "Downloads folder and the Trash, and if you have installed other pixi",
        "environments you no longer need, delete their .pixi folders.",
    ]
    if free < 3:
        return Finding(FAIL, FOLDER, "There is not enough disk space left", detail, room)
    if free < 8:
        return Finding(WARN, FOLDER, "Disk space is getting tight", detail, room)
    return Finding(OK, FOLDER, "There is room for the environment", detail)


def pixi_check(ctx: Context) -> Finding:
    """Whether pixi is installed, and whether this terminal can see it."""
    executable = shutil.which("pixi")
    if executable is not None:
        # Kept for the network check, which asks this pixi in particular to
        # download something rather than looking it up again.
        ctx.pixi = executable
        version = (run_briefly([executable, "--version"], 15) or "").strip()
        return Finding(OK, PIXI, version or "pixi is installed", [executable])

    already = security.pixi_locations()
    if already:
        return Finding(FAIL, PIXI, "pixi is installed, but this terminal cannot see it",
                       [f"It is at {already[0]}"], [
                           "The installer adds pixi to your PATH, and a terminal only reads",
                           "its PATH when it starts. This one started before that happened.",
                           "",
                           "Close this terminal completely and open a new one, then run",
                           "`im doctor` again. In VS Code, close the terminal panel with the",
                           "bin icon and open a new terminal rather than reusing this one.",
                       ])

    install = ('powershell -ExecutionPolicy Bypass -c "irm -useb https://pixi.sh/install.ps1 | iex"'
               if ctx.system == "Windows" else "curl -fsSL https://pixi.sh/install.sh | sh")
    return Finding(FAIL, PIXI, "pixi is not installed", [], [
        "pixi is what builds the course environment, so nothing else can work",
        "until it is there. Install it with:",
        "",
        f"    {install}",
        "",
        "Then close the terminal, open a new one, and run `im doctor` again.",
    ])


def environment_check(ctx: Context) -> list[Finding]:
    """Whether the environment pixi.toml describes has actually been built."""
    if ctx.folder is None:
        return []
    env = ctx.folder.joinpath(*ENV_PATH)
    for candidate in (env / "bin" / "python", env / "python.exe",
                      env / "Scripts" / "python.exe"):
        if is_file(candidate):
            ctx.env_python = candidate
            break

    if ctx.env_python is None:
        return [Finding(FAIL, ENVIRONMENT, "It has not been installed yet",
                        [f"There is no {env}"], [
                            "In your course folder, run:",
                            "",
                            "    pixi install",
                            "",
                            "It downloads a few gigabytes the first time, so leave it several",
                            "minutes before deciding it has stopped.",
                        ])]

    findings = [Finding(OK, ENVIRONMENT, "It is installed", [str(env)])]
    manifest, lock = ctx.folder / MARKER, ctx.folder / "pixi.lock"
    if not lock.exists():
        findings.append(Finding(WARN, ENVIRONMENT, "There is no pixi.lock", [], [
            "Without it, pixi solves the environment from scratch and may not",
            "build the same one everyone else has. Run `im update` to fetch the",
            "course's current pixi.toml and pixi.lock.",
        ]))
    elif manifest.exists() and manifest.stat().st_mtime > lock.stat().st_mtime + 1:
        findings.append(Finding(WARN, ENVIRONMENT, "pixi.toml has changed since pixi.lock was made",
                                [], [
                                    "The environment on disk may not be the one pixi.toml now asks",
                                    "for. Run `pixi install` in your course folder to catch it up,",
                                    "or `im update` to take the course's current pair of files.",
                                ]))
    return findings


def moved_check(ctx: Context) -> Finding | None:
    """Whether the environment was built where the course folder now stands.

    A pixi environment is not portable: the notebook kernel, and every command
    installed into it, hold the folder's full path in plain text. Moving,
    renaming or copying the course folder leaves all of them pointing at a
    place that is not there, and the errors that follow name files a student
    has never heard of rather than the folder they dragged last week.
    """
    if ctx.folder is None or ctx.env_python is None:
        return None
    origin = built_for(ctx.folder.joinpath(*ENV_PATH))
    if origin is None:
        return None
    if same_folder(origin, ctx.folder):
        return Finding(OK, ENVIRONMENT, "It was built for this folder")
    return Finding(FAIL, ENVIRONMENT, "It was built for a different folder",
                   [f"It was built for {origin}", f"but the course folder is {ctx.folder}"], [
                       "The course folder has been moved, renamed, or copied since the",
                       "environment was installed, and the environment did not come with",
                       "it. It holds the old path in hundreds of places, among them the",
                       "kernel every notebook starts, so notebooks and commands fail",
                       "saying a file or an interpreter is not there.",
                       "",
                       "Build it again where the folder is now. In your course folder, run:",
                       "",
                       "    pixi clean",
                       "    pixi install",
                       "",
                       "That throws the environment away and installs it from pixi.lock,",
                       "which downloads a few gigabytes and takes several minutes. None of",
                       "your own work is in the environment, so none of it is touched.",
                       "",
                       "If pixi does not know the `clean` command, delete the `.pixi`",
                       "folder inside your course folder yourself and run `pixi install`.",
                   ])


def packages_check(ctx: Context) -> Finding | None:
    """Whether the course environment can import everything the course uses."""
    if ctx.env_python is None:
        return None
    missing = missing_packages(ctx.env_python)
    if missing is None:
        return Finding(WARN, ENVIRONMENT, "Could not ask it which packages it has",
                       [f"{ctx.env_python} did not answer"], [
                           "The environment is there but its Python would not run. Rebuild it",
                           "by running `pixi install` in your course folder, and bring the",
                           "message it prints to class if it fails.",
                       ])
    if not missing:
        return Finding(OK, ENVIRONMENT, "Everything the course needs is in it")
    return Finding(FAIL, ENVIRONMENT, f"{len(missing)} of its packages are missing", missing, [
        "Run `im update` in your course folder. It fetches the course's current",
        "pixi.toml and pixi.lock and installs them, which is what puts these back.",
        "",
        "If that fails on the download, the internet checks below are where the",
        "reason will be.",
    ])


def interpreter_check(ctx: Context) -> Finding | None:
    """Whether the `im` being run is the one inside the course environment."""
    if ctx.folder is None or ctx.env_python is None:
        return None
    env = ctx.folder.joinpath(*ENV_PATH)
    try:
        inside = Path(sys.prefix).resolve() == env.resolve()
    except OSError:
        inside = False
    if inside:
        return Finding(OK, ENVIRONMENT, "`im` is running from inside it")
    return Finding(WARN, ENVIRONMENT, "`im` is running from outside it",
                   [f"This `im` runs on {sys.executable}", f"The environment is {env}"], [
                       "That is fine for `im doctor`, which looks at your course",
                       "environment directly rather than from inside it. It is not fine",
                       "for `im check`, which can only see the packages in the Python it",
                       "is itself running on, and would report things missing that are",
                       "installed a folder away.",
                       "",
                       "Run that one through pixi, from your course folder:",
                       "",
                       "    pixi run im check",
                   ])


def security_check(ctx: Context) -> list[Finding]:
    """What security software is installed, and whether it is actually in the way.

    Installed is not the same as guilty, and this check used to treat it as if
    it were: every student carrying Bitdefender was handed a job of work —
    exclusions, scanning turned off, protection paused — including the large
    majority whose install had failed for some entirely unrelated reason, and a
    numbered list that tells a hundred people to do something only three of
    them need is a list nobody reads to the bottom of.

    So the paragraph now waits for evidence, which is what the internet checks
    collect: a certificate pixi refuses, or a download of pixi's own that never
    arrives. Until then what is installed is a line in the scan and nothing
    more.
    """
    findings: list[Finding] = []
    result = security.survey(ctx.system)
    probes, download = looked_at_the_network(ctx)
    verdict = network_verdict(probes, download)

    if not result.asked:
        if ctx.system == "Windows" and verdict == CLEAR:
            findings.append(Finding(OK, SECURITY, "Windows would not say what is running",
                                    ["Whatever is here, pixi's downloads are getting past it"]))
        elif ctx.system == "Windows":
            findings.append(Finding(WARN, SECURITY, "Windows would not say what is running",
                                    ["The Security Center could not be asked"], [
                                        "If `pixi install` is failing with a certificate or network",
                                        "error, look at your antivirus settings first: they are the",
                                        "most common cause and this check could not rule them out.",
                                    ]))
    elif result.third_party:
        # One product is worth naming in the line a student reads; three are a
        # list, and the list belongs underneath it.
        named = (result.third_party[0] if len(result.third_party) == 1
                 else "Third-party security software")
        listed = result.third_party if len(result.third_party) > 1 else []

        if verdict == TROUBLE:
            title = f"{named} may be why pixi cannot get through"
            # A certificate that names the product outright is better evidence
            # than a folder on disk, and the internet check is about to present
            # it. Where there is one, it does the explaining and this line
            # points at it rather than saying the same thing first.
            vendors = sorted({seen.vendor for seen in probes
                              if seen.outcome == probe.INTERCEPTED and seen.vendor})
            if vendors:
                ctx.explained = interception_title(vendors)
            findings.append(Finding(WARN, SECURITY, title, listed,
                                    scanning_advice(ctx, result.third_party, title)))
        elif verdict == CLEAR:
            # Only one of these two is evidence about pixi itself, so only one
            # of them gets to say so.
            settled = (f"{named} is installed, and is letting pixi through",
                       "pixi's own download went through") if download.ok else \
                      (f"{named} is installed, and nothing suggests it is in the way",
                       "Every certificate came from a public authority")
            findings.append(Finding(OK, SECURITY, settled[0], listed + [settled[1]]))
        else:
            why = ("--offline was asked for" if ctx.offline
                   else "nothing could be reached to test it against")
            findings.append(Finding(WARN, SECURITY, f"{named} is installed", listed, [
                *textwrap.wrap(f"Whether it is in pixi's way was not checked, "
                               f"because {why}.", 70),
                "",
                "Run `im doctor` again with a working connection and without",
                "--offline. It looks at who signed pixi's connections and makes",
                "pixi fetch a file itself, which is what tells apart security",
                "software that is interfering from security software that is",
                "merely installed.",
            ]))
    elif result.built_in:
        findings.append(Finding(OK, SECURITY,
                                f"{result.built_in[0]} only, which does not get in the way"))
    else:
        findings.append(Finding(OK, SECURITY, "Nothing found that inspects pixi's traffic"))

    if ctx.system == "Windows" and security.controlled_folder_access():
        findings.append(Finding(WARN, SECURITY, "Controlled folder access is turned on", [], [
            "Windows is refusing writes into Documents, Desktop and a few other",
            "folders by any program it does not recognise, and it does not",
            "recognise pixi. If your course folder is in one of those, installing",
            "will fail with permission errors.",
            "",
            "Windows Security -> Virus & threat protection -> Ransomware",
            "protection -> Allow an app through controlled folder access, and add",
            "pixi. Or keep the course folder outside those folders.",
        ]))
    return findings


def proxy_check(ctx: Context) -> Finding | None:
    """Settings in this terminal that send downloads somewhere else."""
    proxies = [(name, os.environ[name]) for name in PROXY_VARIABLES if os.environ.get(name)]
    bundles = [(name, os.environ[name]) for name in CERTIFICATE_VARIABLES if os.environ.get(name)]
    if not proxies and not bundles:
        return None

    detail = [f"{name}={value}" for name, value in proxies + bundles]
    broken = [name for name, value in bundles if not Path(value).exists()]
    if broken:
        return Finding(FAIL, INTERNET, "A certificate file is set that does not exist",
                       detail, [
                           f"{', '.join(broken)} points at a file that is not there, and every",
                           "download that checks a certificate will fail because of it.",
                           "",
                           "Unset it in this terminal and try again:",
                           "",
                           *(f"    unset {name}" if ctx.system != "Windows"
                             else f"    Remove-Item Env:{name}" for name in broken),
                       ])
    return Finding(WARN, INTERNET, "This terminal redirects downloads", detail, [
        "These settings send downloads through somewhere else, or point at a",
        "different list of certificate authorities. If you did not set them",
        "deliberately — a university network guide, or another course, often",
        "does — they can break pixi on their own while a browser stays happy.",
    ])


def network_check(ctx: Context) -> list[Finding]:
    """Whether pixi's downloads can arrive, and arrive unopened."""
    if ctx.offline:
        return [Finding(WARN, INTERNET, "Not checked, because --offline was asked for", [], [
            "The internet checks are the ones that find downloads being blocked",
            "or opened on the way in, which is what most broken installs turn out",
            "to be. Run `im doctor` without --offline once you have a connection.",
        ])]

    results, download = looked_at_the_network(ctx)
    if not results:
        return []

    grouped: dict[str, list] = {}
    for result in results:
        grouped.setdefault(result.outcome, []).append(result)
    reached = grouped.get(probe.REACHED, [])
    unresolved = grouped.get(probe.UNRESOLVED, [])
    unreachable = grouped.get(probe.UNREACHABLE, [])

    if len(unresolved) + len(unreachable) == len(results):
        return [Finding(FAIL, INTERNET, "This machine cannot reach the internet at all",
                        [f"None of {len(results)} hosts answered"], [
                            "Nothing pixi does will work until this does. Check wifi, and if",
                            "you are on a university network that asks you to sign in through",
                            "a web page, open a browser and sign in first.",
                            "",
                            "If the browser works and this does not, something on this",
                            "machine is blocking programs other than the browser — a firewall",
                            "or the network protection part of an antivirus.",
                        ])]

    findings: list[Finding] = []

    intercepted = grouped.get(probe.INTERCEPTED, [])
    vendors = sorted({result.vendor for result in intercepted if result.vendor})
    if intercepted:
        title = interception_title(vendors)
        findings.append(Finding(
            FAIL, INTERNET, title,
            [f"{result.host}: the certificate says it was issued by {result.issuer}"
             for result in intercepted],
            scanning_advice(ctx, vendors, title)))

    unverified = grouped.get(probe.UNVERIFIED, [])
    if unverified and download.ok:
        # pixi fetched a file from one of these same hosts while this was being
        # checked, so nothing is sitting between the machine and the internet.
        # What cannot check a certificate is the Python running `im`, which is
        # a real fault and a much smaller one.
        findings.append(Finding(
            WARN, INTERNET, "The Python running `im` cannot verify certificates",
            [f"{result.host}: {result.error}" for result in unverified], [
                "pixi downloaded a file of its own from one of these hosts while",
                "this was being checked, so this is not something standing between",
                "the machine and the internet. It is this Python looking for the",
                "list of certificate authorities somewhere it is not.",
                "",
                "`pixi install` does not use it and is unaffected. `im get` and",
                "`im update` do, and will fail. Rebuilding the environment with",
                "`pixi install` in your course folder puts a fresh list back.",
            ]))
    elif unverified:
        findings.append(Finding(
            FAIL, INTERNET, "The certificates for some hosts could not be verified",
            [f"{result.host}: {result.error}" for result in unverified], [
                "This is what antivirus that inspects encrypted traffic looks like",
                "from the outside, and it is also what a university proxy and a",
                "badly wrong clock look like. In that order of likelihood:",
                "",
                "  - pause your antivirus for ten minutes and run `im doctor` again",
                "  - check the clock and time zone on this machine",
                "  - if you are on a network that signs you in through a web page,",
                "    open a browser and sign in first",
            ]))

    unknown = grouped.get(probe.UNKNOWN_CA, [])
    if unknown:
        findings.append(Finding(
            WARN, INTERNET, "Some certificates were signed by an unfamiliar authority",
            [f"{result.host}: issued by {result.issuer}" for result in unknown], [
                "The connection worked, but the name that vouched for it is not one",
                "of the public authorities. Usually that means something on this",
                "machine or this network is inspecting encrypted traffic. pixi",
                "trusts its own built-in list, so it may refuse what your browser",
                "has just accepted.",
                "",
                "If pixi is failing, pause your antivirus for ten minutes and run",
                "`im doctor` again to see whether this line changes.",
            ]))

    if unresolved:
        findings.append(Finding(
            FAIL, INTERNET, "Some of the names pixi needs could not be looked up",
            [f"{result.host}: {result.error}" for result in unresolved], [
                "Other hosts answered, so this is not the internet being down. It is",
                "either a DNS setting or something blocking these names in",
                "particular. Try another network — a phone hotspot is the quickest",
                "test — and if it works there, the block is on this one.",
            ]))

    if unreachable:
        findings.append(Finding(
            FAIL, INTERNET, "Some of the hosts pixi needs did not answer",
            [f"{result.host}: {result.error}" for result in unreachable], [
                "Other hosts answered, so the internet itself is fine and these are",
                "being blocked. A firewall, the network protection part of an",
                "antivirus, or a university network that only allows web browsing",
                "will each do this.",
                "",
                "Try a phone hotspot. If it works there, the block is on this",
                "network; if it fails there too, it is on this machine.",
            ]))

    # Everything above this line was asked with Python, which trusts what the
    # operating system trusts and can be waved through where pixi is not. This
    # is pixi being asked the same question in its own words, and it is the
    # answer that actually decides whether an install can work.
    if download.outcome == probe.REFUSED:
        title = "pixi's own download was refused over a certificate"
        findings.append(Finding(FAIL, INTERNET, title, download.lines,
                                scanning_advice(ctx, vendors, title)))
    elif download.outcome in (probe.STOPPED, probe.SLOW):
        findings.append(Finding(
            FAIL, INTERNET, "pixi's own download did not get through",
            download.lines, [
                "This is the program that fails for you, failing here in the same",
                "way, so whatever is stopping it is stopping `pixi install` too.",
                "",
                "A firewall, the network protection part of an antivirus, or a",
                "university network that only allows web browsing will each do",
                "this. Try a phone hotspot: if it works there, the block is on this",
                "network, and if it fails there too, it is on this machine.",
            ]))
    elif download.outcome == probe.PUZZLING:
        findings.append(Finding(
            WARN, INTERNET, "pixi could not finish a test download",
            download.lines, [
                "pixi was asked to fetch one small file and did not manage it, for",
                "a reason this check does not recognise. If `pixi install` works in",
                "your course folder, ignore this; if it does not, bring these lines",
                "to class.",
            ]))
    elif download.ok:
        findings.append(Finding(
            OK, INTERNET, "pixi downloaded a file of its own without trouble",
            ["from conda.anaconda.org, into a cache thrown away afterwards"]))

    if reached:
        findings.append(Finding(
            OK, INTERNET, f"{len(reached)} of {len(results)} hosts answered properly",
            [f"{result.host} ({result.issuer})" for result in reached]))

        offset = probe.clock_offset(reached[0].host)
        if offset is not None and abs(offset) > 300:
            findings.append(Finding(
                WARN, INTERNET, "This machine's clock is wrong",
                [f"It is {abs(offset) / 60:.0f} minutes "
                 f"{'ahead of' if offset > 0 else 'behind'} {reached[0].host}"], [
                    "A clock that is far out makes valid certificates look expired or",
                    "not yet valid, and the error talks about certificates rather than",
                    "about the clock. Set the date and time to update automatically,",
                    "and check the time zone while you are there.",
                ]))
    return findings


def vscode_check(ctx: Context) -> list[Finding]:
    """Whether the editor the course is taught in is here, with what it needs."""
    places = [Path(p) for p in (
        "/Applications/Visual Studio Code.app",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft VS Code\Code.exe"),
        str(Path.home() / "Applications" / "Visual Studio Code.app"),
    )]
    found = [place for place in places if place.exists()]
    command = shutil.which("code")

    if command is None and not found:
        return [Finding(WARN, EDITOR, "It was not found where it usually installs", [], [
            "The course is taught in VS Code, and the notebooks are opened in it.",
            "If you are using something else on purpose, ignore this. Otherwise",
            "install it from https://code.visualstudio.com and open your course",
            "folder with File -> Open Folder.",
        ])]

    if command is None:
        return [Finding(OK, EDITOR, "It is installed",
                        [str(found[0]),
                         "The `code` command is not on PATH, so its extensions were not checked"])]

    listed = run_briefly([command, "--list-extensions"], 30)
    if listed is None:
        return [Finding(OK, EDITOR, "It is installed", [command])]

    installed = {line.strip().lower() for line in listed.splitlines() if line.strip()}
    missing = [(key, name) for key, name in EXTENSIONS if key not in installed]
    if not missing:
        return [Finding(OK, EDITOR, "It is installed, with the Python and Jupyter extensions")]
    return [Finding(WARN, EDITOR, "It is missing an extension the course needs",
                    [name for _, name in missing], [
                        "Without these, VS Code opens a notebook but cannot run it, and the",
                        "kernel picker either stays empty or never finds your .pixi",
                        "environment. Install them with:",
                        "",
                        *(f"    code --install-extension {key}" for key, _ in missing),
                    ])]


# In the order a student should read them, which is also the order they run in:
# what this machine is, where they are, what is installed, and only then the
# two things that are somebody else's fault.
CHECKS = (
    machine_check,
    version_check,
    folder_check,
    path_checks,
    writable_check,
    disk_check,
    pixi_check,
    environment_check,
    moved_check,
    packages_check,
    interpreter_check,
    security_check,
    proxy_check,
    network_check,
    vscode_check,
)
