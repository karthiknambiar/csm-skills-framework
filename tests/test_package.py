"""Smoke tests for the installable project scaffold."""

import csaf


def test_package_exposes_version() -> None:
    assert csaf.__version__ == "0.1.0.dev0"

