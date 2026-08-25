"""Tests for `im doctor`.

Nothing here touches the network or the machine it runs on. The one check that
would, the one that opens a connection to every host pixi downloads from, is
handed made-up answers instead, because the interesting cases — a certificate
signed by an antivirus, one host blocked while the rest work — are exactly the
ones that cannot be arranged on demand.
"""

import json
import platform
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from im_course_tools import checks, doctor, probe
from im_course_tools.checks import FAIL, OK, WARN, Context
from im_course_tools.cli import main
from im_course_tools.security import Survey


@pytest.fixture
def course(tmp_path: Path) -> Path:
    """A student's course folder, recognised by its pixi manifest."""
    folder = tmp_path / "instructing-machines"
    folder.mkdir()
    (folder / "pixi.toml").write_text("[workspace]\nname = 'instructing-machines'\n")
    (folder / "pixi.lock").write_text("version: 6\n")
    return folder


@pytest.fixture
def here(tmp_path: Path) -> Context:
    return Context(system=platform.system(), cwd=tmp_path)


def build(course: Path, manifest: Path | None) -> Path:
    """An installed environment, stamped with the manifest pixi built it from."""
    env = course.joinpath(*checks.ENV_PATH)
    python = env / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("")
    if manifest is not None:
        record = env.joinpath(*checks.ENV_RECORD)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"manifest_path": str(manifest),
                                      "environment_name": "default"}))
    return python


def statuses(findings) -> list[str]:
    return [finding.status for finding in findings]


def written(findings) -> str:
    """Everything a finding would put on the screen, as one searchable string."""
    parts = []
    for finding in findings:
        parts += [finding.title, *finding.detail, *finding.advice]
    return "\n".join(parts)


# --- reading a certificate -------------------------------------------------- #

@pytest.mark.parametrize("issuer, product", [
    ("Kaspersky Anti-Virus Personal Root Certificate", "Kaspersky"),
    ("ESET SSL Filter CA", "ESET"),
    ("Avast Web/Mail Shield Root", "Avast"),
    ("Bitdefender Personal CA.Net-Defender", "Bitdefender"),
    ("Zscaler Inc.", "Zscaler"),
    ("Fortinet Ltd.", "FortiGate"),
])
def test_a_product_that_signs_its_own_certificates_is_named(issuer, product):
    assert probe.interceptor(issuer) == product


def test_a_real_authority_is_not_mistaken_for_one():
    assert probe.interceptor("DigiCert Inc") is None
    assert probe.interceptor(None) is None


def test_the_authorities_the_course_hosts_actually_use_are_recognised():
    for issuer in ("Let's Encrypt", "GlobalSign nv-sa", "Sectigo Limited",
                   "DigiCert Inc", "Amazon", "Google Trust Services LLC"):
        assert probe.public_authority(issuer), issuer


def test_an_authority_nobody_has_heard_of_is_not_waved_through():
    assert not probe.public_authority("Some Company Root CA")


# --- asking pixi itself ----------------------------------------------------- #

BOXED = """Using channels: conda-forge
Error:   × Request failed after 3 retries
  ├─▶ error sending request for url (https://conda.anaconda.org/conda-forge/
  │   noarch/repodata_shards.msgpack.zst)
  ├─▶ client error (Connect)
  ╰─▶ invalid peer certificate: UnknownIssuer
"""


def test_pixis_error_survives_being_taken_out_of_its_box():
    """It is drawn as a tree and wrapped mid-url, and neither travels."""
    lines = probe.readable(BOXED)
    assert lines[0] == "Request failed after 3 retries"
    assert lines[-1] == "invalid peer certificate: UnknownIssuer"
    assert "conda-forge/noarch/repodata_shards.msgpack.zst)" in lines[1]


class Finished:
    """What subprocess.run would have handed back."""

    def __init__(self, returncode, stderr=""):
        self.returncode, self.stderr, self.stdout = returncode, stderr, ""


def fake_pixi(monkeypatch, answer):
    monkeypatch.setattr(probe.shutil, "which", lambda _: "/somewhere/pixi")
    if isinstance(answer, Exception):
        def run(*a, **k):
            raise answer
    else:
        def run(*a, **k):
            return answer
    monkeypatch.setattr(probe.subprocess, "run", run)


@pytest.mark.parametrize("said, outcome", [
    ("invalid peer certificate: UnknownIssuer", probe.REFUSED),
    ("tcp connect error: Connection refused (os error 61)", probe.STOPPED),
    ("error: unrecognised option '--platform'", probe.PUZZLING),
])
def test_the_way_pixi_failed_is_told_apart(monkeypatch, said, outcome):
    fake_pixi(monkeypatch, Finished(1, said))
    assert probe.pixi_download().outcome == outcome


def test_pixi_downloading_is_the_whole_answer(monkeypatch):
    fake_pixi(monkeypatch, Finished(0))
    assert probe.pixi_download().ok


def test_pixi_still_trying_is_not_pixi_succeeding(monkeypatch):
    fake_pixi(monkeypatch, subprocess.TimeoutExpired("pixi", 45))
    assert probe.pixi_download(timeout=45).outcome == probe.SLOW


def test_without_pixi_the_question_is_not_asked_rather_than_answered(monkeypatch):
    """Not asked and nothing wrong have to stay different answers."""
    monkeypatch.setattr(probe.shutil, "which", lambda _: None)
    result = probe.pixi_download()
    assert result.outcome == probe.NOT_TRIED
    assert not result.ok and not result.failed
    assert not probe.public_authority(None)


def test_the_issuer_is_read_off_a_certificate():
    certificate = {"issuer": ((("countryName", "US"),),
                              (("organizationName", "Let's Encrypt"),),
                              (("commonName", "R11"),))}
    assert probe.issuer_of(certificate) == "Let's Encrypt"
    assert probe.issuer_of({"issuer": ((("commonName", "R11"),),)}) == "R11"
    assert probe.issuer_of(None) is None


# --- finding the course folder ---------------------------------------------- #

def test_the_course_folder_is_reported_when_it_is_there(here, course, monkeypatch):
    monkeypatch.setenv("IM_COURSE_FOLDER", str(course))
    finding = checks.folder_check(here)
    assert finding.status == OK
    assert here.folder == course


def test_being_outside_the_course_folder_is_a_failure(here, monkeypatch):
    monkeypatch.delenv("IM_COURSE_FOLDER", raising=False)
    monkeypatch.setattr(checks, "search_briefly", lambda *a, **k: [])
    finding = checks.folder_check(here)
    assert finding.status == FAIL
    assert here.folder is None
    assert "VS Code" in written([finding])


