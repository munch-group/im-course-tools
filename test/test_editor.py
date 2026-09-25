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


def remember(data: Path, key: str, uri: str, when: float) -> Path:
    """One folder as VS Code records it, last used at `when`."""
    entry = data / "User" / "workspaceStorage" / key
    entry.mkdir(parents=True)
    (entry / "workspace.json").write_text(json.dumps({"folder": uri}))
    (entry / "state.vscdb").write_bytes(b"")
    for path in (entry / "workspace.json", entry / "state.vscdb", entry):
        os.utime(path, (when, when))
    return entry


def test_opened_folders_are_read_with_when_they_were_last_used(tmp_path):
    course = tmp_path / "course folder"
    remember(tmp_path, "a", course.as_uri(), 1000)
    remember(tmp_path, "b", (course / "week1").as_uri(), 2000)

    found = sorted(editor.opened_folders(tmp_path), key=lambda o: o.when)
    assert [(o.folder, o.when) for o in found] == [(course, 1000), (course / "week1", 2000)]


def test_the_newest_file_in_a_folder_s_storage_is_when_it_was_used(tmp_path):
    entry = remember(tmp_path, "a", (tmp_path / "x").as_uri(), 1000)
    os.utime(entry / "state.vscdb", (5000, 5000))
    assert editor.opened_folders(tmp_path)[0].when == 5000


def test_what_is_not_a_local_folder_is_passed_over(tmp_path):
    remember(tmp_path, "remote", "vscode-remote://ssh-remote%2Bhost/home/a", 1)
    entry = tmp_path / "User" / "workspaceStorage" / "multi-root"
    entry.mkdir(parents=True)
    (entry / "workspace.json").write_text('{"workspace": "file:///a/b.code-workspace"}')
    broken = tmp_path / "User" / "workspaceStorage" / "broken"
    broken.mkdir()
    (broken / "workspace.json").write_text("not json")
    assert editor.opened_folders(tmp_path) == []


def test_no_vscode_at_all_is_no_folders(tmp_path):
    assert editor.opened_folders(tmp_path / "nothing here") == []


@pytest.mark.parametrize("uri, expected", [
    ("file:///Users/a/instructing-machines", "/Users/a/instructing-machines"),
    ("file:///Users/a/my%20course/week1", "/Users/a/my course/week1"),
    ("file:///c%3A/Users/a/instructing-machines", "c:/Users/a/instructing-machines"),
    ("vscode-remote://wsl%2Bubuntu/home/a", None),
    ("file://server/share/course", None),
])
def test_a_folder_uri_becomes_the_path_it_names(uri, expected):
    found = editor.folder_from_uri(uri)
    assert found == (None if expected is None else Path(expected))


def test_vscode_keeps_its_memory_somewhere_different_on_each_system(tmp_path):
    home = tmp_path
    assert editor.user_data_dir("Darwin", home, {}) == \
        home / "Library" / "Application Support" / "Code"
    assert editor.user_data_dir("Windows", home, {"APPDATA": str(tmp_path / "Roaming")}) == \
        tmp_path / "Roaming" / "Code"
    assert editor.user_data_dir("Linux", home, {}) == home / ".config" / "Code"
