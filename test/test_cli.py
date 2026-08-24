"""Tests for the `im` command.

Each test runs against a fake course website on disk, reached through a file://
URL, and a fake course folder marked by a pixi.toml. Both are pointed at with
the same environment variables a person would use to try the tool against a
local preview, so nothing here touches the real site.
"""

import io
import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from im_course_tools.cli import main

CHAPTERS = ["iteration", "lists"]
PROJECTS = ["alignmentproject", "translationproject"]


def notebook_bytes(title: str) -> str:
    return json.dumps({
        "cells": [{"cell_type": "markdown", "metadata": {}, "source": [f"# {title}"]}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    })


# The course folder as the website publishes it: the six files `im update`
# keeps current, and two more that it must never touch.
TEMPLATE = {
    "pixi.toml": "[workspace]\nname = 'instructing-machines'\n",
    "pixi.lock": "version: 6\n",
    ".pin_pixi_path.py": "# tell VS Code where pixi is\n",
    ".gitignore": ".pixi/\n__pycache__/\n",
    ".vscode/settings.json": '{\n    // the course settings\n}\n',
    ".vscode/extensions.json": '{\n    "recommendations": []\n}\n',
    "week1/notebooks.ipynb": notebook_bytes("week one"),
    "data/data_table.csv": "a,b\n1,2\n",
}
ROOT = "instructing-machines"


def zip_bytes(entries: dict[str, str], root: str = ROOT) -> bytes:
    """One published course folder, laid out the way the book's build lays it out."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in entries.items():
            archive.writestr(f"{root}/{name}", text)
    return buffer.getvalue()


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A published course website: notebooks, project zips and the course folder."""
    site = tmp_path / "site"
    (site / "notebooks").mkdir(parents=True)
    for name in CHAPTERS:
        (site / "notebooks" / f"{name}.ipynb").write_text(notebook_bytes(name))
    (site / "notebooks" / "index.txt").write_text("\n".join(CHAPTERS) + "\n")

    (site / "project-files").mkdir()
    for name in PROJECTS:
        with zipfile.ZipFile(site / "project-files" / f"{name}.zip", "w") as zf:
            zf.writestr(f"{name}/{name}.py", "# write your code here\n")
            zf.writestr(f"{name}/test_{name}.py", "# the tests\n")
    (site / "project-files" / "index.txt").write_text("\n".join(PROJECTS) + "\n")

    # Published loose as well as inside the zip, exactly as the site does it.
    (site / "pixi.toml").write_text(TEMPLATE["pixi.toml"])
    (site / "pixi.lock").write_text(TEMPLATE["pixi.lock"])
    (site / f"{ROOT}.zip").write_bytes(zip_bytes(TEMPLATE))
    return site


@pytest.fixture
def course(tmp_path: Path) -> Path:
    """A student's course folder, recognised by its pixi manifest."""
    folder = tmp_path / "instructing-machines"
    folder.mkdir()
    (folder / "pixi.toml").write_text("[workspace]\nname = 'instructing-machines'\n")
    (folder / "pixi.lock").write_text("version: 6\n")
    return folder


@pytest.fixture
def run(site, course, monkeypatch):
    monkeypatch.setenv("IM_COURSE_URL", site.as_uri())
    monkeypatch.setenv("IM_COURSE_FOLDER", str(course))

    def invoke(*args):
        return CliRunner().invoke(main, list(args))
    return invoke


# --- listing --------------------------------------------------------------- #

def test_get_with_no_name_lists_both(run):
    result = run("get")
    assert result.exit_code == 0
    for name in CHAPTERS + PROJECTS:
        assert name in result.output


def test_a_site_without_projects_still_offers_chapters(run, site):
    (site / "project-files" / "index.txt").unlink()
    result = run("get")
    assert result.exit_code == 0
    assert "iteration" in result.output
    assert "alignmentproject" not in result.output


# --- chapters -------------------------------------------------------------- #

def test_get_saves_a_chapter(run, course):
    result = run("get", "iteration")
    assert result.exit_code == 0
    assert (course / "iteration.ipynb").exists()


def test_asking_twice_never_overwrites_a_notebook(run, course):
    run("get", "iteration")
    (course / "iteration.ipynb").write_text("my own edits")
    result = run("get", "iteration")
    assert result.exit_code == 0
    assert (course / "iteration.ipynb").read_text() == "my own edits"
    assert (course / "iteration-2.ipynb").exists()


def test_a_chapter_name_folds(run, course):
    assert run("get", "Iteration.ipynb").exit_code == 0
    assert (course / "iteration.ipynb").exists()


# --- projects -------------------------------------------------------------- #

def test_get_unpacks_a_project(run, course):
    result = run("get", "alignmentproject")
    assert result.exit_code == 0
    assert (course / "projects" / "alignmentproject" / "alignmentproject.py").exists()


@pytest.mark.parametrize("asked", ["alignment", "alignment-project", "alignment_project"])
def test_a_project_name_folds(run, course, asked):
    assert run("get", asked).exit_code == 0
    assert (course / "projects" / "alignmentproject").is_dir()


def test_asking_twice_never_overwrites_a_project(run, course):
    run("get", "alignmentproject")
    written = course / "projects" / "alignmentproject" / "alignmentproject.py"
    written.write_text("a week of my own work")
    result = run("get", "alignment")
    assert result.exit_code == 1
    assert written.read_text() == "a week of my own work"
    assert "left it alone" in result.output


def test_a_zip_cannot_write_outside_the_projects_folder(run, course, site):
    with zipfile.ZipFile(site / "project-files" / "alignmentproject.zip", "w") as zf:
        zf.writestr("../../escaped.py", "should never be written\n")
    result = run("get", "alignmentproject")
    assert result.exit_code == 1
    assert not (course.parent / "escaped.py").exists()
    assert not (course / "escaped.py").exists()


# --- not found, and nowhere to put it -------------------------------------- #

def test_a_near_miss_is_suggested(run):
    result = run("get", "iterashun")
    assert result.exit_code == 1
    assert "iteration" in result.output


def test_an_unknown_name_points_at_the_list(run):
    result = run("get", "bananas")
    assert result.exit_code == 1
    assert "im get" in result.output


def test_outside_a_course_folder_it_says_so(run, monkeypatch, tmp_path):
    monkeypatch.delenv("IM_COURSE_FOLDER")
    monkeypatch.chdir(tmp_path)
    result = run("get", "iteration")
    assert result.exit_code != 0
    assert "course folder" in result.output


def test_the_course_folder_is_found_from_a_subfolder(run, course, monkeypatch):
    deep = course / "week2" / "scratch"
    deep.mkdir(parents=True)
    monkeypatch.delenv("IM_COURSE_FOLDER")
    monkeypatch.chdir(deep)
    assert run("get", "lists").exit_code == 0
    assert (course / "lists.ipynb").exists()
    assert not (deep / "lists.ipynb").exists()


# --- the environment ------------------------------------------------------- #

def test_check_names_what_is_missing(run):
    result = run("check")
    # this test environment has none of the course widgets installed
    assert result.exit_code == 1
    assert "steps-widget" in result.output


@pytest.fixture
def stopping_at_install(monkeypatch):
    """`im update` up to the point where it would hand over to pixi."""
    monkeypatch.setenv("PATH", "/nonexistent")
    return monkeypatch


def test_update_replaces_an_old_file_and_keeps_what_was_there(run, course,
                                                              stopping_at_install):
    (course / "pixi.toml").write_text("[workspace]\nname = 'last year'\n")
    result = run("update")
    assert (course / "pixi.toml.backup").read_text() == "[workspace]\nname = 'last year'\n"
    assert (course / "pixi.toml").read_text() == TEMPLATE["pixi.toml"]
    assert "Updated pixi.toml, keeping your old one as pixi.toml.backup" in result.output
    assert "Could not find pixi" in result.output      # it went on to install
    assert result.exit_code == 1


def test_update_brings_the_configs_and_the_script_too(run, course, stopping_at_install):
    """The whole point: a fix to any of these used to reach nobody."""
    run("update")
    for name in (".gitignore", ".pin_pixi_path.py",
                 ".vscode/settings.json", ".vscode/extensions.json"):
        assert course.joinpath(*name.split("/")).read_text() == TEMPLATE[name]


def test_update_leaves_a_file_that_is_already_current_completely_alone(
        run, course, stopping_at_install):
    (course / ".gitignore").write_text(TEMPLATE[".gitignore"])
    result = run("update")
    assert not (course / ".gitignore.backup").exists()
    assert ".gitignore" not in result.output.split("Installing")[0]


def test_update_says_so_when_there_was_nothing_to_do(run, course, stopping_at_install):
    for name, text in TEMPLATE.items():
        if name in ("week1/notebooks.ipynb", "data/data_table.csv"):
            continue
        target = course.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    result = run("update")
    assert "All 6 of your course folder's files were already up to date." in result.output
    assert not list(course.rglob("*.backup"))


def test_update_counts_the_ones_it_did_not_have_to_touch(run, course, stopping_at_install):
    (course / "pixi.toml").write_text("[workspace]\nname = 'last year'\n")
    assert "The other 1 file was already up to date." in run("update").output


def test_update_does_not_touch_a_notebook_or_the_data(run, course, stopping_at_install):
    """Both are in the same download, and both are places a student works."""
    (course / "week1").mkdir()
    (course / "week1" / "notebooks.ipynb").write_text("my own work")
    (course / "data").mkdir()
    (course / "data" / "data_table.csv").write_text("mine\n")
    run("update")
    assert (course / "week1" / "notebooks.ipynb").read_text() == "my own work"
    assert (course / "data" / "data_table.csv").read_text() == "mine\n"


def test_update_ignores_line_endings_a_windows_editor_rewrote(run, course,
                                                              stopping_at_install):
    (course / ".gitignore").write_bytes(
        TEMPLATE[".gitignore"].replace("\n", "\r\n").encode())
    run("update")
    assert not (course / ".gitignore.backup").exists()


def test_update_still_works_when_the_course_folder_drops_a_file(run, course, site,
                                                                stopping_at_install):
    thinner = {k: v for k, v in TEMPLATE.items() if k != ".gitignore"}
    site.joinpath(f"{ROOT}.zip").write_bytes(zip_bytes(thinner))
    result = run("update")
    assert not (course / ".gitignore").exists()
    assert (course / ".pin_pixi_path.py").exists()
    assert result.exit_code == 1                      # only because pixi is missing


def test_update_refuses_a_page_that_is_not_the_course_folder(run, course, site):
    site.joinpath(f"{ROOT}.zip").write_bytes(b"<!DOCTYPE html><title>404</title>")
    before = (course / "pixi.toml").read_text()
    result = run("update")
    assert result.exit_code == 1
    assert (course / "pixi.toml").read_text() == before
    assert not list(course.rglob("*.backup"))


def test_update_refuses_a_download_with_no_environment_in_it(run, course, site):
    site.joinpath(f"{ROOT}.zip").write_bytes(
        zip_bytes({k: v for k, v in TEMPLATE.items() if k != "pixi.lock"}))
    result = run("update")
    assert result.exit_code == 1
    assert "no pixi.lock" in result.output
    assert not (course / ".pin_pixi_path.py").exists()   # nothing written at all


def test_update_refuses_a_manifest_that_is_not_a_manifest(run, course, site):
    site.joinpath(f"{ROOT}.zip").write_bytes(
        zip_bytes({**TEMPLATE, "pixi.toml": "<!DOCTYPE html><title>404</title>"}))
    before = (course / "pixi.toml").read_text()
    result = run("update")
    assert result.exit_code == 1
    assert (course / "pixi.toml").read_text() == before
    assert not (course / ".pin_pixi_path.py").exists()


def test_update_takes_the_folder_whatever_it_unpacks_into(run, course, site,
                                                          stopping_at_install):
    """The name of the folder in the zip is the website's business, not ours."""
    site.joinpath(f"{ROOT}.zip").write_bytes(zip_bytes(TEMPLATE, root="im-2027"))
    run("update")
    assert (course / ".pin_pixi_path.py").exists()
