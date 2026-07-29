"""
Shared pytest fixtures for the offline test suite.

Run the whole suite with:  pytest   (from the project root)
No network access and no API keys are required - every test runs against
local fixtures or an in-memory database.
"""

import sqlite3

import pytest

from govspend_free import db, utils

# Don't sleep between fixture "fetches" during tests - the polite delay is
# only meaningful against real remote hosts.
utils.POLITE_DELAY_SECONDS = 0


@pytest.fixture
def tmp_conn():
    """A fresh in-memory database (schema loaded) for each test, so tests
    can't leak rows into one another. Replaces the previous single shared
    connection that forced tests to tolerate each other's leftover data."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()
