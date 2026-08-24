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
SHELL = "Your terminal"
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

# The kernel every notebook starts. It names the environment's own Python by
# its whole path, which makes it the second place on disk that still remembers
# where the course folder stood when the environment was built.
KERNEL_SPEC = ("share", "jupyter", "kernels", "python3", "kernel.json")

# Scripts pixi writes with that same path in their first line. Windows gets
# .exe files here instead, where the kernel above is the one that answers.
SHEBANGS = (("bin", "pip"), ("bin", "jupyter"), ("bin", "im"), ("bin", "pytest"))

# The files each shell reads when it starts, the one to write to first. pixi's
# installer appends its PATH line to one of these, choosing by asking the same
# question this does — so a student whose terminal runs a shell other than the
# one the installer saw ends up with the line in a file nothing reads.
STARTUP_FILES = {
    "zsh": (".zshrc", ".zprofile", ".zshenv"),
    "bash": (".bash_profile", ".bashrc", ".profile"),
    "sh": (".profile",),
    "dash": (".profile",),
    "ksh": (".kshrc", ".profile"),
    "fish": (".config/fish/config.fish",),
}

# Where the installer puts pixi, as it is written inside a startup file.
PIXI_ON_PATH = ".pixi/bin"

# Shells worth believing when the process that started `im` names one. Anything
# else — pixi, VS Code, python — means `im` was not typed into a shell directly,
# and $SHELL is the better answer.
KNOWN_SHELLS = frozenset(STARTUP_FILES) | {"tcsh", "csh", "powershell", "pwsh", "cmd"}

# What to call them in a sentence a student reads.
SHELL_NAMES = {"powershell": "Windows PowerShell", "pwsh": "PowerShell",
               "cmd": "Command Prompt"}

# Asked of PowerShell: what it is enforcing, and then every scope, so that a
# policy set by the university can be told from one the student can change.
# Only single quotes, which survive being passed through Windows' own quoting.
POLICY_QUERY = ("Get-ExecutionPolicy; Get-ExecutionPolicy -List | ForEach-Object "
                "{ $_.Scope.ToString() + '=' + $_.ExecutionPolicy.ToString() }")

# The order PowerShell reads them in: the first that is not Undefined wins.
POLICY_SCOPES = ("MachinePolicy", "UserPolicy", "Process", "CurrentUser", "LocalMachine")

# The two that stop the scripts pixi and VS Code write from running at all.
POLICY_TITLES = {"restricted": "PowerShell is not allowed to run scripts",
                 "allsigned": "PowerShell only runs scripts that are signed"}

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
    # The shell this was typed into. Asking costs a process, two checks want
    # the answer, and "" means it was asked and could not be told.
    shell: str | None = None


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


def folder_inside(cwd: Path, depth: int = 2, budget: int = 60) -> Path | None:
    """A course folder sitting just below this one, for a zip unpacked twice over.

    Windows' "Extract all" offers a destination folder named after the zip, and
    the zip already holds a folder of that name, so accepting the default —
    which everybody does — leaves the course folder one level further down than
    anyone expects: instructing-machines inside instructing-machines. What the
    student then opens in VS Code is a folder with nothing in it but another
    folder, and every command they are told to run is run in the wrong one of
    the two, where there is no pixi.toml and nothing works.

    Nothing is wrong with the copy itself, which is why this is worth telling
    apart from a course folder that is genuinely missing: the fix is one `cd`
    and not a download.
    """
    queue = [(cwd, 0)]
    while queue and budget > 0:
        place, level = queue.pop(0)
        budget -= 1
        try:
            entries = sorted(place.iterdir())
        except OSError:
            continue
        if place != cwd and any(e.name == MARKER and is_file(e) for e in entries):
            return place
        if level < depth:
            for entry in entries:
                if entry.name.startswith(".") or entry.name in SKIP_WHEN_LOOKING:
                    continue
                if is_dir(entry):
                    queue.append((entry, level + 1))
    return None


