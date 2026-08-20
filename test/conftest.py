"""Fixtures shared by the whole suite.

Nothing here should ask the internet what the current version of
im-course-tools is. The answer changes without warning, which would make a
passing test start failing on a release day, and the question would put a
network round-trip in front of every test that invokes a command.
"""

import pytest


@pytest.fixture(autouse=True)
def no_update_check(monkeypatch):
    """Every test runs as though the version check is switched off.

    The tests that are about the check itself switch it back on for themselves,
    which keeps that decision visible in the test that depends on it.
    """
    monkeypatch.setenv("IM_NO_UPDATE_CHECK", "1")
