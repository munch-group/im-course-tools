"""Running every check, and laying the answers out for someone who is stuck.

The scan prints as it happens, so the command visibly does something while it
waits on the network, and it prints only what is wrong: a student running this
is stuck, and forty ticks scrolling past is forty lines of hiding the one that
matters. --verbose shows everything that was looked at, and the file written by
--report holds it whether or not anyone asked. What to do about any of it comes
afterwards, in full, numbered, and only for what was actually wrong. The scan is
for reading quickly and the list underneath is for acting on, and running them
together makes both harder to use.

Only failures set the exit code. A course folder inside OneDrive is worth a
paragraph and is not worth telling a student their setup is broken over.
"""

from __future__ import annotations

import datetime
import os
import platform
import sys
from pathlib import Path

from .checks import CHECKS, FAIL, MACHINE, OK, WARN, Context, Finding

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
                           "Please show this line to your instructor."])
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

    echo("Looking at your setup. The internet checks take a few seconds.")
    echo("")

    findings: list[Finding] = []
    group = None
    fine = 0
    for finding in findings_for(context, checks):
        findings.append(finding)
        if finding.status == OK and not verbose:
            fine += 1
            continue
        if finding.group != group:
            if group is not None:
                echo("")
            group = finding.group
            echo(group)
        echo(f"  {mark[finding.status]} {finding.title}")
        for detail in finding.detail:
            echo(f"      {detail}")

    if fine:
        if group is not None:
            echo("")
        echo(f"{fine} {'other ' if group is not None else ''}"
             f"{'things were' if fine > 1 else 'thing was'} looked at and found fine.")
        echo("Run `im doctor --verbose` to see them.")

    echo("")
    if trouble_first(findings):
        echo("")
        for line in render_advice(findings):
            echo(line)
    else:
        echo("Nothing here looks wrong.")
        echo("")
        echo("If something still does not work, run `im doctor --report` and send")
        echo("the file it writes to your instructor.")
        echo("")

    if report:
        target = (context.folder or context.cwd) / REPORT_NAME
        try:
            target.write_text(render_report(findings, context, version), encoding="utf-8")
        except OSError as error:
            echo(f"Could not write the report: {error}")
            return 1
        echo(f"Written to {target}")
        echo("Send that file to your instructor. It holds no passwords.")
        echo("")

    return 1 if any(finding.status == FAIL for finding in findings) else 0