def only_thing_in(cwd: Path, entry: Path) -> bool:
    """Whether that folder is all this one holds, ignoring what unpacking leaves."""
    try:
        return not [e for e in cwd.iterdir()
                    if e != entry and e.name != "__MACOSX" and not e.name.startswith(".")]
    except OSError:
        return False


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


def parent_name(system: str) -> str | None:
    """The name of the program `im` was started by, or None if it would not say."""
    if system == "Windows":
        said = security.powershell(f"(Get-Process -Id {os.getppid()}).ProcessName", 15)
    else:
        said = run_briefly(["ps", "-o", "comm=", "-p", str(os.getppid())], 5)
    lines = [line.strip() for line in (said or "").splitlines() if line.strip()]
    if not lines:
        return None
    # A login shell lists itself as -zsh, and macOS answers with a whole path.
    name = Path(lines[0].lstrip("-")).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name or None


def shell_of(ctx: Context) -> str | None:
    """The shell this terminal is running, asked once and remembered.

    $SHELL is the login shell, which is a different question with a different
    answer often enough to matter: the terminal VS Code opens, and a student
    who once followed an instruction to use bash, both leave $SHELL naming a
    shell nobody is typing into — and the startup file that has to hold pixi's
    PATH line is the one the shell actually running reads.

    So the process that started `im` is asked first, and $SHELL is the fallback
    for when that answer is not a shell at all, which it is not when `im` was
    run through pixi or from inside VS Code.
    """
    if ctx.shell is None:
        found = parent_name(ctx.system) or ""
        if found.lower() not in KNOWN_SHELLS:
            found = Path(os.environ.get("SHELL") or "").name or found
        ctx.shell = found.lower()
    return ctx.shell or None


def startup_files(shell: str | None, system: str = "", home: Path | None = None) -> list[Path]:
    """The files that shell reads when it starts, the one to write to first.

    macOS opens bash as a login shell, which reads .bash_profile and stops;
    everywhere else it is .bashrc. Getting that the wrong way round is a line
    added to a file the shell never opens, which looks exactly like having
    done nothing.
    """
    home = home or Path.home()
    shell = (shell or "").lower()
    names = STARTUP_FILES.get(shell, ())
    if shell == "bash" and system != "Darwin":
        names = (".bashrc", ".bash_profile", ".profile")
    base = home
    if shell == "zsh" and os.environ.get("ZDOTDIR"):
        base = Path(os.environ["ZDOTDIR"]).expanduser()
    return [base.joinpath(*name.split("/")) for name in names]


def pixi_on_startup(files: list[Path]) -> Path | None:
    """The first of those files that puts pixi's own folder on PATH, if any does."""
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if PIXI_ON_PATH in text.replace("\\", "/"):
            return path
    return None


def path_line(shell: str | None) -> str:
    """The line that puts pixi on PATH, written the way that shell writes it."""
    if (shell or "").lower() == "fish":
        return 'fish_add_path "$HOME/.pixi/bin"'
    return 'export PATH="$HOME/.pixi/bin:$PATH"'


def tilde(path: Path, home: Path | None = None) -> str:
    """A path in the home folder as a student would type it."""
    home = home or Path.home()
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def append_command(shell: str | None, target: Path) -> str:
    """The one line that adds pixi to that shell's PATH for good."""
    return f"echo '{path_line(shell)}' >> {tilde(target)}"


def execution_policy() -> tuple[str, str | None] | None:
    """What PowerShell allows scripts to do, and the scope that decides it.

    The scope matters as much as the answer. A policy a student set, or one
    Windows came with, is theirs to change in a single command; one arriving
    through MachinePolicy or UserPolicy is the university's, and telling them
    to run Set-ExecutionPolicy against it wastes the one thing they will try.
    """
    output = security.powershell(POLICY_QUERY)
    if output is None:
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    scopes = {}
    for line in lines[1:]:
        scope, _, value = line.partition("=")
        scopes[scope.strip()] = value.strip()
    for scope in POLICY_SCOPES:
        value = scopes.get(scope, "")
        if value and value.lower() != "undefined":
            return lines[0], scope
    return lines[0], None


