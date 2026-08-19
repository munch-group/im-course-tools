"""Tests for the `im` command.

Each test runs against a fake course website on disk, reached through a file://
URL, and a fake course folder marked by a pixi.toml. Both are pointed at with
the same environment variables a person would use to try the tool against a
local preview, so nothing here touches the real site.
"""

import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from im_tools.cli import main

CHAPTERS = ["iteration", "lists"]
PROJECTS = ["alignmentproject", "translationproject"]


def notebook_bytes(title: str) -> str:
    return json.dumps({
        "cells": [{"cell_type": "markdown", "metadata": {}, "source": [f"# {title}"]}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    })


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A published course website: notebooks, project zips and both manifests."""
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

    (site / "pixi.toml").write_text("[workspace]\nname = 'instructing-machines'\n")
    (site / "pixi.lock").write_text("version: 6\n")
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


def test_update_refreshes_both_files_and_keeps_backups(run, course, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")     # stop before `pixi install`
    (course / "pixi.toml").write_text("[workspace]\nname = 'old'\n")
    result = run("update")
    assert (course / "pixi.toml.backup").read_text() == "[workspace]\nname = 'old'\n"
    assert "instructing-machines" in (course / "pixi.toml").read_text()
    assert (course / "pixi.lock.backup").exists()
    assert "Could not find pixi" in result.output
    assert result.exit_code == 1


def test_update_refuses_a_page_that_is_not_a_manifest(run, course, site):
    site.joinpath("pixi.toml").write_text("<!DOCTYPE html><title>404</title>")
    before = (course / "pixi.toml").read_text()
    result = run("update")
    assert result.exit_code == 1
    assert (course / "pixi.toml").read_text() == before
    assert not (course / "pixi.toml.backup").exists()
