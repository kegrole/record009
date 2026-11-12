"""SQLite storage with primary-key upserts and physical delete propagation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .watermark import is_newer


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
"""


class ItemStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute(SCHEMA_SQL)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ItemStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def get(self, record_id: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            "SELECT id, name, updated_at, deleted FROM items WHERE id = ?",
            (record_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "updated_at": row[2], "deleted": bool(row[3])}

    def count(self) -> int:
        cursor = self.connection.execute("SELECT COUNT(*) FROM items")
        return int(cursor.fetchone()[0])

    def upsert(self, record: dict[str, Any]) -> tuple[bool, bool]:
        """Upsert one record and return ``(written, conflict)``.

        A conflict means an existing row had an older ``updated_at`` and was
        overwritten by the remote version. Equal versions are idempotent
        duplicate delivery, not conflicts.
        """
        existing = self.get(record["id"])
        conflict = False
        if existing is not None:
            if not is_newer(record["updated_at"], existing["updated_at"]):
                return False, False
            conflict = True

        self.connection.execute(
            """
            INSERT INTO items (id, name, updated_at, deleted)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                updated_at = excluded.updated_at,
                deleted = excluded.deleted
            """,
            (record["id"], record["name"], record["updated_at"]),
        )
        return True, conflict

    def delete(self, record_id: str) -> bool:
        cursor = self.connection.execute("DELETE FROM items WHERE id = ?", (record_id,))
        return cursor.rowcount > 0

    def commit(self) -> None:
        self.connection.commit()
