"""SQLite engine pragmas are no longer applicable — persistence is now file-based."""
from __future__ import annotations

import pytest


def test_sqlite_pragmas_applied() -> None:
    pytest.skip("SQLite engine removed — persistence is now file-based JSON")