def test_a_lost_course_folder_is_looked_for_where_downloads_land(tmp_path):
    home = tmp_path / "home"
    (home / "Desktop" / "instructing-machines").mkdir(parents=True)
    (home / "Desktop" / "instructing-machines" / "pixi.toml").write_text("[workspace]\n")
    (home / "Documents" / "unrelated").mkdir(parents=True)
    assert checks.likely_course_folders(home=home) == \
        [home / "Desktop" / "instructing-machines"]


def test_the_search_gives_up_rather_than_hanging(tmp_path, monkeypatch):
    def never_finishes(*args, **kwargs):
        import time
        time.sleep(30)
        return ["should not arrive"]

    monkeypatch.setattr(checks, "likely_course_folders", never_finishes)
    assert checks.search_briefly(seconds=0.2) == []


# --- the ways an ordinary-looking path breaks pixi -------------------------- #

def test_a_course_folder_inside_onedrive_is_flagged(here):
    here.folder = Path("/Users/student/OneDrive - Aarhus universitet/instructing-machines")
    finding = checks.cloud_finding(here, here.folder)
    assert finding.status == WARN
    assert "OneDrive" in finding.title


def test_a_course_folder_inside_icloud_is_flagged(here):
    here.folder = Path("/Users/s/Library/Mobile Documents/com~apple~CloudDocs/course")
    assert checks.cloud_finding(here, here.folder).status == WARN


def test_letters_that_some_tools_mishandle_are_flagged(here):
    finding = checks.letters_finding(here, Path("/Users/Kasper Mønch/kurset"))
    assert finding.status == WARN
    assert "ø" in written([finding])


def test_a_plain_path_has_nothing_said_about_its_letters(here):
    assert checks.letters_finding(here, Path("/Users/kasper/course")) is None


def test_a_long_windows_path_is_flagged(here, monkeypatch):
    monkeypatch.setattr(checks, "long_paths_enabled", lambda: False)
    here.system = "Windows"
    finding = checks.length_finding(here, Path("C:/Users/student/" + "a" * 120))
    assert finding.status == FAIL
    assert "260" in written([finding])


def test_a_long_path_matters_less_when_windows_allows_it(here, monkeypatch):
    monkeypatch.setattr(checks, "long_paths_enabled", lambda: True)
    here.system = "Windows"
    assert checks.length_finding(here, Path("C:/Users/student/" + "a" * 100)) is None


def test_the_same_path_is_fine_on_a_mac(here):
    here.system = "Darwin"
    assert checks.length_finding(here, Path("/Users/student/" + "a" * 120)) is None


def test_a_course_folder_on_a_network_share_is_flagged(here):
    here.system = "Windows"
    finding = checks.drive_finding(here, Path(r"\\campus\home\student\course"))
    assert finding.status == WARN
    assert "network share" in finding.title


def test_an_unremarkable_path_says_so(here, course):
    here.folder = course
    assert statuses(checks.path_checks(here)) == [OK]


def test_the_path_is_not_examined_when_there_is_no_course_folder(here):
    assert checks.path_checks(here) == []


# --- room and permission ---------------------------------------------------- #

def test_a_full_disk_is_a_failure(here, course, monkeypatch):
    here.folder = course
    monkeypatch.setattr(checks.shutil, "disk_usage",
                        lambda _: type("U", (), {"free": 1e9, "total": 500e9})())
    finding = checks.disk_check(here)
    assert finding.status == FAIL


def test_a_folder_that_cannot_be_written_to_is_a_failure(here, tmp_path):
    here.folder = tmp_path / "not-there"
    finding = checks.writable_check(here)
    assert finding.status == FAIL


def test_writing_leaves_nothing_behind(here, course):
    assert checks.writable_check(here.__class__(system=here.system, cwd=course,
                                                folder=course)).status == OK
    assert list(course.glob(".im-doctor*")) == []


# --- the environment -------------------------------------------------------- #

def test_an_environment_that_was_never_installed_is_a_failure(here, course):
    here.folder = course
    findings = checks.environment_check(here)
    assert statuses(findings) == [FAIL]
    assert "pixi install" in written(findings)


def test_an_installed_environment_is_found(here, course):
    python = course / ".pixi" / "envs" / "default" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    here.folder = course
    assert statuses(checks.environment_check(here)) == [OK]
    assert here.env_python == python


def test_a_manifest_newer_than_its_lock_file_warns(here, course):
    python = course / ".pixi" / "envs" / "default" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    lock = course / "pixi.lock"
    import os
    os.utime(lock, (0, 0))
    here.folder = course
    assert statuses(checks.environment_check(here)) == [OK, WARN]


def test_an_environment_built_for_another_folder_is_a_failure(here, course):
    here.env_python = build(course, manifest=course.parent / "old-name" / "pixi.toml")
    here.folder = course
    finding = checks.moved_check(here)
    assert finding.status == FAIL
    assert "pixi clean" in written([finding])
    assert str(course.parent / "old-name") in written([finding])


def test_an_environment_built_where_it_stands_is_fine(here, course):
    here.env_python = build(course, manifest=course / "pixi.toml")
    here.folder = course
    assert checks.moved_check(here).status == OK


def test_the_same_folder_reached_by_a_link_has_not_moved(here, course, tmp_path):
    build(course, manifest=course / "pixi.toml")
    link = tmp_path / "shortcut"
    try:
        link.symlink_to(course, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not allow symlinks")
    here.folder = link
    here.env_python = link.joinpath(*checks.ENV_PATH, "bin", "python")
    assert checks.moved_check(here).status == OK


def test_an_environment_that_never_said_where_it_was_built_is_not_guessed_about(here, course):
    here.env_python = build(course, manifest=None)
    here.folder = course
    assert checks.moved_check(here) is None


def test_nothing_is_said_about_an_environment_that_is_not_there(here, course):
    here.folder = course
    assert checks.moved_check(here) is None


def test_packages_missing_from_the_environment_are_named(here, course, monkeypatch):
    here.env_python = course / "python"
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: "steps-widget\nim-pytest\n")
    finding = checks.packages_check(here)
    assert finding.status == FAIL
    assert finding.detail == ["steps-widget", "im-pytest"]
    assert "im update" in written([finding])


def test_an_environment_that_will_not_answer_warns(here, course, monkeypatch):
    here.env_python = course / "python"
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: None)
    assert checks.packages_check(here).status == WARN


def test_a_globally_installed_im_says_so_without_interrupting_anybody(here, course):
    """It is what the course recommends, so it is not a thing to go and do."""
    here.folder, here.env_python = course, course / "python"
    finding = checks.interpreter_check(here)
    assert finding.status == OK
    assert "pixi --quiet run im check" in written([finding])


# --- the terminal the student is typing into -------------------------------- #

def shell_is(here, monkeypatch, name, login=None):
    """A terminal whose own process says it is running `name`."""
    monkeypatch.setattr(checks, "parent_name", lambda system: name)
    if login is None:
        monkeypatch.delenv("SHELL", raising=False)
    else:
        monkeypatch.setenv("SHELL", login)
    here.shell = None
    return here


