"""Running every check, and laying the answers out for someone who is stuck.

What reaches the screen is what can be acted on: for each thing that is wrong,
the one line naming it and the commands to paste, failures before warnings,
each with a blank line around it. Nothing else. A student running this is
stuck, and every line they have to read past is a line hiding the command
underneath it — which is how a paragraph explaining a fix ends up preventing
one.

The explanations are not thrown away; they are moved. `--verbose` prints
everything that was looked at, with the reasoning in full, and the file
`--report` writes holds it whether or not anyone asked. The reader there is an
instructor with the time to spend, and what they need is the opposite of brief.

Nothing prints while the checks run, so the one line up front says the internet
checks take a few seconds: printing findings as they arrive means printing each
one twice, once where it was found and once where it can be acted on, and
twice is what this command is trying to stop doing.

Only failures set the exit code. A course folder inside OneDrive is worth a
paragraph and is not worth telling a student their setup is broken over.
"""

from __future__ import annotations

import datetime
import os
import platform
import sys
from pathlib import Path

from .checks import (CHECKS, FAIL, MACHINE, OK, PIXI, SHELL, WARN, Context,
                     Finding)

REPORT_NAME = "im-doctor-report.txt"

# Written into the report so a machine can be recognised from it, and nothing
# else: never the whole environment, which is where tokens and passwords live.
REPORT_VARIABLES = (
    "PATH", "SHELL", "TERM", "TERM_PROGRAM", "COMSPEC", "LANG", "LC_ALL",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "PIP_CERT", "PIP_INDEX_URL",
    "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_CHANNELS", "CONDA_SSL_VERIFY",
    "VIRTUAL_ENV", "ZDOTDIR",
    "PIXI_PROJECT_ROOT", "PIXI_PROJECT_NAME", "PIXI_ENVIRONMENT_NAME", "PIXI_CACHE_DIR",
    "RATTLER_CACHE_DIR", "IM_COURSE_URL", "IM_COURSE_FOLDER",
)

# What the report is written with, so it survives Notepad.
PLAIN = {OK: "[ok]", WARN: "[! ]", FAIL: "[xx]"}


def marks(stream=None) -> dict[str, str]:
    """The three symbols, in whichever alphabet this terminal can actually print.

    An older Windows terminal is still on a code page with no tick in it, and
    writing one there raises rather than degrading. Asking first costs nothing
    and keeps the report readable in the place it is most needed.
    """
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "✓✗".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return {OK: "+", WARN: "!", FAIL: "x"}
    return {OK: "✓", WARN: "!", FAIL: "✗"}


def findings_for(context: Context, checks=CHECKS):
    """Every check in turn, with a crash in one of them costing only that one.

    A check reads unusual corners of an unfamiliar machine, which is exactly
    where an unforeseen exception comes from, and a doctor that dies on the
    machine it was written for is no doctor at all.
    """
    group = MACHINE
    for check in checks:
        try:
            produced = check(context)
        except Exception as error:              # noqa: BLE001 - see the docstring
            name = check.__name__.replace("_check", "").replace("_checks", "")
            yield Finding(WARN, group, f"The {name} check could not be run",
                          [f"{type(error).__name__}: {error}"],
                          ["This is a fault in `im doctor` itself, not in your setup.",
                           "Please show this line to your instructor."],
                          fix=["This is a fault in `im doctor` itself, not in your setup.",
                               f"Show this to your instructor: {type(error).__name__}: {error}"])
            continue
        if produced is None:
            continue
        for finding in ([produced] if isinstance(produced, Finding) else produced):
            group = finding.group
            yield finding


def trouble_first(findings: list[Finding]) -> list[Finding]:
    """The things worth acting on, failures before warnings, otherwise in order."""
    return [f for f in findings if f.status == FAIL] + \
           [f for f in findings if f.status == WARN]


def render(findings: list[Finding], mark: dict[str, str]) -> list[str]:
    """The scan, grouped under its headings."""
    lines: list[str] = []
    group = None
    for finding in findings:
        if finding.group != group:
            if group is not None:
                lines.append("")
            group = finding.group
            lines.append(group)
        lines.append(f"  {mark[finding.status]} {finding.title}")
        lines.extend(f"      {detail}" for detail in finding.detail)
    return lines


def padded(lines: list[str]) -> list[str]:
    """The same lines with one blank around each block, and never two."""
    kept: list[str] = []
    for line in lines:
        blank = not line.strip()
        if blank and (not kept or not kept[-1]):
            continue
        kept.append("" if blank else line)
    while kept and not kept[-1]:
        kept.pop()
    return ["", *kept, ""] if kept else []


def first_thing(wrong: list[Finding]) -> Finding | None:
    """The one thing to do before the rest of the list is worth reading.

    An environment that has not been activated is the second step of a
    staircase whose first step is being in the course folder at all, and there
    is no use handing somebody the fourth step while they are standing on the
    second. So the rest waits for the next run.

    Unless what is wrong is pixi itself or the terminal it is typed into, in
    which case `pixi shell` is not going to work either and saying so first
    would leave a student trying to climb a step that is not there.
    """
    if any(finding.group in (PIXI, SHELL) for finding in wrong):
        return None
    return next((finding for finding in wrong if finding.alone), None)


