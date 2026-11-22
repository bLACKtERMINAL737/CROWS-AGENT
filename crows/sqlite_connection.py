"""SQLite connection lifecycle helpers."""
import sqlite3


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit or roll back on context exit, then always release the connection."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()