def test_the_shell_in_use_is_asked_of_the_process_not_of_the_environment(here, monkeypatch):
    shell_is(here, monkeypatch, "bash", login="/bin/zsh")
    assert checks.shell_of(here) == "bash"


def test_a_login_shell_is_not_a_different_shell(here, monkeypatch):
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: "-zsh\n")
    monkeypatch.setattr(checks.os, "getppid", lambda: 1)
    assert checks.parent_name("Darwin") == "zsh"


def test_a_shell_named_by_its_whole_path_is_still_named(monkeypatch):
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: "/bin/zsh\n")
    assert checks.parent_name("Darwin") == "zsh"


def test_what_started_im_is_ignored_when_it_is_not_a_shell(here, monkeypatch):
    """`pixi run im doctor` makes pixi the parent, and pixi reads no startup file."""
    shell_is(here, monkeypatch, "pixi", login="/bin/zsh")
    assert checks.shell_of(here) == "zsh"


def test_the_shell_is_asked_once_however_many_checks_want_it(here, monkeypatch):
    asked = []
    monkeypatch.setattr(checks, "parent_name", lambda system: asked.append(1) or "zsh")
    here.shell = None
    checks.shell_of(here)
    checks.shell_of(here)
    assert len(asked) == 1


def test_the_shell_this_terminal_runs_is_reported(here, monkeypatch):
    shell_is(here, monkeypatch, "zsh", login="/bin/zsh")
    finding = checks.shell_finding(here)
    assert finding.status == OK
    assert finding.title == "zsh"
    assert finding.detail == []


def test_a_shell_that_is_not_the_login_one_is_worth_a_line(here, monkeypatch):
    shell_is(here, monkeypatch, "bash", login="/bin/zsh")
    assert "SHELL=/bin/zsh" in written([checks.shell_finding(here)])


def test_each_shell_is_looked_for_in_the_files_it_actually_reads(tmp_path):
    assert checks.startup_files("zsh", "Darwin", tmp_path)[0] == tmp_path / ".zshrc"
    assert checks.startup_files("bash", "Darwin", tmp_path)[0] == tmp_path / ".bash_profile"
    assert checks.startup_files("bash", "Linux", tmp_path)[0] == tmp_path / ".bashrc"
    assert checks.startup_files("fish", "Darwin", tmp_path)[0] == \
        tmp_path / ".config" / "fish" / "config.fish"
    assert checks.startup_files("tcsh", "Darwin", tmp_path) == []


def test_the_line_that_puts_pixi_on_path_is_found_wherever_it_is(tmp_path):
    (tmp_path / ".zshrc").write_text("# nothing here\n")
    (tmp_path / ".zprofile").write_text('export PATH="$HOME/.pixi/bin:$PATH"\n')
    files = checks.startup_files("zsh", "Darwin", tmp_path)
    assert checks.pixi_on_startup(files) == tmp_path / ".zprofile"


def test_no_startup_file_mentioning_pixi_is_told_from_one_that_does(tmp_path):
    (tmp_path / ".zshrc").write_text("alias ll='ls -l'\n")
    assert checks.pixi_on_startup(checks.startup_files("zsh", "Darwin", tmp_path)) is None


def test_fish_is_given_the_line_fish_understands():
    assert "fish_add_path" in checks.path_line("fish")
    assert checks.path_line("zsh").startswith("export PATH=")


def pixi_installed_but_unseen(here, monkeypatch, tmp_path, shell="zsh"):
    """pixi on disk where the installer puts it, and not on this terminal's PATH."""
    monkeypatch.setattr(checks.shutil, "which", lambda _: None)
    monkeypatch.setattr(checks.security, "pixi_locations",
                        lambda *a: [tmp_path / ".pixi" / "bin" / "pixi"])
    monkeypatch.setattr(checks.Path, "home", classmethod(lambda cls: tmp_path))
    shell_is(here, monkeypatch, shell, login=f"/bin/{shell}")
    here.system = "Darwin"
    return here


def test_a_shell_whose_startup_file_never_heard_of_pixi_is_told_which_file(
        here, monkeypatch, tmp_path):
    pixi_installed_but_unseen(here, monkeypatch, tmp_path, shell="bash")
    (tmp_path / ".zshrc").write_text('export PATH="$HOME/.pixi/bin:$PATH"\n')
    finding = checks.pixi_check(here)
    assert finding.status == FAIL
    said = written([finding])
    assert "Opening a new terminal will not help" in said
    assert "echo 'export PATH=\"$HOME/.pixi/bin:$PATH\"' >> ~/.bash_profile" in said


def test_a_startup_file_that_does_have_it_is_told_to_open_a_new_terminal(
        here, monkeypatch, tmp_path):
    pixi_installed_but_unseen(here, monkeypatch, tmp_path)
    (tmp_path / ".zshrc").write_text('export PATH="$HOME/.pixi/bin:$PATH"\n')
    finding = checks.pixi_check(here)
    assert finding.status == FAIL
    assert "Close this terminal completely" in written([finding])


def test_windows_is_not_sent_to_edit_a_startup_file(here, monkeypatch, tmp_path):
    pixi_installed_but_unseen(here, monkeypatch, tmp_path)
    here.system = "Windows"
    assert "Close this terminal completely" in written([checks.pixi_check(here)])


def on_path_from(here, monkeypatch, tmp_path, where):
    here.system, here.pixi = "Darwin", str(where)
    monkeypatch.setattr(checks.Path, "home", classmethod(lambda cls: tmp_path))
    return shell_is(here, monkeypatch, "zsh", login="/bin/zsh")


def test_pixi_on_path_that_no_startup_file_puts_there_is_a_warning(
        here, monkeypatch, tmp_path):
    on_path_from(here, monkeypatch, tmp_path, tmp_path / ".pixi" / "bin" / "pixi")
    (tmp_path / ".zshrc").write_text("# an empty one\n")
    finding = checks.pixi_path_finding(here)
    assert finding.status == WARN
    assert "echo 'export PATH=" in written([finding])
    assert "~/.zshrc" in written([finding])


def test_pixi_on_path_because_a_startup_file_puts_it_there_is_fine(
        here, monkeypatch, tmp_path):
    on_path_from(here, monkeypatch, tmp_path, tmp_path / ".pixi" / "bin" / "pixi")
    (tmp_path / ".zshrc").write_text('export PATH="$HOME/.pixi/bin:$PATH"\n')
    finding = checks.pixi_path_finding(here)
    assert finding.status == OK
    assert "~/.zshrc" in finding.title


