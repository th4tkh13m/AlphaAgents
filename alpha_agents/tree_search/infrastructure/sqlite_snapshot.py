"""Observe SQLite through stable DB/WAL filesystem copies only."""

import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def signature(path):
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


@contextmanager
def snapshot_connection(source):
    source = Path(source)
    for _ in range(8):
        with tempfile.TemporaryDirectory(prefix="alpha-snapshot-") as temporary:
            paths = [source, Path(str(source) + "-wal"), Path(str(source) + "-journal")]
            before = [signature(path) for path in paths]
            if before[0] is None:
                raise FileNotFoundError(source)
            try:
                for path in paths:
                    if path.exists():
                        shutil.copy2(path, Path(temporary) / path.name)
                stable = before == [signature(path) for path in paths]
            except (FileNotFoundError, sqlite3.DatabaseError):
                stable = False
            if stable:
                connection = sqlite3.connect(str(Path(temporary) / source.name))
                connection.row_factory = sqlite3.Row
                try:
                    valid = connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    connection.execute("PRAGMA query_only=ON")
                except sqlite3.DatabaseError:
                    connection.close()
                    valid = False
                if valid:
                    try:
                        yield connection
                        return
                    finally:
                        connection.close()
                else:
                    connection.close()
        time.sleep(.1)
    raise RuntimeError("Could not capture a stable SQLite filesystem snapshot")
