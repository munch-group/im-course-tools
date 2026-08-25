"""Reaching the hosts pixi downloads from, and seeing who signed their certificates.

A `pixi install` that fails on a student's machine fails for one of three
reasons almost every time: nothing here can reach the internet at all, this one
host is blocked, or something on the machine is opening the encrypted
connection on its way past and signing it again with a certificate of its own.

Antivirus products do the third deliberately, so that they can read what is
inside, and it breaks pixi while the browser sitting next to it stays perfectly
happy. The browser is happy because the antivirus installed itself into the
list of certificate authorities the operating system trusts. pixi carries its
own list, compiled in, which nothing can install itself into.

So "the internet works" is not an answer a student can act on. Who signed the
certificate is the part that explains the failure, and that is what this asks.

It asks twice over. The connections here are opened by Python, which trusts a
different list of authorities on every operating system and can therefore be
let through where pixi is turned away, so the last thing this module does is
make pixi itself fetch something and watch what happens. That is the only
question really being asked: whether the program that fails for the student can
download.
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# What a course environment actually downloads from: conda packages from the
# first two, anything installed with pip from the next two, and the widgets the
# course publishes on GitHub from the last two.
HOSTS = (
    "conda.anaconda.org",
    "prefix.dev",
    "pypi.org",
    "files.pythonhosted.org",
    "github.com",
    "objects.githubusercontent.com",
)

# How a single host turned out. One of these per host, and the doctor decides
# which of them is worth interrupting a student for.
REACHED = "reached"           # connected, and a public authority signed it
INTERCEPTED = "intercepted"   # something signed it that should not have
UNVERIFIED = "unverified"     # the certificate did not verify at all
UNKNOWN_CA = "unknown-ca"     # it verified, but not by an authority we know
UNRESOLVED = "unresolved"     # the name could not be looked up
UNREACHABLE = "unreachable"   # the name resolved, the connection did not happen

# Products that put themselves in the middle of an encrypted connection, by the
# name they sign with. Matching one of these is not a guess: the certificate
# for conda.anaconda.org says, in writing, that this product issued it.
INTERCEPTORS = {
    "kaspersky": "Kaspersky",
    "eset": "ESET",
    "avast": "Avast",
    "avg": "AVG",
    "bitdefender": "Bitdefender",
    "sophos": "Sophos",
    "mcafee": "McAfee",
    "norton": "Norton",
    "symantec web": "Symantec Web Security",
    "trend micro": "Trend Micro",
    "f-secure": "F-Secure",
    "gdata": "G DATA",
    "g data": "G DATA",
    "eset ssl filter": "ESET",
    "dr.web": "Dr.Web",
    "malwarebytes": "Malwarebytes",
    "webroot": "Webroot",
    "zscaler": "Zscaler",
    "netskope": "Netskope",
    "fortinet": "FortiGate",
    "fortigate": "FortiGate",
    "palo alto": "Palo Alto Networks",
    "blue coat": "Blue Coat",
    "bluecoat": "Blue Coat",
    "forcepoint": "Forcepoint",
    "websense": "Forcepoint",
    "sonicwall": "SonicWall",
    "cisco umbrella": "Cisco Umbrella",
    "untangle": "Untangle",
    "kerio": "Kerio",
    "squid": "a Squid proxy",
    "mitmproxy": "mitmproxy",
    "charles proxy": "Charles Proxy",
    "fiddler": "Fiddler",
}

# Authorities that sign for the real internet. This list exists only so that a
# certificate signed by something nobody recognises can be mentioned quietly,
# rather than called antivirus with no evidence. Missing an authority here
# costs a student one sentence of "this looks unusual", so it is allowed to be
# incomplete in a way the list above is not.
PUBLIC_AUTHORITIES = (
    "digicert", "let's encrypt", "isrg", "sectigo", "comodo", "amazon",
    "google trust services", "globalsign", "godaddy", "starfield",
    "cloudflare", "entrust", "identrust", "baltimore", "usertrust",
    "microsoft", "apple", "actalis", "buypass", "zerossl", "ssl.com",
    "certum", "thawte", "verisign", "geotrust", "rapidssl", "harica",
    "quovadis", "swisssign", "trustwave", "wisekey", "e-tugra", "firmaprofesional",
)


@dataclass
class Probe:
    """One host, asked once."""

    host: str
    outcome: str
    issuer: str | None = None
    vendor: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == REACHED


def interceptor(issuer: str | None) -> str | None:
    """The product that signed this certificate, if it is one we recognise."""
    if not issuer:
        return None
    lowered = issuer.lower()
    for fragment, name in INTERCEPTORS.items():
        if fragment in lowered:
            return name
    return None


def public_authority(issuer: str | None) -> bool:
    """Whether the name that signed this certificate signs for the real internet."""
    if not issuer:
        return False
    lowered = issuer.lower()
    return any(fragment in lowered for fragment in PUBLIC_AUTHORITIES)


def issuer_of(certificate) -> str | None:
    """The organisation named on a certificate, falling back to its common name."""
    if not certificate:
        return None
    named = dict(pair for part in certificate.get("issuer", ()) for pair in part)
    return named.get("organizationName") or named.get("commonName")


def probe_host(host: str, timeout: float = 6.0) -> Probe:
    """Open one TLS connection to `host` and report what came back.

    Nothing is sent and nothing is downloaded: the handshake alone answers both
    questions, whether the host can be reached and who vouches for it.
    """
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        return Probe(host, UNRESOLVED, error=str(error))

    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secure:
                issuer = issuer_of(secure.getpeercert())
    except ssl.SSLCertVerificationError as error:
        return Probe(host, UNVERIFIED, error=error.verify_message or str(error))
    except ssl.SSLError as error:
        return Probe(host, UNVERIFIED, error=str(error))
    except socket.timeout:
        return Probe(host, UNREACHABLE, error="it did not answer in time")
    except OSError as error:
        return Probe(host, UNREACHABLE, error=str(error))

    vendor = interceptor(issuer)
    if vendor is not None:
        return Probe(host, INTERCEPTED, issuer=issuer, vendor=vendor)
    if not public_authority(issuer):
        return Probe(host, UNKNOWN_CA, issuer=issuer)
    return Probe(host, REACHED, issuer=issuer)


def probe_all(hosts=HOSTS, timeout: float = 6.0) -> list[Probe]:
    """Every host at once, so the whole question costs one host's worth of waiting."""
    hosts = list(dict.fromkeys(hosts))
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        return list(pool.map(lambda host: probe_host(host, timeout), hosts))