def test_a_pixi_installed_somewhere_else_gets_no_opinion(here, monkeypatch, tmp_path):
    on_path_from(here, monkeypatch, tmp_path, Path("/opt/homebrew/bin/pixi"))
    (tmp_path / ".zshrc").write_text("# an empty one\n")
    assert checks.pixi_path_finding(here).status == OK


def test_the_startup_file_is_not_windows_business(here, monkeypatch, tmp_path):
    on_path_from(here, monkeypatch, tmp_path, tmp_path / ".pixi" / "bin" / "pixi")
    here.system = "Windows"
    assert checks.pixi_path_finding(here) is None


# --- what powershell is allowed to run -------------------------------------- #

def listing(**scopes) -> str:
    """`Get-ExecutionPolicy -List`, with every scope not named left Undefined."""
    return "".join(f"{scope}={scopes.get(scope, 'Undefined')}\n"
                   for scope in checks.POLICY_SCOPES)


def policy(monkeypatch, effective, **scopes):
    """A machine whose PowerShell answers exactly this."""
    said = None if effective is None else f"{effective}\n" + listing(**scopes)
    monkeypatch.setattr(checks.security, "powershell", lambda *a, **k: said)


def test_powershell_refusing_to_run_scripts_is_a_warning_with_the_one_command(
        here, monkeypatch):
    here.system = "Windows"
    policy(monkeypatch, "Restricted")
    finding = checks.scripts_finding(here)
    assert finding.status == WARN
    said = written([finding])
    assert "disabled on this system" in said       # the words the student saw
    assert "Set-ExecutionPolicy RemoteSigned -Scope CurrentUser" in said


def test_a_policy_the_student_may_change_is_told_from_one_they_may_not(here, monkeypatch):
    here.system = "Windows"
    policy(monkeypatch, "AllSigned", MachinePolicy="AllSigned")
    finding = checks.scripts_finding(here)
    assert finding.status == WARN
    said = written([finding])
    assert "Set-ExecutionPolicy RemoteSigned" not in said
    assert "-ExecutionPolicy Bypass" in said
    assert "IT support" in said


def test_a_policy_that_allows_scripts_is_fine(here, monkeypatch):
    here.system = "Windows"
    policy(monkeypatch, "RemoteSigned", CurrentUser="RemoteSigned")
    finding = checks.scripts_finding(here)
    assert finding.status == OK
    assert "set for CurrentUser" in written([finding])


def test_windows_refusing_to_say_is_not_read_as_a_refusal_to_run(here, monkeypatch):
    here.system = "Windows"
    policy(monkeypatch, None)
    assert checks.scripts_finding(here) is None


def test_a_mac_is_not_asked_about_powershell(here, monkeypatch):
    here.system = "Darwin"
    policy(monkeypatch, "Restricted")
    assert checks.scripts_finding(here) is None


# A window started with `powershell -ExecutionPolicy Bypass` runs everything
# until it is closed, which is exactly the state the advice above leaves a
# student in. Reading only what is in force here would call that fixed.

def test_a_window_started_with_the_rule_set_aside_is_not_an_all_clear(here, monkeypatch):
    here.system = "Windows"
    policy(monkeypatch, "Bypass", Process="Bypass", LocalMachine="Restricted")
    finding = checks.scripts_finding(here)
    assert finding.status == WARN
    said = written([finding])
    assert "next window" in finding.title
    assert "This window is enforcing Bypass, set for Process" in said
    assert "A new window would enforce Restricted, set for LocalMachine" in said
    assert "Set-ExecutionPolicy RemoteSigned -Scope CurrentUser" in said


def test_a_bypass_window_over_a_machine_with_nothing_set_still_warns(here, monkeypatch):
    """Nothing outside the window is set, so what is left is Windows' default."""
    here.system = "Windows"
    policy(monkeypatch, "Bypass", Process="Bypass")
    finding = checks.scripts_finding(here)
    assert finding.status == WARN
    assert "A new window would enforce Restricted" in written([finding])


def test_a_bypass_window_under_a_rule_it_cannot_change_says_so(here, monkeypatch):
    here.system = "Windows"
    policy(monkeypatch, "Bypass", Process="Bypass", MachinePolicy="AllSigned")
    said = written([checks.scripts_finding(here)])
    assert "Set-ExecutionPolicy RemoteSigned" not in said
    assert "-ExecutionPolicy Bypass" in said


def test_a_window_that_alone_refuses_is_told_to_open_another(here, monkeypatch):
    """The other way round, and the only case whose fix is not a command."""
    here.system = "Windows"
    policy(monkeypatch, "Restricted", Process="Restricted", CurrentUser="RemoteSigned")
    finding = checks.scripts_finding(here)
    assert finding.status == WARN
    said = written([finding])
    assert "though a new one would" in finding.title
    assert "Close this one and open a new terminal." in said
    assert "Set-ExecutionPolicy" not in said


def test_the_policy_in_force_and_the_one_that_lasts_are_read_apart(monkeypatch):
    monkeypatch.setattr(checks.security, "powershell",
                        lambda *a, **k: "Bypass\n" + listing(Process="Bypass",
                                                             CurrentUser="AllSigned"))
    answer = checks.execution_policy()
    assert (answer.effective, answer.scope) == ("Bypass", "Process")
    assert (answer.lasting, answer.lasting_scope) == ("AllSigned", "CurrentUser")


# --- a zip unpacked one level too deep -------------------------------------- #

def unpacked_twice(tmp_path: Path) -> Path:
    """What "Extract all" leaves behind when its default destination is taken."""
    outer = tmp_path / "instructing-machines"
    inner = outer / "instructing-machines"
    inner.mkdir(parents=True)
    (inner / "pixi.toml").write_text("[workspace]\n")
    return outer


def test_the_folder_inside_this_one_is_found(tmp_path):
    outer = unpacked_twice(tmp_path)
    assert checks.folder_inside(outer) == outer / "instructing-machines"


def test_a_folder_with_no_course_folder_under_it_is_not_invented(tmp_path):
    (tmp_path / "holiday photos").mkdir()
    assert checks.folder_inside(tmp_path) is None


def test_standing_one_folder_above_the_course_folder_says_which_one_to_go_to(
        tmp_path, monkeypatch):
    outer = unpacked_twice(tmp_path)
    monkeypatch.delenv("IM_COURSE_FOLDER", raising=False)
    here = Context(system="Windows", cwd=outer)
    finding = checks.folder_check(here)
    assert finding.status == FAIL
    said = written([finding])
    assert "the one inside this one" in finding.title
    assert f'cd "{outer / "instructing-machines"}"' in said
    assert "Extract all" in said
    assert "downloading again" in said


def test_a_mac_is_not_told_about_a_button_it_does_not_have(tmp_path, monkeypatch):
    outer = unpacked_twice(tmp_path)
    monkeypatch.delenv("IM_COURSE_FOLDER", raising=False)
    said = written([checks.folder_check(Context(system="Darwin", cwd=outer))])
    assert "Extract all" not in said
    assert "Finder" in said


