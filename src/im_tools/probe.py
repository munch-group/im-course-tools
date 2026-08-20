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
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