# What pixi is asked to fetch. `search` is the one pixi command that downloads
# without needing a workspace or touching one, and a single package from a
# single channel costs the index for that channel and nothing else: about a
# megabyte, once. The package only has to exist; what it says is never read.
PIXI_SEARCH = ("search", "--limit", "1", "--channel", "conda-forge",
               "--platform", "noarch", "tqdm")

# How that attempt turned out.
DOWNLOADED = "downloaded"     # pixi's own downloads work, and that settles it
REFUSED = "refused"           # a certificate pixi would not accept
STOPPED = "stopped"           # the connection did not happen at all
SLOW = "slow"                 # it was still trying when we stopped waiting
PUZZLING = "puzzling"         # it failed for a reason this does not recognise
NOT_TRIED = "not-tried"       # pixi is not here, or the internet was not asked

# The words pixi uses when a certificate is the problem. rattler, underneath
# pixi, says "invalid peer certificate: UnknownIssuer" almost verbatim when an
# antivirus has signed the connection, and this is that sentence.
REFUSED_WORDS = ("certificate", "unknownissuer", "self-signed", "self signed",
                 "handshake", "tls", "webpki")

# And when the connection never happened. Both lists are read after the fact,
# so a message that matches neither is reported rather than guessed at.
STOPPED_WORDS = ("connect error", "connection refused", "connection reset",
                 "connection closed", "dns error", "failed to lookup",
                 "no such host", "timed out", "timeout", "os error",
                 "error sending request", "proxy", "network is unreachable")