def test_a_folder_below_that_is_not_a_doubled_one_is_only_pointed_at(tmp_path, monkeypatch):
    (tmp_path / "Downloads" / "the-course").mkdir(parents=True)
    (tmp_path / "Downloads" / "the-course" / "pixi.toml").write_text("[workspace]\n")
    monkeypatch.delenv("IM_COURSE_FOLDER", raising=False)
    said = written([checks.folder_check(Context(system="Windows", cwd=tmp_path / "Downloads"))])
    assert "unpacked into a new folder" not in said
    assert f'cd "{tmp_path / "Downloads" / "the-course"}"' in said


def test_the_wider_search_is_still_what_answers_when_nothing_is_below(tmp_path, monkeypatch):
    monkeypatch.delenv("IM_COURSE_FOLDER", raising=False)
    monkeypatch.setattr(checks, "search_briefly", lambda *a, **k: [])
    finding = checks.folder_check(Context(system="Darwin", cwd=tmp_path))
    assert finding.title == "You are not in your course folder"


# --- an environment built somewhere else, without pixi having said so ------- #

def kernel(course: Path, python: Path) -> None:
    """The kernel spec a notebook starts, naming the Python it was built with."""
    spec = course.joinpath(*checks.ENV_PATH, *checks.KERNEL_SPEC)
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(json.dumps({"argv": [str(python), "-m", "ipykernel_launcher",
                                         "-f", "{connection_file}"]}))


def test_the_kernel_still_names_the_folder_the_environment_was_built_in(here, course, tmp_path):
    here.env_python = build(course, manifest=None)
    kernel(course, tmp_path / "old-name" / ".pixi" / "envs" / "default" / "bin" / "python")
    here.folder = course
    finding = checks.moved_check(here)
    assert finding.status == FAIL
    assert str(tmp_path / "old-name") in written([finding])
    assert "pixi clean" in written([finding])


def test_a_kernel_naming_this_very_folder_is_not_a_move(here, course):
    here.env_python = build(course, manifest=None)
    kernel(course, course.joinpath(*checks.ENV_PATH, "bin", "python"))
    here.folder = course
    assert checks.moved_check(here).status == OK


def test_a_script_written_by_pixi_says_it_too(here, course, tmp_path):
    here.env_python = build(course, manifest=None)
    pip = course.joinpath(*checks.ENV_PATH, "bin", "pip")
    old = tmp_path / "old-name" / ".pixi" / "envs" / "default" / "bin" / "python"
    pip.write_text(f"#!{old}\nimport sys\n")
    here.folder = course
    assert str(tmp_path / "old-name") in written([checks.moved_check(here)])


def test_pixis_own_stamp_is_believed_over_the_rest(here, course, tmp_path):
    """It is rewritten on every install; the others can be left over from one."""
    here.env_python = build(course, manifest=course / "pixi.toml")
    kernel(course, tmp_path / "old-name" / ".pixi" / "envs" / "default" / "bin" / "python")
    here.folder = course
    assert checks.moved_check(here).status == OK


def test_a_python_that_is_not_in_a_pixi_folder_at_all_says_nothing(here, course):
    here.env_python = build(course, manifest=None)
    kernel(course, Path("/usr/local/bin/python3"))
    here.folder = course
    assert checks.moved_check(here) is None


# --- whether the environment is being used ---------------------------------- #

def nothing_active(monkeypatch):
    for name in ("PIXI_PROJECT_ROOT", "PIXI_ENVIRONMENT_NAME", "CONDA_PREFIX",
                 "CONDA_DEFAULT_ENV", "VIRTUAL_ENV"):
        monkeypatch.delenv(name, raising=False)


def ready(here, course):
    """A course folder whose environment is built, which is when this is asked."""
    here.folder, here.env_python = course, build(course, manifest=course / "pixi.toml")
    return here


def test_the_course_environment_being_active_is_the_answer_wanted(here, course, monkeypatch):
    nothing_active(monkeypatch)
    monkeypatch.setenv("CONDA_PREFIX", str(course.joinpath(*checks.ENV_PATH)))
    monkeypatch.setenv("PIXI_PROJECT_ROOT", str(course))
    finding = checks.activation_check(ready(here, course))
    assert finding.status == OK
    assert "active in this terminal" in finding.title


def test_nothing_activated_is_said_plainly_with_how_to_activate(here, course, monkeypatch):
    nothing_active(monkeypatch)
    finding = checks.activation_check(ready(here, course))
    assert finding.status == WARN
    said = written([finding])
    assert "pixi shell" in said
    assert "pixi --quiet run im check" in said


def test_a_conda_environment_in_the_way_names_itself(here, course, monkeypatch):
    nothing_active(monkeypatch)
    monkeypatch.setenv("CONDA_PREFIX", "/opt/anaconda3")
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    finding = checks.activation_check(ready(here, course))
    assert finding.status == WARN
    said = written([finding])
    assert "base is active" in finding.title
    assert "conda deactivate" in said
    assert "auto_activate_base false" in said


def test_a_virtual_environment_is_left_the_way_virtual_environments_are(
        here, course, monkeypatch):
    nothing_active(monkeypatch)
    monkeypatch.setenv("VIRTUAL_ENV", "/Users/student/venv")
    finding = checks.activation_check(ready(here, course))
    assert finding.status == WARN
    assert "deactivate" in written([finding])


def test_another_projects_pixi_environment_is_told_apart(here, course, monkeypatch, tmp_path):
    nothing_active(monkeypatch)
    monkeypatch.setenv("PIXI_PROJECT_ROOT", str(tmp_path / "some-other-project"))
    monkeypatch.setenv("CONDA_PREFIX", str(tmp_path / "some-other-project" / ".pixi"))
    finding = checks.activation_check(ready(here, course))
    assert finding.status == WARN
    said = written([finding])
    assert "another folder" in finding.title
    assert f'cd "{course}"' in said


def test_a_second_environment_of_this_folder_is_named_as_that(here, course, monkeypatch):
    nothing_active(monkeypatch)
    monkeypatch.setenv("PIXI_PROJECT_ROOT", str(course))
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "docs")
    monkeypatch.setenv("CONDA_PREFIX", str(course / ".pixi" / "envs" / "docs"))
    finding = checks.activation_check(ready(here, course))
    assert finding.status == WARN
    assert "docs" in finding.title


def test_an_environment_that_is_not_built_is_not_told_to_be_activated(here, course, monkeypatch):
    nothing_active(monkeypatch)
    here.folder = course
    assert checks.activation_check(here) is None


# --- security software ------------------------------------------------------ #

