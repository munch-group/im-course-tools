"""What else on this machine might be standing between pixi and the internet.

Third-party antivirus is the single most common reason a student's `pixi
install` fails on a laptop whose browser works perfectly. It inspects encrypted
traffic by signing it again itself, which pixi refuses, and it holds files open
while pixi is unpacking them, which fails in a way that looks random and moves
around between runs.

Nothing here changes a setting or asks for a password. It only reads what the
machine already publishes about itself: on Windows the register of antivirus
products that Windows keeps for its own Security Center, and on macOS, which
keeps no such register, the presence of the handful of products a university
laptop is actually likely to be carrying.
"""

from __future__ import annotations

import glob
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Asked of Windows itself. Every antivirus registers here, including Defender,
# which is why the answer has to be read rather than counted.
ANTIVIRUS_QUERY = (
    "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct"
    " | Select-Object -ExpandProperty displayName"
)

# Whether Windows is refusing writes to Documents and Desktop. It is off by
# default, but a managed university laptop often has it on, and a course folder
# on the Desktop is then a folder pixi cannot write to.
RANSOMWARE_QUERY = (
    "(Get-MpPreference).EnableControlledFolderAccess"
)

# Products worth naming on a Mac, and where they leave themselves. The last few
# are not antivirus at all but outbound firewalls and campus network agents,
# which block pixi in exactly the same way and are worth the same sentence.
MACOS_PRODUCTS = (
    ("Sophos", ("/Applications/Sophos*", "/Library/Sophos Anti-Virus")),
    ("ESET", ("/Applications/ESET*",)),
    ("Kaspersky", ("/Applications/Kaspersky*", "/Library/Application Support/Kaspersky Lab")),
    ("Bitdefender", ("/Applications/Bitdefender*", "/Library/Bitdefender")),
    ("Norton", ("/Applications/Norton*",)),
    ("McAfee", ("/Applications/McAfee*", "/Library/McAfee")),
    ("Avast", ("/Applications/Avast*",)),
    ("AVG", ("/Applications/AVG*",)),
    ("Malwarebytes", ("/Applications/Malwarebytes*",)),
    ("Trend Micro", ("/Applications/Trend Micro*",)),
    ("F-Secure", ("/Applications/F-Secure*",)),
    ("Webroot", ("/Applications/Webroot*",)),
    ("Microsoft Defender", ("/Applications/Microsoft Defender*",)),
    ("CrowdStrike Falcon", ("/Applications/Falcon.app", "/Library/CS")),
    ("SentinelOne", ("/Applications/SentinelOne*", "/Library/Sentinel")),
    ("Cisco Secure Endpoint", ("/opt/cisco/amp", "/Library/Application Support/Cisco/AMP*")),
    ("Jamf Protect", ("/Library/Application Support/JamfProtect",)),
    ("Netskope", ("/Library/Application Support/Netskope", "/Applications/Netskope*")),
    ("Zscaler", ("/Applications/Zscaler*", "/Library/Application Support/Zscaler")),
    ("Little Snitch", ("/Applications/Little Snitch*",
                       "/Library/Application Support/Objective Development/Little Snitch")),
    ("LuLu", ("/Applications/LuLu.app",)),
)

# Windows' own antivirus, under the two names it reports itself by. It is not
# the problem, so it should not be reported as one.
BUILT_IN = ("windows defender", "microsoft defender")


@dataclass
class Survey:
    """What is running here, and whether we managed to ask."""

    products: list[str] = field(default_factory=list)
    asked: bool = True
    raw: list[str] = field(default_factory=list)

    @property
    def third_party(self) -> list[str]:
        return [p for p in self.products
                if not any(name in p.lower() for name in BUILT_IN)]

    @property
    def built_in(self) -> list[str]:
        return [p for p in self.products
                if any(name in p.lower() for name in BUILT_IN)]


def powershell(script: str, timeout: float = 25.0) -> str | None:
    """Run one PowerShell line and return its output, or None if we could not.

    None means the question could not be asked, which is a different answer
    from "nothing found" and has to stay distinguishable from it: a machine
    that will not say what antivirus it has is not a machine without one.
    """
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        return None
    try:
        finished = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if finished.returncode != 0:
        return None
    return finished.stdout


def windows_survey() -> Survey:
    """The antivirus products Windows knows about."""
    output = powershell(ANTIVIRUS_QUERY)
    if output is None:
        return Survey(asked=False)
    names = [line.strip() for line in output.splitlines() if line.strip()]
    return Survey(products=names, raw=names)


def controlled_folder_access() -> bool | None:
    """Whether Windows is blocking writes to Documents and Desktop, if it will say."""
    output = powershell(RANSOMWARE_QUERY)
    if output is None:
        return None
    answer = output.strip().lower()
    if answer in ("1", "true"):
        return True
    if answer in ("0", "false", "2"):
        return False
    return None


def macos_survey() -> Survey:
    """The security products a Mac is carrying, found where they install themselves."""
    found = []
    for name, patterns in MACOS_PRODUCTS:
        for pattern in patterns:
            if glob.glob(pattern):
                found.append(name)
                break
    return Survey(products=found, raw=found)


def survey(system: str | None = None) -> Survey:
    """What is installed here, asked the way this operating system answers."""
    system = system or ("Windows" if sys.platform == "win32"
                        else "Darwin" if sys.platform == "darwin"
                        else "Linux")
    if system == "Windows":
        return windows_survey()
    if system == "Darwin":
        return macos_survey()
    return Survey(products=[], asked=False)


def pixi_locations(home: Path | None = None) -> list[Path]:
    """Where pixi puts itself, whether or not the terminal can see it.

    A student who ran the installer and then read "pixi is not installed" has
    almost always just not opened a new terminal since, and the difference
    matters: one is a five-second fix and the other is a download.
    """
    home = home or Path.home()
    candidates = [home / ".pixi" / "bin" / "pixi", home / ".pixi" / "bin" / "pixi.exe"]
    return [path for path in candidates if path.exists()]
