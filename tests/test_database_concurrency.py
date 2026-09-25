"""A dashboard read transaction must not reject a concurrent telemetry write."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.core.database import initialize_database


class DatabaseConcurrencyTests(unittest.TestCase):
    def test_existing_database_reader_does_not_block_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "conversations.db")
            with sqlite3.connect(path) as setup:
                setup.execute("CREATE TABLE samples(value INTEGER)")
                setup.execute("INSERT INTO samples VALUES (1)")
                self.assertEqual(setup.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            initialize_database(path)
            with sqlite3.connect(path) as writer, sqlite3.connect(f"file:{path}?mode=ro", uri=True) as reader:
                reader.execute("BEGIN")
                self.assertEqual(reader.execute("SELECT * FROM samples").fetchall(), [(1,)])
                writer.execute("PRAGMA busy_timeout=50")
                writer.execute("INSERT INTO samples VALUES (2)")
                writer.commit()
                self.assertEqual(reader.execute("SELECT * FROM samples").fetchall(), [(1,)])
                reader.commit()
                self.assertEqual(reader.execute("SELECT * FROM samples").fetchall(), [(1,), (2,)])
            initialize_database(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_unconfigured_and_memory_databases_are_unchanged(self):
        initialize_database("")
        initialize_database(":memory:")