def installed(here, monkeypatch, products, survey=None):
    """A machine carrying `products`, with the ransomware setting off."""
    monkeypatch.setattr(checks.security, "survey",
                        lambda _: survey or Survey(products=products))
    monkeypatch.setattr(checks.security, "controlled_folder_access", lambda: False)


def answered(here, results=None, download=None):
    """The look at the network, already taken, so that no check takes one.

    Setting both is what makes `looked_at_the_network` believe it has already
    happened, which is the point: none of these tests may touch a socket.
    """
    here.probes = [probe.Probe("conda.anaconda.org", probe.REACHED, issuer="DigiCert Inc")] \
        if results is None else results
    here.download = probe.Download(probe.DOWNLOADED) if download is None else download


def test_antivirus_that_is_only_installed_is_not_a_job_to_do(here, monkeypatch):
    """The whole point: nearly every laptop has one and nearly none is at fault."""
    installed(here, monkeypatch, ["Bitdefender Total Security"])
    answered(here)
    findings = checks.security_check(here)
    assert statuses(findings) == [OK]
    assert "Bitdefender" in written(findings)
    assert "exclusions" not in written(findings)


def test_antivirus_that_is_in_the_way_gets_the_whole_paragraph(here, monkeypatch):
    installed(here, monkeypatch, ["Kaspersky Internet Security"])
    answered(here, download=probe.Download(probe.REFUSED, ["invalid peer certificate"]))
    findings = checks.security_check(here)
    assert statuses(findings) == [WARN]
    assert "Kaspersky" in written(findings)
    assert "exclusions" in written(findings)


def test_a_blocked_host_is_enough_to_ask_about_the_antivirus(here, monkeypatch):
    installed(here, monkeypatch, ["ESET Security", "Little Snitch"])
    answered(here, results=[
        probe.Probe("conda.anaconda.org", probe.UNREACHABLE, error="timed out"),
        probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc"),
    ])
    findings = checks.security_check(here)
    assert statuses(findings) == [WARN]
    assert "ESET Security" in written(findings)      # both are named underneath
    assert "Little Snitch" in written(findings)


def test_no_connection_at_all_is_not_blamed_on_the_antivirus(here, monkeypatch):
    """Switched-off wifi is not evidence about anything else."""
    installed(here, monkeypatch, ["Norton 360"])
    answered(here, results=[probe.Probe(host, probe.UNREACHABLE, error="timed out")
                            for host in ("pypi.org", "github.com")],
             download=probe.Download(probe.NOT_TRIED))
    findings = checks.security_check(here)
    assert statuses(findings) == [WARN]
    assert "exclusions" not in written(findings)
    assert "was not checked" in written(findings)


def test_offline_says_it_could_not_tell_rather_than_guessing(here, monkeypatch):
    here.offline = True
    installed(here, monkeypatch, ["Norton 360"])
    findings = checks.security_check(here)
    assert statuses(findings) == [WARN]
    assert "exclusions" not in written(findings)
    assert "--offline" in written(findings)


def test_the_antivirus_windows_comes_with_is_not_worth_one(here, monkeypatch):
    here.system = "Windows"
    installed(here, monkeypatch, ["Windows Defender"])
    answered(here)
    assert statuses(checks.security_check(here)) == [OK]


def test_windows_refusing_to_say_is_not_read_as_nothing_installed(here, monkeypatch):
    here.system = "Windows"
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(asked=False))
    monkeypatch.setattr(checks.security, "controlled_folder_access", lambda: None)
    answered(here, download=probe.Download(probe.STOPPED, ["tcp connect error"]))
    findings = checks.security_check(here)
    assert statuses(findings) == [WARN]
    assert "could not rule them out" in written(findings)


def test_windows_refusing_to_say_is_let_go_when_pixi_is_getting_through(here, monkeypatch):
    here.system = "Windows"
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(asked=False))
    monkeypatch.setattr(checks.security, "controlled_folder_access", lambda: False)
    answered(here)
    assert statuses(checks.security_check(here)) == [OK]


def test_controlled_folder_access_is_reported_separately(here, monkeypatch):
    here.system = "Windows"
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(products=[]))
    monkeypatch.setattr(checks.security, "controlled_folder_access", lambda: True)
    answered(here)
    assert statuses(checks.security_check(here)) == [OK, WARN]


def test_defender_is_told_apart_from_the_rest():
    survey = Survey(products=["Windows Defender", "ESET Security"])
    assert survey.third_party == ["ESET Security"]
    assert survey.built_in == ["Windows Defender"]


# --- the network ------------------------------------------------------------ #

def fake_probes(here, monkeypatch, results, download=None):
    monkeypatch.setattr(checks.probe, "probe_all", lambda hosts, **kw: results)
    monkeypatch.setattr(checks.probe, "clock_offset", lambda *a, **k: 0.0)
    monkeypatch.setattr(checks.probe, "pixi_download",
                        lambda *a, **k: download or probe.Download(probe.DOWNLOADED))


def test_offline_skips_the_network_but_says_why(here):
    here.offline = True
    findings = checks.network_check(here)
    assert statuses(findings) == [WARN]
    assert "--offline" in written(findings)


def test_an_intercepted_connection_names_the_product(here, monkeypatch):
    fake_probes(here, monkeypatch, [
        probe.Probe("conda.anaconda.org", probe.INTERCEPTED,
                    issuer="Kaspersky Web Anti-Virus", vendor="Kaspersky"),
        probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc"),
    ])
    findings = checks.network_check(here)
    assert FAIL in statuses(findings)
    assert "Kaspersky" in findings[0].title
    assert "HTTPS or SSL scanning" in written(findings)


def test_a_certificate_that_will_not_verify_is_a_failure(here, monkeypatch):
    fake_probes(here, monkeypatch, [
        probe.Probe("pypi.org", probe.UNVERIFIED, error="self signed certificate"),
        probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc"),
    ], download=probe.Download(probe.NOT_TRIED))
    findings = checks.network_check(here)
    assert findings[0].status == FAIL
    assert "clock" in written(findings)


def test_a_certificate_only_this_python_cannot_check_is_a_smaller_thing(here, monkeypatch):
    """A conda environment that has been moved loses its own list of authorities.

    Every host then looks tampered with to `im` and none of them is, which the
    old wording called a failure and told the student to pause their antivirus
    over. pixi fetching a file from the same host while this was being checked
    is what tells the two apart.
    """
    fake_probes(here, monkeypatch, [
        probe.Probe("pypi.org", probe.UNVERIFIED, error="unable to get local issuer"),
        probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc"),
    ])
    findings = checks.network_check(here)
    assert FAIL not in statuses(findings)
    assert "im update" in written(findings)
    assert "pause your antivirus" not in written(findings)


