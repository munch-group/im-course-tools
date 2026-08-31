"""The conflicting-extension repair, exercised against a stand-in for VS Code.

Nothing here runs the real `code`: an extension a student installed is not this
test suite's to uninstall, and a test that only passes on a machine with VS Code
on it is a test that fails in CI. The stub records the arguments it was called
with, which is what these assertions are actually about.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from im_course_tools import editor

CONFLICTS = json.dumps({
    "extensions": [
        {"id": "ms-python.vscode-python-envs", "why": "It has no pixi support."},
        {"id": "some.other-extension", "why": ""},
    ]
}).encode("utf-8")


@pytest.fixture
def fake_code(tmp_path: Path):
    """A `code` that lists two extensions and logs every call."""
    log = tmp_path / "calls.log"
    script = tmp_path / "code"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'if [ "$1" = "--list-extensions" ]; then\n'
        "  echo ms-python.python\n"
        "  echo MS-Python.VSCode-Python-Envs\n"      # marketplace casing differs
        "  echo ms-toolsai.jupyter\n"
        "fi\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def test_conflicts_reads_the_published_list():
    named = editor.conflicts(CONFLICTS)
    assert [c.id for c in named] == ["ms-python.vscode-python-envs", "some.other-extension"]
    assert named[0].why == "It has no pixi support."


@pytest.mark.parametrize("published", [None, b"", b"not json", b"{}", b'{"extensions": 3}',
                                       b'{"extensions": [{"no-id": 1}]}'])
def test_a_list_that_is_not_a_list_yields_nothing(published):
    """`im update` repairs a broken environment; a broken list must not stop it."""
    assert editor.conflicts(published) == []


def test_installed_is_compared_without_case(fake_code):
    script, _ = fake_code
    assert "ms-python.vscode-python-envs" in editor.installed(script)


def test_only_what_is_installed_is_reported(fake_code):
    script, _ = fake_code
    here = editor.conflicting_here(CONFLICTS, script)
    assert [c.id for c in here] == ["ms-python.vscode-python-envs"]


def test_repair_uninstalls_the_conflict_and_says_why(fake_code, monkeypatch):
    script, log = fake_code
    monkeypatch.setattr(editor, "code_command", lambda: script)

    said: list[str] = []
    removed = editor.repair(CONFLICTS, said.append)

    assert removed == 1
    calls = log.read_text()
    assert "--uninstall-extension ms-python.vscode-python-envs" in calls
    assert "some.other-extension" not in calls          # not installed, left alone
    printed = "\n".join(said)
    assert "Removed the VS Code extension ms-python.vscode-python-envs." in printed
    assert "It has no pixi support." in printed
    assert "Reload VS Code" in printed


def test_repair_does_nothing_without_vs_code(monkeypatch):
    monkeypatch.setattr(editor, "code_command", lambda: None)
    said: list[str] = []
    assert editor.repair(CONFLICTS, said.append) == 0
    assert said == []


def test_repair_is_quiet_when_there_is_nothing_to_remove(tmp_path, monkeypatch):
    script = tmp_path / "code"
    script.write_text('#!/bin/sh\nif [ "$1" = "--list-extensions" ]; then echo ms-python.python; fi\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(editor, "code_command", lambda: script)

    said: list[str] = []
    assert editor.repair(CONFLICTS, said.append) == 0
    assert said == []


def test_a_failed_uninstall_tells_the_student_what_to_do(tmp_path, monkeypatch):
    script = tmp_path / "code"
    script.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "--list-extensions" ]; then echo ms-python.vscode-python-envs; exit 0; fi\n'
        'exit 1\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(editor, "code_command", lambda: script)

    said: list[str] = []
    assert editor.repair(CONFLICTS, said.append) == 0
    printed = "\n".join(said)
    assert "Could not remove" in printed
    assert "Extensions view" in printed
    assert "Reload VS Code" not in printed


def test_code_command_finds_the_one_inside_the_application(tmp_path, monkeypatch):
    """The macOS case: no `code` on PATH, but VS Code is installed."""
    application = tmp_path / "Visual Studio Code.app"
    inside = application / "Contents/Resources/app/bin/code"
    inside.parent.mkdir(parents=True)
    inside.write_text("#!/bin/sh\n")

    monkeypatch.setattr(editor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(editor, "APPLICATIONS", ((str(application), "Contents/Resources/app/bin/code"),))
    assert editor.code_command() == inside


def test_code_command_prefers_the_one_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(editor.shutil, "which", lambda _name: str(tmp_path / "code"))
    assert editor.code_command() == tmp_path / "code"
