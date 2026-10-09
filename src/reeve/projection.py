"""Projections: persisted state folded from events.

A projection owns some tables and knows how to apply one event to them.
The store applies projections inside the same transaction that appends the
events, so a projection error rolls the whole transaction back. Everything a
projection holds must be rebuildable from the log: ``Store.rebuild`` clears
the tables and replays every active event, and a test asserts the result is
identical to the incrementally-maintained state.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import StoredEvent


class Projection:
    """Base class. Subclasses set ``name`` and ``tables`` and implement ``apply``.

    ``tables`` lists the table names the projection owns; they are cleared on
    rebuild and compared by the rebuild-equality check. Table names must be
    prefixed with ``p_`` so they can never collide with core tables.
    """

    name: str = ""
    tables: tuple[str, ...] = ()

    def create(self, connection: sqlite3.Connection) -> None:
        """Create the projection's tables (idempotent)."""
        raise NotImplementedError

    def apply(self, connection: sqlite3.Connection, event: "StoredEvent") -> None:
        """Fold one event into the tables. Must be deterministic and must not
        re-run any game rules: events record results, not requests."""
        raise NotImplementedError

    def clear(self, connection: sqlite3.Connection) -> None:
        for table in self.tables:
            connection.execute(f"DELETE FROM {table}")

    def dump(self, connection: sqlite3.Connection) -> dict[str, list[tuple]]:
        """Full, ordered contents of every owned table (for equality checks)."""
        contents_by_table: dict[str, list[tuple]] = {}
        for table in self.tables:
            column_names = [column[1] for column in connection.execute(f"PRAGMA table_info({table})")]
            ordering = ", ".join(column_names)
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY {ordering}")
            contents_by_table[table] = [tuple(row) for row in rows]
        return contents_by_table
