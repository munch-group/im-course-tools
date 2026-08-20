"""Tests for `im doctor`.

Nothing here touches the network or the machine it runs on. The one check that
would, the one that opens a connection to every host pixi downloads from, is
handed made-up answers instead, because the interesting cases — a certificate
signed by an antivirus, one host blocked while the rest work — are exactly the
ones that cannot be arranged on demand.
"""

import json
import platform
from pathlib import Path

import pytest
from click.testing import CliRunner

from im_tools import checks, doctor, probe
from im_tools.checks import FAIL, OK, WARN, Context
from im_tools.cli import main
from im_tools.security import Survey


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


def test_a_globally_installed_im_says_so(here, course):
    here.folder, here.env_python = course, course / "python"
    finding = checks.interpreter_check(here)
    assert finding.status == WARN
    assert "pixi run im check" in written([finding])


# --- security software ------------------------------------------------------ #

def test_third_party_antivirus_is_worth_a_warning(here, monkeypatch):
    monkeypatch.setattr(checks.security, "survey",
                        lambda _: Survey(products=["Kaspersky Internet Security"]))
    monkeypatch.setattr(checks.security, "controlled_folder_access", lambda: False)
    findings = checks.security_check(here)
    assert statuses(findings) == [WARN]
    assert "Kaspersky" in written(findings)
    assert "exclusions" in written(findings)


def test_the_antivirus_windows_comes_with_is_not_worth_one(here, monkeypatch):
    here.system = "Windows"
    monkeypatch.setattr(checks.security, "survey",
                        lambda _: Survey(products=["Windows Defender"]))
    monkeypatch.setattr(checks.security, "controlled_folder_access", lambda: False)
    assert statuses(checks.security_check(here)) == [OK]


def test_windows_refusing_to_say_is_not_read_as_nothing_installed(here, monkeypatch):
    here.system = "Windows"
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(asked=False))
    monkeypatch.setattr(checks.security, "controlled_folder_access", lambda: None)
    findings = checks.security_check(here)
    assert statuses(findings) == [WARN]
    assert "could not rule them out" in written(findings)


def test_controlled_folder_access_is_reported_separately(here, monkeypatch):
    here.system = "Windows"
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(products=[]))
    monkeypatch.setattr(checks.security, "controlled_folder_access", lambda: True)
    assert statuses(checks.security_check(here)) == [OK, WARN]


def test_defender_is_told_apart_from_the_rest():
    survey = Survey(products=["Windows Defender", "ESET Security"])
    assert survey.third_party == ["ESET Security"]
    assert survey.built_in == ["Windows Defender"]


# --- the network ------------------------------------------------------------ #

def fake_probes(here, monkeypatch, results):
    monkeypatch.setattr(checks.probe, "probe_all", lambda hosts, **kw: results)
    monkeypatch.setattr(checks.probe, "clock_offset", lambda *a, **k: 0.0)


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
    ])
    findings = checks.network_check(here)
    assert findings[0].status == FAIL
    assert "clock" in written(findings)


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
    monkeypatch.setattr(checks.probe, "probe_all",
                        lambda hosts, **kw: [probe.Probe("github.com", probe.REACHED,
                                                         issuer="DigiCert Inc")])
    monkeypatch.setattr(checks.probe, "clock_offset", lambda *a, **k: 7200.0)
    findings = checks.network_check(here)
    assert "clock is wrong" in written(findings)


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
    assert printed.index("1. a failure") < printed.index("2. a warning")


def test_a_check_that_crashes_costs_only_itself():
    def explodes(ctx):
        raise RuntimeError("a corner nobody foresaw")

    lines = []
    code = doctor.diagnose(lines.append, checks=[
        explodes, lambda ctx: checks.Finding(OK, checks.PIXI, "pixi 0.53.0")])
    assert code == 0                      # a fault in the doctor is not a fault in the setup
    printed = "\n".join(lines)
    assert "could not be run" in printed
    assert "pixi 0.53.0" in printed


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
    assert "You are not in your course folder" in result.output
    assert "What to do" in result.output


def test_the_command_runs_inside_one(course, monkeypatch):
    monkeypatch.setenv("IM_COURSE_FOLDER", str(course))
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(products=[]))
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: None)
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert str(course) in result.output
    assert "Internet access" in result.output
