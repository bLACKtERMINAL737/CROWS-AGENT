import sqlite3

import pytest

from crows.sqlite_connection import ClosingSQLiteConnection


def test_context_manager_commits_and_closes_connection(tmp_path):
    db_path = tmp_path / "connection.sqlite3"
    connection = sqlite3.connect(db_path, factory=ClosingSQLiteConnection)
    with connection as con:
        con.execute("CREATE TABLE values_table (value TEXT)")
        con.execute("INSERT INTO values_table VALUES ('committed')")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")

    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT value FROM values_table").fetchone()[0] == "committed"


def test_context_manager_rolls_back_and_closes_connection(tmp_path):
    db_path = tmp_path / "rollback.sqlite3"
    with sqlite3.connect(db_path) as setup:
        setup.execute("CREATE TABLE values_table (value TEXT)")

    connection = sqlite3.connect(db_path, factory=ClosingSQLiteConnection)
    with pytest.raises(RuntimeError, match="abort transaction"):
        with connection as con:
            con.execute("INSERT INTO values_table VALUES ('not committed')")
            raise RuntimeError("abort transaction")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")

    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 0