def test_an_unfamiliar_authority_is_only_a_warning(here, monkeypatch):
    fake_probes(here, monkeypatch, [
        probe.Probe("pypi.org", probe.UNKNOWN_CA, issuer="Acme Corp Root"),
        probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc"),
    ])
    findings = checks.network_check(here)
    assert FAIL not in statuses(findings)
    assert WARN in statuses(findings)


def test_nothing_reachable_at_all_is_said_once(here, monkeypatch):
    fake_probes(here, monkeypatch, [
        probe.Probe(host, probe.UNREACHABLE, error="timed out")
        for host in ("pypi.org", "github.com", "prefix.dev")
    ])
    findings = checks.network_check(here)
    assert len(findings) == 1
    assert "cannot reach the internet at all" in findings[0].title


def test_one_blocked_host_is_told_apart_from_being_offline(here, monkeypatch):
    fake_probes(here, monkeypatch, [
        probe.Probe("conda.anaconda.org", probe.UNREACHABLE, error="timed out"),
        probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc"),
    ])
    findings = checks.network_check(here)
    assert "the internet itself is fine" in written(findings)
    assert OK in statuses(findings)


def test_a_wrong_clock_is_noticed(here, monkeypatch):
    fake_probes(here, monkeypatch,
                [probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc")])
    monkeypatch.setattr(checks.probe, "clock_offset", lambda *a, **k: 7200.0)
    findings = checks.network_check(here)
    assert "clock is wrong" in written(findings)


def test_pixi_downloading_for_itself_is_the_line_that_settles_it(here, monkeypatch):
    fake_probes(here, monkeypatch,
                [probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc")])
    findings = checks.network_check(here)
    assert "pixi downloaded a file of its own" in written(findings)
    assert FAIL not in statuses(findings)


def test_a_certificate_pixi_will_not_accept_is_a_failure(here, monkeypatch):
    """Python trusts what the machine trusts; pixi does not, and pixi is the one failing."""
    fake_probes(here, monkeypatch,
                [probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc")],
                download=probe.Download(probe.REFUSED,
                                        ["invalid peer certificate: UnknownIssuer"]))
    findings = checks.network_check(here)
    assert FAIL in statuses(findings)
    assert "invalid peer certificate: UnknownIssuer" in written(findings)
    assert "HTTPS or SSL scanning" in written(findings)


def test_pixi_failing_for_a_reason_nobody_listed_is_only_a_warning(here, monkeypatch):
    fake_probes(here, monkeypatch,
                [probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc")],
                download=probe.Download(probe.PUZZLING, ["error: something new"]))
    findings = checks.network_check(here)
    assert FAIL not in statuses(findings)
    assert "error: something new" in written(findings)


def test_the_long_paragraph_is_written_out_once_and_pointed_at_after(here, monkeypatch):
    """Two checks, both right, and one explanation between them."""
    installed(here, monkeypatch, ["Kaspersky Internet Security"])
    fake_probes(here, monkeypatch, [
        probe.Probe("conda.anaconda.org", probe.INTERCEPTED,
                    issuer="Kaspersky Web Anti-Virus", vendor="Kaspersky"),
        probe.Probe("github.com", probe.REACHED, issuer="DigiCert Inc"),
    ], download=probe.Download(probe.REFUSED, ["invalid peer certificate"]))

    first = checks.security_check(here)
    second = checks.network_check(here)
    everything = written(first + second)
    assert everything.count("add your course folder to whatever it calls") == 1
    assert everything.count("This is the same thing as") == 2         # both later findings


# --- putting it together ---------------------------------------------------- #

def quiet(*findings):
    """A check list that produces exactly what a test asks it to."""
    return [lambda ctx, made=list(findings): made]


def test_a_clean_run_says_so_and_succeeds():
    lines = []
    code = doctor.diagnose(lines.append, checks=quiet(
        checks.Finding(OK, checks.MACHINE, "macOS")))
    assert code == 0
    assert "Nothing here looks wrong" in "\n".join(lines)


def test_only_what_can_be_acted_on_reaches_the_screen():
    lines = []
    doctor.diagnose(lines.append, checks=quiet(
        checks.Finding(OK, checks.MACHINE, "macOS"),
        checks.Finding(WARN, checks.PIXI, "something to fix",
                       detail=["seen at /somewhere"],
                       advice=["The long way round, which is why this is wrong."],
                       fix=["    do this"])))
    printed = "\n".join(lines)
    assert "something to fix" in printed
    assert "    do this" in printed
    assert "macOS" not in printed                  # nothing that was fine
    assert "The long way round" not in printed     # nor the reasoning behind it
    assert "seen at /somewhere" not in printed     # nor what it was read off


def test_a_finding_with_no_short_answer_falls_back_to_its_long_one():
    """Worse to read than a fix, and better than saying nothing at all."""
    lines = []
    doctor.diagnose(lines.append, checks=quiet(
        checks.Finding(FAIL, checks.PIXI, "something is wrong",
                       advice=["Here is the long way round."])))
    assert "Here is the long way round." in "\n".join(lines)


def test_every_block_has_a_blank_line_on_each_side_and_never_two():
    lines = []
    doctor.diagnose(lines.append, checks=quiet(
        checks.Finding(FAIL, checks.PIXI, "one", fix=["    do this"]),
        checks.Finding(WARN, checks.FOLDER, "two", fix=["", "    and this", ""])))
    assert lines[0] == ""
    assert lines[-1] == ""
    assert "\n\n\n" not in "\n".join(lines)
    for number, line in enumerate(lines):
        if line.startswith(("✓", "!", "✗", "+", "x")) and number:
            assert lines[number - 1] == "", f"no blank line before {line!r}"
            assert lines[number + 1] == "", f"no blank line after {line!r}"


# --- one step of the staircase at a time ------------------------------------ #

def not_activated(**extra):
    """The environment, installed and standing unused."""
    return checks.Finding(WARN, checks.ENVIRONMENT,
                          "The course environment is not active in this terminal",
                          fix=["Activate it, and run `im doctor` again once you have:",
                               "", "    pixi shell", "    im doctor"],
                          alone=["Everything looks fine, but the course environment is",
                                 "not active. Activate it:", "", "    pixi shell"],
                          **extra)


def test_an_unused_environment_on_a_machine_where_all_else_is_fine():
    lines = []
    code = doctor.diagnose(lines.append, checks=quiet(
        checks.Finding(OK, checks.MACHINE, "macOS"), not_activated()))
    printed = "\n".join(lines)
    assert code == 0
    assert "Everything looks fine" in printed
    assert "    pixi shell" in printed
    assert "im doctor" not in printed        # there is nothing to come back for


def test_an_unused_environment_comes_before_whatever_else_is_wrong():
    """No use handing somebody the fourth step while they stand on the second."""
    lines = []
    doctor.diagnose(lines.append, checks=quiet(
        not_activated(),
        checks.Finding(FAIL, checks.INTERNET, "a blocked host", fix=["    do this"])))
    printed = "\n".join(lines)
    assert "Activate it, and run `im doctor` again" in printed
    assert "a blocked host" not in printed   # it waits for the next run
    assert "Everything looks fine" not in printed


def test_the_thing_that_is_wrong_wins_when_activating_could_not_work():
    """`pixi shell` is not a step a student can climb without pixi."""
    lines = []
    doctor.diagnose(lines.append, checks=quiet(
        not_activated(),
        checks.Finding(FAIL, checks.PIXI, "pixi is not installed",
                       fix=["    install pixi"])))
    printed = "\n".join(lines)
    assert "pixi is not installed" in printed
    assert "    install pixi" in printed
    assert "Everything looks fine" not in printed


def test_a_terminal_that_cannot_run_scripts_wins_for_the_same_reason():
    lines = []
    doctor.diagnose(lines.append, checks=quiet(
        not_activated(),
        checks.Finding(WARN, checks.SHELL, "PowerShell is not allowed to run scripts",
                       fix=["    Set-ExecutionPolicy RemoteSigned -Scope CurrentUser"])))
    assert "Set-ExecutionPolicy" in "\n".join(lines)


def test_being_outside_the_folder_ends_the_command_there():
    """Nothing after it was looked at, so nothing after it is said."""
    looked = []
    lines = []
    doctor.diagnose(lines.append, checks=[
        lambda ctx: checks.Finding(FAIL, checks.FOLDER, "not in it",
                                   fix=["Please navigate to it."], stop=True),
        lambda ctx: looked.append(1) or checks.Finding(OK, checks.PIXI, "pixi"),
    ])
    assert looked == []                      # the rest of the checks never ran
    assert "\n".join(lines).strip() == "Please navigate to it."


def test_the_reasoning_is_kept_for_whoever_wants_it(tmp_path, monkeypatch):
    """It is moved out of the student's way, not thrown away."""
    monkeypatch.setenv("IM_COURSE_FOLDER", str(tmp_path))
    finding = checks.Finding(FAIL, checks.PIXI, "something is wrong",
                             advice=["The long way round."], fix=["    do this"])
    lines = []
    doctor.diagnose(lines.append, verbose=True, checks=quiet(finding))
    assert "The long way round." in "\n".join(lines)

    doctor.diagnose(lambda _: None, report=True, cwd=tmp_path, checks=quiet(finding))
    assert "The long way round." in (tmp_path / doctor.REPORT_NAME).read_text()


def test_verbose_shows_everything_that_was_looked_at():
    lines = []
    doctor.diagnose(lines.append, verbose=True, checks=quiet(
        checks.Finding(OK, checks.MACHINE, "macOS"),
        checks.Finding(WARN, checks.PIXI, "something to fix", advice=["fix it"])))
    printed = "\n".join(lines)
    assert "macOS" in printed
    assert "looked at and found fine" not in printed


def test_the_report_holds_the_lines_the_screen_left_out(tmp_path, monkeypatch):
    """The person reading a report is looking for what was ruled out."""
    monkeypatch.setenv("IM_COURSE_FOLDER", str(tmp_path))
    doctor.diagnose(lambda _: None, report=True, cwd=tmp_path, checks=quiet(
        checks.Finding(OK, checks.MACHINE, "macOS"),
        checks.Finding(FAIL, checks.PIXI, "pixi is not installed", advice=["install it"])))
    assert "macOS" in (tmp_path / doctor.REPORT_NAME).read_text()


def test_a_warning_on_its_own_does_not_fail_the_command():
    lines = []
    code = doctor.diagnose(lines.append, checks=quiet(
        checks.Finding(WARN, checks.FOLDER, "inside OneDrive", advice=["move it"])))
    assert code == 0
    assert "move it" in "\n".join(lines)


def test_a_failure_fails_the_command_and_is_listed_first():
    lines = []
    code = doctor.diagnose(lines.append, checks=quiet(
        checks.Finding(WARN, checks.FOLDER, "a warning", advice=["later"]),
        checks.Finding(FAIL, checks.PIXI, "a failure", advice=["first"])))
    assert code == 1
    printed = "\n".join(lines)
    assert printed.index("a failure") < printed.index("a warning")


def test_a_check_that_crashes_costs_only_itself():
    def explodes(ctx):
        raise RuntimeError("a corner nobody foresaw")

    lines = []
    code = doctor.diagnose(lines.append, verbose=True, checks=[
        explodes, lambda ctx: checks.Finding(OK, checks.PIXI, "pixi 0.53.0")])
    assert code == 0                      # a fault in the doctor is not a fault in the setup
    printed = "\n".join(lines)
    assert "could not be run" in printed
    assert "pixi 0.53.0" in printed       # the check after the crash still ran


def test_the_report_holds_the_findings_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("IM_COURSE_FOLDER", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_for_sharing")
    lines = []
    doctor.diagnose(lines.append, report=True, cwd=tmp_path, checks=quiet(
        checks.Finding(FAIL, checks.PIXI, "pixi is not installed", advice=["install it"])))
    written_out = (tmp_path / doctor.REPORT_NAME).read_text()
    assert "pixi is not installed" in written_out
    assert "install it" in written_out
    assert "ghp_not_for_sharing" not in written_out


def test_the_symbols_fall_back_when_the_terminal_cannot_print_them():
    class Ancient:
        encoding = "cp437"

    assert doctor.marks(Ancient())[OK] == "+"

    class Modern:
        encoding = "utf-8"

    assert doctor.marks(Modern())[OK] == "✓"


def test_the_command_runs_from_outside_a_course_folder(tmp_path, monkeypatch):
    """`im doctor` is the one command that must work where the others refuse."""
    monkeypatch.delenv("IM_COURSE_FOLDER", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(checks, "search_briefly", lambda *a, **k: [])
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(products=[]))
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: None)
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert result.exit_code == 1                      # no course folder is a failure
    assert result.output.strip() == (
        "Please navigate to your instructing-machines folder using the cd\n"
        "command, and run `im doctor` again once you are there.")


def test_the_command_runs_inside_one(course, monkeypatch):
    monkeypatch.setenv("IM_COURSE_FOLDER", str(course))
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(products=[]))
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: None)
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert "--offline" in result.output               # the internet was not looked at
    assert "im doctor --report" in result.output      # and where to go if this fails


def test_verbose_is_where_the_whole_scan_still_lives(course, monkeypatch):
    monkeypatch.setenv("IM_COURSE_FOLDER", str(course))
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(products=[]))
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: None)
    result = CliRunner().invoke(main, ["doctor", "--offline", "-v"])
    assert str(course) in result.output
    assert "Internet access" in result.output