def render_fixes(findings: list[Finding], mark: dict[str, str]) -> list[str]:
    """Every thing that is wrong, as the line naming it and the way out of it.

    A finding that was never given a short answer falls back to its long one,
    which is worse to read and better than silence.
    """
    lines: list[str] = []
    for finding in trouble_first(findings):
        lines += ["", f"{mark[finding.status]} {finding.title}", ""]
        lines += finding.fix or finding.advice or ["    Bring this line to class."]
    return lines


def render_advice(findings: list[Finding]) -> list[str]:
    """The numbered list of what to do, one entry per thing that was wrong."""
    lines = ["What to do", ""]
    for number, finding in enumerate(trouble_first(findings), 1):
        lines.append(f"{number}. {finding.title}")
        for line in finding.advice or ["Bring this line to class."]:
            lines.append(f"   {line}" if line else "")
        lines.append("")
    return lines


def render_report(findings: list[Finding], context: Context, version: str = "") -> str:
    """The whole thing as a file, for a student to send rather than describe.

    A screenshot of a terminal arrives cropped, and a description arrives
    paraphrased. This arrives whole, and it is worth more than either.
    """
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "im doctor report",
        f"written {when}" + (f" by im-course-tools {version}" if version else ""),
        "",
    ]
    lines += render(findings, PLAIN)
    lines += ["", ""]
    if any(f.status in (FAIL, WARN) for f in findings):
        lines += render_advice(findings)
    else:
        lines += ["Nothing looked wrong.", ""]

    lines += ["", "Details", ""]
    lines.append(f"  command      {' '.join(sys.argv)}")
    lines.append(f"  python       {sys.version.split()[0]} at {sys.executable}")
    lines.append(f"  platform     {platform.platform()}")
    lines.append(f"  working dir  {context.cwd}")
    lines.append(f"  course dir   {context.folder or 'not found'}")
    lines.append("")
    for name in REPORT_VARIABLES:
        value = os.environ.get(name)
        if value:
            lines.append(f"  {name}={value}")
    return "\n".join(lines) + "\n"


def diagnose(echo, *, offline: bool = False, report: bool = False,
             cwd: Path | None = None, checks=CHECKS, version: str = "",
             stream=None, verbose: bool = False) -> int:
    """Look at everything, say what was found, and say what to do about it.

    Everything looked at is kept, and only what is wrong is printed unless
    `verbose`. The count of the rest is printed too, so that a scan which found
    nothing to say still visibly happened.
    """
    context = Context(system=platform.system(),
                      cwd=Path(cwd) if cwd else Path.cwd(),
                      offline=offline)
    mark = marks(stream)

    # Stopped rather than finished, when a check says nothing after it can
    # mean anything. The one that does is being outside the course folder.
    #
    # Which is also why the line about waiting is not said until the course
    # folder has been found: said before, it would be the first half of a
    # two-line answer whose second half is "you are in the wrong folder", and
    # a promise of internet checks that are never going to run.
    findings: list[Finding] = []
    announced = False
    for finding in findings_for(context, checks):
        findings.append(finding)
        if finding.stop:
            break
        if not announced and context.folder is not None:
            announced = True
            echo("")
            echo("Looking at your setup. The internet checks take a few seconds.")
    stopped = bool(findings) and findings[-1].stop
    wrong = trouble_first(findings)

    # Built up whole and padded once, so that every block has a blank line on
    # each side and nowhere has two, however the pieces below were assembled.
    said: list[str] = []
    if verbose:
        said += render(findings, mark)
        said += [""]
        said += render_advice(findings) if wrong else ["Nothing looked wrong."]
    elif stopped:
        # One sentence and nothing else. Everything this student could act on
        # is in the folder they are being sent to.
        said += findings[-1].fix or [findings[-1].title]
    elif wrong and first_thing(wrong) is not None:
        gate = first_thing(wrong)
        said += gate.fix if [f for f in wrong if f is not gate] else gate.alone
    elif wrong:
        said += render_fixes(findings, mark)
    else:
        said += ["Nothing here looks wrong."]

    if not report and not stopped and first_thing(wrong) is None:
        opening = ("If that does not fix it, run this and send the file it writes"
                   if wrong else
                   "If something still does not work, run this and send the file")
        said += ["", opening,
                 "to your instructor:" if wrong else "it writes to your instructor:",
                 "",
                 "    im doctor --report"]

    if report:
        target = (context.folder or context.cwd) / REPORT_NAME
        try:
            target.write_text(render_report(findings, context, version), encoding="utf-8")
        except OSError as error:
            said += ["", f"Could not write the report: {error}"]
            for line in padded(said):
                echo(line)
            return 1
        said += ["", f"Written to {target}", "",
                 "Send that file to your instructor. It holds no passwords."]

    for line in padded(said):
        echo(line)

    return 1 if any(finding.status == FAIL for finding in findings) else 0
