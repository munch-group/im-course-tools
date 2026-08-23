"""The `im` command: the one terminal tool the course asks students to learn.

    im check                 is my environment working?
    im doctor                why is it not working?
    im get iteration         a chapter notebook
    im get alignmentproject  a whole project
    im get                   everything on offer
    im update                refresh the environment from the website

Every command that writes something writes it into the course folder, found by
walking up from wherever the student happens to be, and none of them ever
overwrite work: a notebook that already exists is left alone and the fresh copy
lands beside it, and a project that already exists stops the command.
"""

from __future__ import annotations

import urllib.error

import click

from . import environment, notebooks, projects, release
from .course import CourseFolderNotFound, course_folder
from .doctor import diagnose

try:                                            # installed metadata, not a constant
    from importlib.metadata import version as _version
    __version__ = _version("im-course-tools")
except Exception:                               # pragma: no cover - source checkout
    __version__ = "0.0.0"


def _offline(error) -> int:
    click.echo(f"Could not reach the course website: {error}")
    click.echo("Check that you are online. Nothing has been changed.")
    return 1


def _folder():
    """The course folder, or a friendly exit explaining that there isn't one."""
    try:
        return course_folder()
    except CourseFolderNotFound as error:
        raise click.ClickException(str(error))


def _catalog(offering) -> tuple[list, Exception | None]:
    """One of the two lists, asked for and forgiven separately.

    A website published before the projects existed has no project list, and
    that should cost a student the projects rather than the whole command.
    """
    try:
        return offering(), None
    except urllib.error.URLError as error:
        return [], error


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="im")
def main() -> None:
    """Tools for the Instructing Machines course."""
    # Asked in the background and said at the end, so that no command waits on
    # it. A fix only reaches a student who knows there is one.
    release.announce_later(click.echo)


@main.command()
def check() -> None:
    """Check that the course environment is complete."""
    raise SystemExit(environment.check(click.echo))


@main.command()
@click.option("--report", is_flag=True,
              help="Also write a file to send to your instructor.")
@click.option("--offline", is_flag=True,
              help="Skip the checks that need the internet.")
@click.option("--upgrade/--no-upgrade", default=None,
              help="Upgrade `im` first, or leave it, without being asked.")
@click.option("-v", "--verbose", is_flag=True,
              help="Also show the things that were looked at and were fine.")
def doctor(report: bool, offline: bool, upgrade: bool | None, verbose: bool) -> None:
    """Work out why the course setup is not working.

    Unlike the other commands this one runs anywhere, because not being in the
    course folder is one of the things it is there to notice.

    The one thing it will change is `im` itself: a stale `im` is the one fault
    that would otherwise be diagnosed by the very code that has the bug. It
    asks first, and everything else it only reads.
    """
    if not offline and upgrade is not False:
        asking = (lambda question: click.confirm(question, default=True)) \
            if upgrade is None else None
        stop = release.upgrade_if_newer(click.echo, confirm=asking)
        if stop is not None:
            raise SystemExit(stop)
    raise SystemExit(diagnose(click.echo, offline=offline, report=report,
                              version=__version__, verbose=verbose))


@main.command()
@click.argument("name", required=False)
def get(name: str | None) -> None:
    """Download a chapter notebook, or a whole project, by NAME."""
    chapters, chapter_error = _catalog(notebooks.available)
    project_list, project_error = _catalog(projects.available)

    if not chapters and not project_list:
        error = chapter_error or project_error
        if error is not None:
            raise SystemExit(_offline(error))
        click.echo("The course website is not offering anything right now.")
        click.echo("Use the download buttons on the website instead.")
        raise SystemExit(1)

    if name is None:
        if chapters:
            click.echo("Ask for a chapter, like `im get iteration`:\n")
            for chapter in chapters:
                click.echo(f"    {chapter}")
        if project_list:
            click.echo("\nOr for a project, like `im get alignmentproject`:\n")
            for project in project_list:
                click.echo(f"    {project}")
        return

    chapter = notebooks.resolve(name, chapters)
    project = projects.resolve(name, project_list)

    # Nothing is called both today, but nothing stops a chapter and a project
    # from sharing a name later, and quietly picking one of them would be a bad
    # way to find out. The file extension settles it.
    if chapter and project:
        as_chapter = f"{chapter}.ipynb"
        as_project = f"{project}.zip"
        width = max(len(as_chapter), len(as_project))
        click.echo(f"There is both a chapter and a project called '{name}'.")
        click.echo("")
        click.echo(f"    im get {as_chapter:<{width}}   for the chapter")
        click.echo(f"    im get {as_project:<{width}}   for the project")
        raise SystemExit(1)

    folder = _folder()
    try:
        if chapter:
            raise SystemExit(notebooks.download(folder, chapter, click.echo))
        if project:
            raise SystemExit(projects.download(folder, project, click.echo))
    except urllib.error.URLError as error:
        click.echo(f"\nCould not download it: {error}")
        click.echo("Nothing has been changed.")
        raise SystemExit(1)

    click.echo(f"There is nothing called '{name}'.")
    near = notebooks.suggestions(name, chapters) + projects.suggestions(name, project_list)
    if near:
        click.echo("Did you mean: " + ", ".join(dict.fromkeys(near)) + "?")
    else:
        click.echo("Run `im get` on its own to see the whole list.")
    raise SystemExit(1)


@main.command()
@click.option("--no-upgrade", is_flag=True,
              help="Do not upgrade `im` itself first.")
def update(no_upgrade: bool) -> None:
    """Refresh the course environment from the website."""
    folder = _folder()

    # `im update` is the command for putting the environment right, and `im`
    # is part of the environment. Upgrading it first means the refresh that
    # follows is done by the current code rather than by the code being fixed.
    if not no_upgrade:
        stop = release.upgrade_if_newer(click.echo)
        if stop is not None:
            raise SystemExit(stop)
    try:
        raise SystemExit(environment.update(folder, click.echo))
    except urllib.error.URLError as error:
        raise SystemExit(_offline(error))


if __name__ == "__main__":                      # pragma: no cover
    main()
