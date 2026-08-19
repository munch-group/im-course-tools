"""im-tools: the `im` command for the Instructing Machines course.

The course folder students download holds their own work and nothing else. The
commands that fetch chapters and projects, check the environment and refresh it
live here instead, so a fix reaches a hundred students through a release rather
than through asking them to download a file again.
"""

from .cli import main
from .course import base_url, course_folder, fetch, fetch_bytes, url_for

__all__ = ["main", "base_url", "course_folder", "fetch", "fetch_bytes", "url_for"]
