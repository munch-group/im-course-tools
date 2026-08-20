"""Tests for the version check and the upgrade.

None of this asks a package index anything. The two questions worth testing —
is this version older than that one, and what upgrades an install of this kind
— are answered from a string and from a folder layout, and both can be put on
disk exactly. The index itself is handed made-up answers.
"""

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from im_course_tools import release
from im_course_tools.cli import main
from im_course_tools.release import (CONDA, CONDA_GLOBAL, CONDA_PROJECT, PACKAGE, PIP,
                              PIPX, SOURCE, Install)


@pytest.fixture(autouse=True)
def a_fresh_start(monkeypatch, tmp_path):
    """The check switched on, no answer remembered, and nothing said yet.

    This is the one module where the version check is the thing being tested,
    so it undoes the suite-wide fixture that switches it off. The index is
    never actually asked: every test that gets that far replaces `ask_index`.
    """
    monkeypatch.delenv("IM_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(release, "_said", False)
    monkeypatch.setattr(release, "cache_file", lambda home=None: tmp_path / "latest.json")


def conda_prefix(where: Path, channel: str = "https://conda.anaconda.org/munch-group/",
                 version: str = "0.1.3") -> Path:
    """A prefix with the record conda writes when it installs the package."""
    meta = where / "conda-meta"
    meta.mkdir(parents=True)
    (meta / f"{PACKAGE}-{version}-pyhd8ed1ab_0.json").write_text(json.dumps(
        {"name": PACKAGE, "version": version, "channel": channel}))
    (where / "im_course_tools").mkdir(parents=True, exist_ok=True)
    return where


def code_in(prefix: Path) -> Path:
    """A path standing in for the module file, so the install counts as installed."""
    return prefix / "im_course_tools" / "release.py"


# --- is that one newer than this one ---------------------------------------- #

@pytest.mark.parametrize("version, numbers", [
    ("0.1.4", (0, 1, 4)),
    ("v0.1.4", (0, 1, 4)),
    ("1.0", (1, 0)),
    ("0.1.4.rc1", (0, 1, 4)),
    ("0.1.4rc1", (0, 1)),
    ("", ()),
])
def test_a_version_is_read_as_the_numbers_it_starts_with(version, numbers):
    assert release.as_numbers(version) == numbers


def test_a_later_version_is_newer():
    assert release.newer("0.1.4", "0.1.3")
    assert release.newer("0.2.0", "0.1.9")
    assert release.newer("1.0", "0.9.9")


def test_the_same_version_is_not():
    assert not release.newer("0.1.3", "0.1.3")


def test_an_earlier_version_is_not():
    assert not release.newer("0.1.2", "0.1.3")


def test_a_release_candidate_is_not_newer_than_the_release_it_precedes():
    """Erring this way means a student is never sent to something older."""
    assert not release.newer("0.1.4.rc1", "0.1.4")


def test_no_answer_at_all_is_not_newer():
    assert not release.newer(None, "0.1.3")
    assert not release.newer("", "0.1.3")


# --- how did this copy get here ---------------------------------------------- #

def test_a_pixi_global_install_is_recognised(tmp_path):
    home = tmp_path / "home"
    prefix = conda_prefix(home / ".pixi" / "envs" / PACKAGE)
    install = release.describe(prefix, home=home, code=code_in(prefix))
    assert install.kind == CONDA_GLOBAL
    assert install.version == "0.1.3"


def test_an_install_inside_a_course_environment_is_recognised(tmp_path):
    course = tmp_path / "instructing-machines"
    course.mkdir()
    (course / "pixi.toml").write_text("[workspace]\n")
    prefix = conda_prefix(course / ".pixi" / "envs" / "default")
    install = release.describe(prefix, home=tmp_path / "home", code=code_in(prefix))
    assert install.kind == CONDA_PROJECT
    assert install.project == course


def test_a_conda_environment_somewhere_else_is_recognised(tmp_path):
    prefix = conda_prefix(tmp_path / "miniconda3" / "envs" / "work")
    install = release.describe(prefix, home=tmp_path / "home", code=code_in(prefix))
    assert install.kind == CONDA


def test_a_pip_install_is_recognised(tmp_path):
    prefix = tmp_path / "venv"
    (prefix / "im_course_tools").mkdir(parents=True)
    assert release.describe(prefix, home=tmp_path, code=code_in(prefix)).kind == PIP


def test_a_pipx_install_is_recognised(tmp_path):
    prefix = tmp_path / ".local" / "pipx" / "venvs" / PACKAGE
    (prefix / "im_course_tools").mkdir(parents=True)
    assert release.describe(prefix, home=tmp_path, code=code_in(prefix)).kind == PIPX


def test_code_run_from_outside_the_prefix_is_a_checkout(tmp_path):
    """The dev case: what is installed is not what is running, so nothing to upgrade."""
    prefix = conda_prefix(tmp_path / "env")
    install = release.describe(prefix, home=tmp_path,
                               code=tmp_path / "checkout" / "src" / "release.py")
    assert install.kind == SOURCE
    assert not install.upgradable


@pytest.mark.parametrize("channel, owner", [
    ("https://conda.anaconda.org/munch-group/", "munch-group"),
    ("https://conda.anaconda.org/munch-group/noarch", "munch-group"),
    ("https://conda.anaconda.org/conda-forge/osx-arm64", "conda-forge"),
    ("", release.DEFAULT_OWNER),
])
def test_the_publishing_account_is_read_off_the_channel(channel, owner):
    assert release.owner_of({"channel": channel}) == owner


# --- remembering the answer --------------------------------------------------- #

def test_a_recent_answer_is_used_without_asking_again(monkeypatch):
    release.write_cache("9.9.9")
    monkeypatch.setattr(release, "ask_index", lambda *a, **k: pytest.fail("asked anyway"))
    assert release.latest(Install(PIP, "0.1.3", Path("/x"))) == "9.9.9"


def test_a_stale_answer_is_asked_again(monkeypatch):
    release.cache_file().parent.mkdir(parents=True, exist_ok=True)
    release.cache_file().write_text(json.dumps(
        {"asked": time.time() - release.GOOD_FOR - 1, "available": "0.0.1"}))
    monkeypatch.setattr(release, "ask_index", lambda *a, **k: "9.9.9")
    assert release.latest(Install(PIP, "0.1.3", Path("/x"))) == "9.9.9"


def test_a_question_that_could_not_be_asked_is_retried_sooner(monkeypatch):
    """Being offline should cost one timeout an hour, not one every command."""
    now = time.time()
    assert release.fresh({"asked": now, "available": None})
    assert not release.fresh({"asked": now - release.RETRY_AFTER - 1, "available": None})
    assert release.fresh({"asked": now - release.RETRY_AFTER - 1, "available": "1.0"})


def test_the_check_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setenv("IM_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(release, "ask_index", lambda *a, **k: pytest.fail("asked anyway"))
    assert release.latest() is None


# --- what upgrades it --------------------------------------------------------- #

@pytest.fixture
def tools(monkeypatch):
    """pixi, conda and pipx all present, so the command chosen is the tested part."""
    monkeypatch.setattr(release.shutil, "which", lambda name: f"/bin/{name}")


def test_a_global_install_is_upgraded_globally(tools):
    command, folder = release.upgrade_command(Install(CONDA_GLOBAL, "0.1.3", Path("/x")))
    assert command == ["/bin/pixi", "global", "update", PACKAGE]
    assert folder is None


def test_a_course_environment_is_upgraded_in_the_course_folder(tools, tmp_path):
    command, folder = release.upgrade_command(
        Install(CONDA_PROJECT, "0.1.3", Path("/x"), project=tmp_path))
    assert command == ["/bin/pixi", "update", PACKAGE]
    assert folder == tmp_path


def test_another_conda_environment_is_upgraded_from_its_own_channel(tools):
    command, _ = release.upgrade_command(
        Install(CONDA, "0.1.3", Path("/x"), owner="munch-group"))
    assert command[:2] == ["/bin/conda", "update"]
    assert "munch-group" in command


def test_a_pip_install_is_upgraded_with_pip(tools):
    command, _ = release.upgrade_command(Install(PIP, "0.1.3", Path("/x")))
    assert command[1:] == ["-m", "pip", "install", "--upgrade", PACKAGE]


def test_a_pipx_install_is_upgraded_with_pipx(tools):
    command, _ = release.upgrade_command(Install(PIPX, "0.1.3", Path("/x")))
    assert command == ["/bin/pipx", "upgrade", PACKAGE]


def test_a_checkout_has_nothing_to_run(tools):
    assert release.upgrade_command(Install(SOURCE, "0.1.3", Path("/x"))) is None


def test_a_conda_install_with_no_conda_to_drive_it_has_nothing_to_run(monkeypatch):
    monkeypatch.setattr(release.shutil, "which", lambda name: None)
    assert release.upgrade_command(Install(CONDA, "0.1.3", Path("/x"))) is None


# --- saying so ---------------------------------------------------------------- #

def test_nothing_is_said_when_this_is_the_current_version(tools):
    lines = []
    assert not release.say_if_newer(lines.append, "0.1.3", Install(PIP, "0.1.3", Path("/x")))
    assert lines == []


def test_a_newer_version_is_named_with_the_command_that_gets_it(tools):
    lines = []
    assert release.say_if_newer(lines.append, "0.2.0", Install(PIP, "0.1.3", Path("/x")))
    printed = "\n".join(lines)
    assert "0.1.3 -> 0.2.0" in printed
    assert "pip install --upgrade" in printed


def test_it_is_only_said_once(tools):
    lines = []
    install = Install(PIP, "0.1.3", Path("/x"))
    assert release.say_if_newer(lines.append, "0.2.0", install)
    assert not release.say_if_newer(lines.append, "0.2.0", install)


def test_a_checkout_is_told_it_cannot_be_upgraded(tools):
    lines = []
    release.say_if_newer(lines.append, "0.2.0", Install(SOURCE, "0.1.3", Path("/x")))
    assert "cannot upgrade" in "\n".join(lines)


def test_the_background_notice_stays_quiet_when_switched_off(monkeypatch):
    monkeypatch.setenv("IM_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(release, "describe", lambda *a, **k: pytest.fail("looked anyway"))
    lines = []
    release.announce_later(lines.append)
    assert lines == []


# --- doing it ----------------------------------------------------------------- #

@pytest.fixture
def upgradable(monkeypatch, tools):
    """An install one version behind, with the index saying so."""
    install = Install(PIP, "0.1.3", Path("/x"))
    monkeypatch.setattr(release, "describe", lambda *a, **k: install)
    monkeypatch.setattr(release, "ask_index", lambda *a, **k: "0.2.0")
    monkeypatch.setattr(release.sys.stdin, "isatty", lambda: True)
    return install


def ran(monkeypatch, returncode: int, becomes: str = "0.2.0") -> list:
    """Stand in for the upgrade, and for the version probe that follows it.

    `becomes` is what the package reports afterwards, which is how an upgrade
    that ran perfectly and changed nothing gets to be tested.
    """
    calls = []

    def fake_run(command, **kwargs):
        probing = any("importlib.metadata" in str(part) for part in command)
        if not probing:
            calls.append(command)
        return type("Finished", (), {
            "returncode": 0 if probing else returncode,
            "stdout": f"{becomes}\n",
        })()

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    return calls


def test_a_successful_upgrade_asks_for_the_command_to_be_run_again(upgradable, monkeypatch):
    calls = ran(monkeypatch, 0)
    lines = []
    assert release.upgrade_if_newer(lines.append, timeout=0.1) == 0
    assert len(calls) == 1
    printed = "\n".join(lines)
    assert "cannot swap itself out" in printed
    assert "run your command again" in printed


def test_an_upgrade_that_ran_and_changed_nothing_is_not_called_a_success(upgradable,
                                                                          monkeypatch):
    """Otherwise the student is sent round the same loop for as long as they will go."""
    ran(monkeypatch, 0, becomes="0.1.3")
    lines = []
    assert release.upgrade_if_newer(lines.append, timeout=0.1) == 1
    printed = "\n".join(lines)
    assert "still 0.1.3" in printed
    assert "run your command again" not in printed


def test_the_version_afterwards_is_read_off_disk_for_a_conda_install(tmp_path):
    """Not out of this process, which loaded its own version before any upgrade."""
    prefix = conda_prefix(tmp_path / "env", version="0.9.0")
    assert release.installed_now(Install(CONDA, "0.1.3", prefix)) == "0.9.0"


def test_a_failed_upgrade_stops_with_its_own_code_and_says_why(upgradable, monkeypatch):
    ran(monkeypatch, 3)
    lines = []
    assert release.upgrade_if_newer(lines.append, timeout=0.1) == 3
    assert "did not finish cleanly" in "\n".join(lines)
    assert "run your command again" not in "\n".join(lines)


def test_saying_no_carries_on_and_leaves_the_command_behind(upgradable, monkeypatch):
    calls = ran(monkeypatch, 0)
    lines = []
    assert release.upgrade_if_newer(lines.append, confirm=lambda q: False,
                                    timeout=0.1) is None
    assert calls == []
    assert "pip install --upgrade" in "\n".join(lines)


def test_saying_yes_goes_ahead(upgradable, monkeypatch):
    calls = ran(monkeypatch, 0)
    assert release.upgrade_if_newer(lambda line: None, confirm=lambda q: True,
                                    timeout=0.1) == 0
    assert len(calls) == 1


def test_nothing_is_asked_when_nobody_is_there_to_answer(upgradable, monkeypatch):
    """Piped to a file, the question would either hang or answer itself."""
    monkeypatch.setattr(release.sys.stdin, "isatty", lambda: False)
    calls = ran(monkeypatch, 0)
    assert release.upgrade_if_newer(lambda line: None,
                                    confirm=lambda q: pytest.fail("asked anyway"),
                                    timeout=0.1) == 0
    assert len(calls) == 1


def test_an_up_to_date_im_is_left_alone(monkeypatch, tools):
    monkeypatch.setattr(release, "describe",
                        lambda *a, **k: Install(PIP, "0.1.3", Path("/x")))
    monkeypatch.setattr(release, "ask_index", lambda *a, **k: "0.1.3")
    calls = ran(monkeypatch, 0)
    assert release.upgrade_if_newer(lambda line: None, timeout=0.1) is None
    assert calls == []


def test_a_checkout_is_never_upgraded_underneath_a_developer(monkeypatch, tools):
    monkeypatch.setattr(release, "describe",
                        lambda *a, **k: Install(SOURCE, "0.1.3", Path("/x")))
    monkeypatch.setattr(release, "ask_index", lambda *a, **k: pytest.fail("asked anyway"))
    assert release.upgrade_if_newer(lambda line: None, timeout=0.1) is None


# --- the commands -------------------------------------------------------------- #

@pytest.fixture
def course(tmp_path: Path) -> Path:
    folder = tmp_path / "instructing-machines"
    folder.mkdir()
    (folder / "pixi.toml").write_text("[workspace]\n")
    return folder


def test_update_upgrades_im_before_touching_the_environment(course, monkeypatch):
    """The refresh that follows should be done by the new code, not the old."""
    monkeypatch.setenv("IM_COURSE_FOLDER", str(course))
    monkeypatch.setattr(release, "upgrade_if_newer", lambda *a, **k: 0)
    monkeypatch.setattr("im_course_tools.environment.update",
                        lambda *a, **k: pytest.fail("carried on with the old code"))
    result = CliRunner().invoke(main, ["update"])
    assert result.exit_code == 0


def test_update_can_be_told_to_leave_im_alone(course, monkeypatch):
    monkeypatch.setenv("IM_COURSE_FOLDER", str(course))
    monkeypatch.setattr(release, "upgrade_if_newer",
                        lambda *a, **k: pytest.fail("upgraded anyway"))
    monkeypatch.setattr("im_course_tools.environment.update", lambda *a, **k: 0)
    assert CliRunner().invoke(main, ["update", "--no-upgrade"]).exit_code == 0


@pytest.fixture
def quick_doctor(monkeypatch):
    """The slow checks stood in for, so these tests are about the flag alone."""
    from im_course_tools import checks
    from im_course_tools.security import Survey
    monkeypatch.setattr(checks.security, "survey", lambda _: Survey(products=[]))
    monkeypatch.setattr(checks, "run_briefly", lambda *a, **k: None)
    monkeypatch.setattr(checks, "search_briefly", lambda *a, **k: [])


def test_doctor_can_be_told_to_leave_im_alone(monkeypatch, quick_doctor):
    monkeypatch.setattr(release, "upgrade_if_newer",
                        lambda *a, **k: pytest.fail("upgraded anyway"))
    result = CliRunner().invoke(main, ["doctor", "--offline", "--no-upgrade"])
    assert result.exit_code in (0, 1)


def test_doctor_offline_never_reaches_for_a_new_version(monkeypatch, quick_doctor):
    """--offline means offline: the index is a network call like any other."""
    monkeypatch.setattr(release, "upgrade_if_newer",
                        lambda *a, **k: pytest.fail("asked anyway"))
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert result.exit_code in (0, 1)


def test_doctor_reports_which_im_this_is(monkeypatch):
    from im_course_tools import checks
    monkeypatch.setattr(checks.release, "describe",
                        lambda *a, **k: Install(CONDA_GLOBAL, "0.1.3", Path("/x")))
    monkeypatch.setattr(checks.release, "known_latest", lambda: None)
    finding = checks.version_check(checks.Context(system="Darwin", cwd=Path("/x")))
    assert finding.status == "ok"
    assert "im 0.1.3" in finding.title
    assert "pixi global install" in "\n".join(finding.detail)


def test_doctor_warns_when_a_newer_im_is_already_known_about(monkeypatch, tools):
    from im_course_tools import checks
    monkeypatch.setattr(checks.release, "describe",
                        lambda *a, **k: Install(PIP, "0.1.3", Path("/x")))
    monkeypatch.setattr(checks.release, "known_latest", lambda: "0.2.0")
    finding = checks.version_check(checks.Context(system="Darwin", cwd=Path("/x")))
    assert finding.status == "warn"
    assert "0.2.0 is out" in finding.title
    assert "pip install --upgrade" in "\n".join(finding.advice)