def missing_packages(python: Path, timeout: float = 120.0) -> list[str] | None:
    """The course packages that this Python cannot import, or None if it would not say."""
    output = run_briefly([python, "-c", PACKAGE_PROBE % (PACKAGES,)], timeout)
    if output is None:
        return None
    return [line.strip() for line in output.splitlines() if line.strip()]


def folder_of(path: Path) -> Path | None:
    """The course folder a path inside .pixi belongs to, read off the path itself."""
    for parent in path.parents:
        if parent.name == ".pixi":
            return parent.parent
    return None


def stamped_folder(env: Path) -> Path | None:
    """The folder pixi wrote into the environment when it built it."""
    try:
        record = json.loads(env.joinpath(*ENV_RECORD).read_text(encoding="utf-8"))
        manifest = record["manifest_path"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return Path(manifest).parent


def kernel_folder(env: Path) -> Path | None:
    """The folder named by the kernel every notebook in it starts."""
    try:
        spec = json.loads(env.joinpath(*KERNEL_SPEC).read_text(encoding="utf-8"))
        argv = spec["argv"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for word in argv if isinstance(argv, list) else []:
        if isinstance(word, str) and ".pixi" in word:
            return folder_of(Path(word))
    return None


def shebang_folder(env: Path) -> Path | None:
    """The folder named in the first line of the scripts pixi wrote."""
    for parts in SHEBANGS:
        try:
            with env.joinpath(*parts).open("rb") as handle:
                first = handle.readline(400).decode("utf-8", "replace")
        except OSError:
            continue
        if not first.startswith("#!"):
            continue
        for word in first[2:].split():
            if ".pixi" in word:
                return folder_of(Path(word.strip('"')))
    return None


def built_for(env: Path) -> Path | None:
    """The folder this environment was built for, if anything inside it still says.

    Three places remember, and any one of them is enough. pixi stamps the
    manifest's full path in when it builds; the kernel spec names the
    environment's own Python by its whole path, which is what a notebook
    starts; and the scripts pixi writes carry that same path in their first
    line. The stamp is asked first because pixi rewrites it on every install,
    so it is the one that is never merely left over.

    The other two are what answers for an environment built before pixi wrote
    the stamp, or built by conda, which writes none — and those used to get no
    opinion at all, which meant the commonest way to break a course folder went
    unnoticed on exactly the machines least likely to spot it themselves.
    """
    for read in (stamped_folder, kernel_folder, shebang_folder):
        try:
            found = read(env)
        except OSError:                 # an unreadable file is not an answer
            found = None
        if found is not None:
            return found
    return None


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

    inside = folder_inside(ctx.cwd)
    if inside is not None:
        return nested_finding(ctx, inside)

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


def nested_finding(ctx: Context, inside: Path) -> Finding:
    """What to say to someone standing one folder above their own course folder."""
    windows = ctx.system == "Windows"
    separator, browser = ("\\", "File Explorer") if windows else ("/", "Finder")
    doubled = inside.parent == ctx.cwd and inside.name == ctx.cwd.name
    alone = inside.parent == ctx.cwd and only_thing_in(ctx.cwd, inside)

    detail = [f"You are in {ctx.cwd}", f"and your course folder is {inside}"]
    if alone:
        detail.append("which is the only thing in it")

    advice = []
    if doubled:
        advice += [
            "The zip was unpacked into a new folder named after itself, and it",
            "already held a folder of that name, so everything ended up one level",
            f"further down than it looks: {ctx.cwd.name}{separator}{inside.name}.",
        ]
        if windows:
            advice.append('That is what the "Extract all" button offers by default.')
        advice.append("")
    advice += [
        "Nothing is broken and nothing needs downloading again. The folder you",
        "are in is not the one your work is in; the one inside it is.",
        "",
        "Change into it, and run this again from there:",
        "",
        f'    cd "{inside}"',
        "    im doctor",
        "",
        "In VS Code, File -> Open Folder and pick that one, so that the terminal",
        "it opens starts in the right place every time.",
    ]
    if doubled and alone:
        advice += [
            "",
            f"If the extra folder is confusing, drag the inner one out in {browser} to",
            "where the outer one is, and delete the empty one left behind.",
        ]
    return Finding(FAIL, FOLDER, "Your course folder is the one inside this one",
                   detail, advice)


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
        # A new terminal only helps if the line is in a file this shell reads.
        # The installer writes it into the startup file of the shell it was run
        # from, so a student who installed pixi in one shell and works in
        # another can open new terminals all afternoon and never see it.
        shell = shell_of(ctx)
        files = startup_files(shell, ctx.system) if ctx.system != "Windows" else []
        if files and pixi_on_startup(files) is None:
            named = SHELL_NAMES.get(shell, shell)
            return Finding(FAIL, PIXI, "pixi is installed, but this terminal cannot see it",
                           [f"It is at {already[0]}",
                            f"and nothing {named} reads when it starts puts it on PATH"], [
                               "The installer adds pixi to the startup file of whichever shell",
                               f"it was run from, and this terminal runs {named}, which reads",
                               "different files. Opening a new terminal will not help: the line",
                               "has to be in a file this shell reads.",
                               "",
                               "Put it there, then open a new terminal:",
                               "",
                               f"    {append_command(shell, files[0])}",
                           ])
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


def shell_finding(ctx: Context) -> Finding:
    """Which shell this terminal is running, since the two below depend on it."""
    shell = shell_of(ctx)
    if shell is None:
        return Finding(OK, SHELL, "Could not tell what this terminal is running")
    # $SHELL is a POSIX idea; on Windows it is either absent or left over from
    # something else, and either way it says nothing about this terminal.
    login = os.environ.get("SHELL") if ctx.system != "Windows" else None
    detail = []
    if login and Path(login).name.lower() != shell:
        detail.append(f"SHELL={login}, which is not what this terminal is running")
    return Finding(OK, SHELL, SHELL_NAMES.get(shell, shell), detail)


def scripts_finding(ctx: Context) -> Finding | None:
    """Whether PowerShell may run the scripts pixi and VS Code write.

    Windows ships refusing to run any script at all, and says so in a sentence
    that names neither pixi nor the course: "running scripts is disabled on
    this system". `pixi shell` writes a script and runs it, and so does VS
    Code every time it activates an environment in its terminal, so this one
    setting stops both while pixi itself keeps working perfectly.
    """
    if ctx.system != "Windows":
        return None
    answer = execution_policy()
    if answer is None:
        return None
    policy, scope = answer
    detail = [f"The execution policy is {policy}"
              + (f", set for {scope}" if scope else ", which is Windows' own default")]
    if policy.lower() not in POLICY_TITLES:
        return Finding(OK, SHELL, "PowerShell is allowed to run scripts", detail)

    # The two refuse in different words, and the words are what a student
    # searches for, so the paragraph has to use the ones they were shown.
    if policy.lower() == "restricted":
        told = ['PowerShell will not run a script file here, and says "running',
                'scripts is disabled on this system".']
    else:
        told = ["PowerShell will not run a script file here unless somebody has",
                'signed it, and says a script "is not digitally signed".']
    told += ["`pixi shell` writes one and runs it, and so does VS Code every time",
             "it activates an environment in its terminal, so both fail on this",
             "alone while pixi itself keeps working."]
    if scope in ("MachinePolicy", "UserPolicy"):
        advice = told + [
            "",
            f"This one is set by {scope}, which is a rule on the machine rather",
            "than a setting of yours, so `Set-ExecutionPolicy -Scope CurrentUser`",
            "will not change it. Start a PowerShell with the rule set aside for",
            "that one window, and work in it:",
            "",
            "    powershell -ExecutionPolicy Bypass",
            "",
            "If this is a university laptop, its IT support can lift the rule.",
        ]
    else:
        advice = told + [
            "",
            "Allow scripts for your own account, which is the setting Microsoft",
            "recommends and needs no administrator:",
            "",
            "    Set-ExecutionPolicy RemoteSigned -Scope CurrentUser",
            "",
            "Answer Y. It changes nothing for anyone else who uses this computer.",
        ]
    return Finding(WARN, SHELL, POLICY_TITLES[policy.lower()], detail, advice)


def pixi_path_finding(ctx: Context) -> Finding | None:
    """Whether this shell puts pixi on PATH itself, or only happens to have it.

    Being able to see pixi now is not the same as being able to see it
    tomorrow. A student who was walked through `export PATH=...` in class, in
    the terminal they had open at the time, has a machine that works until the
    next terminal and then does not, and `pixi: command not found` says nothing
    about which of the two happened.
    """
    # Windows keeps its PATH in the registry rather than in a startup file, and
    # a pixi nobody can see is pixi_check's to explain, whole.
    if ctx.system == "Windows" or ctx.pixi is None:
        return None
    installer = Path.home() / ".pixi" / "bin"
    try:
        theirs = Path(ctx.pixi).resolve().parent == installer.resolve()
    except OSError:
        theirs = Path(ctx.pixi).parent == installer
    if not theirs:
        return Finding(OK, SHELL, "pixi is on this terminal's PATH",
                       [f"from {Path(ctx.pixi).parent}, not from {installer}"])

    shell = shell_of(ctx)
    files = startup_files(shell, ctx.system)
    if not files:
        return Finding(OK, SHELL, "pixi is on this terminal's PATH", [str(installer)])
    found = pixi_on_startup(files)
    if found is not None:
        return Finding(OK, SHELL, f"{tilde(found)} puts pixi on PATH")
    named = SHELL_NAMES.get(shell, shell)
    return Finding(WARN, SHELL, f"Nothing {named} reads when it starts puts pixi on PATH",
                   [f"pixi is on PATH here, at {ctx.pixi}",
                    "but it was not put there by " +
                    ", ".join(tilde(path) for path in files)], [
                       "It works in this terminal and may not work in the next one, and",
                       "`pixi: command not found` in a terminal that worked yesterday is",
                       "what that looks like. Write the line into the file this shell",
                       "reads, so that every terminal has it:",
                       "",
                       f"    {append_command(shell, files[0])}",
                       "",
                       f"Then open a new terminal, or run `source {tilde(files[0])}` here.",
                   ])


def shell_checks(ctx: Context) -> list[Finding]:
    """The terminal itself: which shell, and the two things it decides."""
    return [finding for finding in (shell_finding(ctx),
                                    scripts_finding(ctx),
                                    pixi_path_finding(ctx)) if finding is not None]


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
            "course's current copy of it.",
        ]))
    elif manifest.exists() and manifest.stat().st_mtime > lock.stat().st_mtime + 1:
        findings.append(Finding(WARN, ENVIRONMENT, "pixi.toml has changed since pixi.lock was made",
                                [], [
                                    "The environment on disk may not be the one pixi.toml now asks",
                                    "for. Run `pixi install` in your course folder to catch it up,",
                                    "or `im update` to take the course's current files instead.",
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
        "pixi.toml and pixi.lock and installs them, which is what puts these",
        "back — along with anything else in the folder's setup that has moved on.",
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


def activation_check(ctx: Context) -> Finding | None:
    """Whether this terminal is standing inside the course environment.

    Installed is not the same as in use. Until the environment is activated,
    `python`, `pytest` and `jupyter` typed into a terminal are whichever ones
    the machine already had, and the import error that follows names a package
    the student can see plainly installed a folder away. Which environment is
    active matters just as much when it is the wrong one: an Anaconda that
    activates `base` in every new terminal shadows the course's Python with one
    that has none of the course's packages in it.

    Only asked once the environment exists, because telling someone to step
    into what has not been built yet is two instructions in the wrong order.
    """
    if ctx.folder is None or ctx.env_python is None:
        return None
    env = ctx.folder.joinpath(*ENV_PATH)
    root = os.environ.get("PIXI_PROJECT_ROOT")
    prefix = os.environ.get("CONDA_PREFIX")
    venv = os.environ.get("VIRTUAL_ENV")
    active = Path(prefix or venv) if (prefix or venv) else None

    if active is not None and same_folder(active, env):
        named = os.environ.get("PIXI_ENVIRONMENT_NAME") or "default"
        return Finding(OK, ENVIRONMENT, "It is active in this terminal",
                       [f"the {named} environment, at {env}"])

    def into(opening: str, *first: str) -> list[str]:
        """The way out of whatever is active here, and the way into the course's."""
        return [opening,
                "",
                *(f"    {command}" for command in first),
                "    pixi shell",
                "",
                "The prompt changes while you are in it, and `exit` leaves again. Or",
                "run a single command through pixi without stepping in at all:",
                "",
                "    pixi run im check"]

    step = into("Step into it. From your course folder, or anywhere inside it:")
    instead = "Leave it and step into the course one, from your course folder:"

    if active is None and not root:
        return Finding(WARN, ENVIRONMENT, "It is not active in this terminal", [], [
            "It is installed, but nothing in this terminal is using it, so",
            "`python`, `pytest` and `jupyter` typed here are whichever ones the",
            "machine already had rather than the ones the course installed. That",
            "is why an import can fail in the terminal and work in a notebook.",
            "",
            *step,
            "",
            "Notebooks in VS Code do not go through this: they pick their kernel",
            "themselves, in the picker at the top right.",
        ])

    if root and ctx.folder is not None and same_folder(Path(root), ctx.folder):
        named = os.environ.get("PIXI_ENVIRONMENT_NAME") or "another"
        return Finding(WARN, ENVIRONMENT,
                       f"The {named} environment is active here, not the course one",
                       [f"{named}, at {active}", f"The course environment is {env}"], [
                           "This course folder has more than one environment in it, and the",
                           "one this terminal is in is not the one the course uses.",
                           "",
                           "    exit",
                           "    pixi shell",
                       ])

    if root:
        return Finding(WARN, ENVIRONMENT, "A pixi environment from another folder is active",
                       [f"PIXI_PROJECT_ROOT={root}", f"The course environment is {env}"], [
                           "This terminal is inside a pixi environment belonging to a",
                           "different project, so its packages are the ones you get here and",
                           "the course's are not.",
                           "",
                           "Leave it and step into this one:",
                           "",
                           "    exit",
                           f'    cd "{ctx.folder}"',
                           "    pixi shell",
                       ])

    if venv and not prefix:
        return Finding(WARN, ENVIRONMENT, "A Python virtual environment is active here",
                       [f"VIRTUAL_ENV={venv}", f"The course environment is {env}"], [
                           "`python` and `pip` in this terminal are that environment's, not",
                           "the course's, and a package the course installed will look",
                           "missing here.",
                           "",
                           *into(instead, "deactivate"),
                       ])

    named = os.environ.get("CONDA_DEFAULT_ENV") or "A conda environment"
    return Finding(WARN, ENVIRONMENT, f"{named} is active in this terminal, not the course one",
                   [f"CONDA_PREFIX={prefix}", f"The course environment is {env}"], [
                       "`python`, `pip` and `jupyter` in this terminal are that",
                       "environment's, so a package the course installed will look missing",
                       "and one it never installed will appear to be there.",
                       "",
                       *into(instead, "conda deactivate"),
                       "",
                       "If a conda environment activates itself in every new terminal, that",
                       "is Anaconda doing it, and this turns it off for good:",
                       "",
                       "    conda config --set auto_activate_base false",
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
    shell_checks,
    environment_check,
    moved_check,
    packages_check,
    interpreter_check,
    activation_check,
    security_check,
    proxy_check,
    network_check,
    vscode_check,
)