@dataclass
class Download:
    """pixi's own attempt at one download, and what it said about it."""

    outcome: str
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == DOWNLOADED

    @property
    def failed(self) -> bool:
        """Whether pixi could not download, as opposed to could not be asked."""
        return self.outcome in (REFUSED, STOPPED, SLOW)


def readable(output: str, keep: int = 4) -> list[str]:
    """pixi's error without the box it is drawn in, innermost cause last.

    pixi draws its errors as a tree of box-drawing characters and wraps long
    urls across lines, neither of which survives being quoted in a report or
    read out over a phone. The cause is at the bottom, so it is the bottom that
    is kept.
    """
    lines: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("Error:"):          # the first line carries both
            line = line[len("Error:"):]
        line = line.lstrip("×│├└╰┌─▶ ").strip()
        if not line or line.startswith("Using channels"):
            continue
        # A url that pixi wrapped: put it back together rather than reporting
        # half of one on a line of its own.
        if lines and lines[-1].count("(") > lines[-1].count(")"):
            lines[-1] += line
            continue
        lines.append(line)
    return lines[-keep:]


def pixi_download(executable=None, timeout: float = 45.0) -> Download:
    """Make pixi itself fetch one small file, and report how that went.

    This is the only check that asks the question in the student's own terms.
    Everything else here is Python reaching a host, which on Windows means
    trusting whatever the operating system trusts — including the certificate
    an antivirus signs with. pixi trusts only the list compiled into it, so
    Python getting through is not evidence that pixi will.

    It downloads into a cache of its own, thrown away afterwards, for two
    reasons: pixi has to go to the network rather than answer out of a file it
    already had, and `im doctor` has to leave nothing behind.
    """
    executable = executable or shutil.which("pixi")
    if executable is None:
        return Download(NOT_TRIED)

    # Emptied by hand rather than by TemporaryDirectory, because its own
    # cleanup raises, and it raises OSError, and the only thing to do with an
    # OSError from around here is report that pixi never got to try. On Windows
    # a cache pixi has just written is exactly where a file left open or marked
    # read-only turns up, so that would mean answering "pixi could not even
    # start" on the strength of a download that had in fact just succeeded.
    cache = tempfile.mkdtemp(prefix="im-doctor-")
    try:
        finished = subprocess.run(
            [str(executable), *PIXI_SEARCH],
            capture_output=True, text=True, timeout=timeout,
            env=dict(os.environ, PIXI_CACHE_DIR=cache, RATTLER_CACHE_DIR=cache,
                     PIXI_NO_PROGRESS="true", PIXI_COLOR="never"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return Download(SLOW, [f"it was still trying after {timeout:.0f} seconds"])
    except (OSError, subprocess.SubprocessError) as error:
        return Download(NOT_TRIED, [str(error)])
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    if finished.returncode == 0:
        return Download(DOWNLOADED)

    said = f"{finished.stderr or ''}\n{finished.stdout or ''}"
    lines = readable(said)
    lowered = said.lower()
    if any(word in lowered for word in REFUSED_WORDS):
        return Download(REFUSED, lines)
    if any(word in lowered for word in STOPPED_WORDS):
        return Download(STOPPED, lines)
    return Download(PUZZLING, lines)


def clock_offset(host: str, timeout: float = 6.0) -> float | None:
    """How far this machine's clock is from a host's, in seconds, or None.

    A clock that is days out makes every certificate look either not valid yet
    or long expired, and the resulting error talks about certificates rather
    than about the clock, which sends students hunting in the wrong place.
    """
    request = urllib.request.Request(
        f"https://{host}/", method="HEAD",
        headers={"User-Agent": "instructing-machines"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            stamp = response.headers.get("Date")
    except urllib.error.HTTPError as error:      # a 404 still carries the time
        stamp = error.headers.get("Date") if error.headers else None
    except Exception:                            # best effort; never the point
        return None
    if not stamp:
        return None
    try:
        theirs = parsedate_to_datetime(stamp)
    except (TypeError, ValueError):
        return None
    if theirs.tzinfo is None:
        theirs = theirs.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - theirs).total_seconds()
