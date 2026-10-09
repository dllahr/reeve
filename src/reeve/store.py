"""The event store: campaigns, transactions, hash-chained events, projections.

Design rules enforced here (see DESIGN.md §4):

* Events are facts that happened, appended in Transactions, atomically.
* Events cannot be updated or deleted (SQLite triggers refuse, not just code).
* Each campaign's events form a SHA-256 hash chain; ``verify_chain`` detects edits.
* Payloads are canonical JSON and may not contain floats, so the same log
  always exports to the same bytes on every platform.
* Projections are applied in the same transaction as the events, and are
  rebuildable from the log.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Sequence

from .projection import Projection

ROLES = ("dm", "player", "observer", "system")
_AUDIENCE_RE = re.compile(r"^(public|party|dm|actor:[A-Za-z0-9_.\-]+)$")
_TYPE_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-:]+$")


class ReeveError(Exception):
    """Base class for refusals raised by the core."""


class ChainError(ReeveError):
    """The hash chain does not verify: the log has been altered."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id          TEXT PRIMARY KEY,
    ruleset     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    sequence_number         INTEGER NOT NULL,
    actor_id    TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('dm','player','observer','system')),
    command     TEXT NOT NULL,
    arguments_json   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retracted')),
    created_at  TEXT NOT NULL,
    UNIQUE (campaign_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   TEXT NOT NULL REFERENCES campaigns(id),
    sequence_number           INTEGER NOT NULL,
    transaction_id       INTEGER NOT NULL REFERENCES transactions(id),
    position_in_transaction   INTEGER NOT NULL,
    type          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    audience      TEXT NOT NULL,
    subjects_json TEXT NOT NULL,
    world_time    INTEGER,
    prev_hash     TEXT NOT NULL,
    hash          TEXT NOT NULL,
    UNIQUE (campaign_id, sequence_number)
);

CREATE INDEX IF NOT EXISTS events_transaction ON events(transaction_id, position_in_transaction);

-- The past is not editable. These are the machine that refuses.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS transactions_no_delete BEFORE DELETE ON transactions
BEGIN SELECT RAISE(ABORT, 'transactions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS transactions_status_only BEFORE UPDATE ON transactions
WHEN NEW.id IS NOT OLD.id OR NEW.campaign_id IS NOT OLD.campaign_id OR NEW.sequence_number IS NOT OLD.sequence_number
  OR NEW.actor_id IS NOT OLD.actor_id OR NEW.role IS NOT OLD.role OR NEW.command IS NOT OLD.command
  OR NEW.arguments_json IS NOT OLD.arguments_json OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'only a transaction''s status may change'); END;
"""


@dataclass(frozen=True)
class StoredEvent:
    campaign_id: str
    sequence_number: int
    transaction_sequence_number: int
    actor_id: str
    role: str
    type: str
    payload: dict
    audience: str
    subjects: tuple[str, ...]
    world_time: int | None
    prev_hash: str
    hash: str


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, no floats."""
    _reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _reject_floats(value: Any, path: str = "payload") -> None:
    if isinstance(value, float):
        raise ReeveError(f"floats are not allowed in events ({path}={value!r}); use integers or strings")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReeveError(f"event keys must be strings ({path}: {key!r})")
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise ReeveError(f"unsupported value in event ({path}: {type(value).__name__})")


def _genesis(campaign_id: str) -> str:
    return hashlib.sha256(f"reeve-genesis:{campaign_id}".encode()).hexdigest()


def _event_hash(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + canonical_json(body)).encode()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")



class Transaction:
    """A transaction: the atomic group of events produced by one command. Use ``Store.transaction``.

    Not to be confused with a 1e "turn" (10 minutes of game time). ``world_time`` on an event is an
    integer count of seconds; the minimum tick is 1 second.
    """

    def __init__(self, store: "Store", campaign_id: str, actor_id: str, role: str,
                 command: str, arguments: dict):
        self._store = store
        self.campaign_id = campaign_id
        self.actor_id = actor_id
        self.role = role
        self.command = command
        self.arguments = arguments
        self.sequence_number: int | None = None
        self._pending_events: list[dict] = []

    def emit(self, event_type: str, payload: dict | None = None, *, audience: str = "public",
             subjects: Sequence[str] = (), world_time: int | None = None) -> None:
        if not _TYPE_RE.match(event_type):
            raise ReeveError(f"event type must be CamelCase, got {event_type!r}")
        if not _AUDIENCE_RE.match(audience):
            raise ReeveError(f"bad audience {audience!r}: use public, party, dm or actor:<id>")
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise ReeveError("event payload must be a dict")
        for subject_id in subjects:
            if not _ID_RE.match(subject_id):
                raise ReeveError(f"bad subject id {subject_id!r}")
        if world_time is not None and (not isinstance(world_time, int) or isinstance(world_time, bool)):
            raise ReeveError("world_time must be an int or None")
        _reject_floats(payload)
        self._pending_events.append(dict(type=event_type, payload=payload, audience=audience,
                                         subjects=sorted(set(subjects)), world_time=world_time))

    def __enter__(self) -> "Transaction":
        self._store._begin()
        try:
            self._store._open_transaction(self)
        except BaseException:
            self._store._rollback()
            raise
        return self

    def __exit__(self, exception_type, exception, traceback) -> bool:
        if exception_type is not None:
            self._store._rollback()
            return False
        try:
            self._store._close_transaction(self)
        except BaseException:
            self._store._rollback()
            raise
        self._store._commit()
        return False


class Store:
    """SQLite-backed event store. Single writer."""

    def __init__(self, path: str = ":memory:", *, projections: Iterable[Projection] = (),
                 clock: Callable[[], str] = _utc_now):
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._connection.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(SCHEMA)
        self._clock = clock
        self._projections = list(projections)
        projection_names = [projection.name for projection in self._projections]
        if len(set(projection_names)) != len(projection_names) or not all(projection_names):
            raise ReeveError("projections need unique, non-empty names")
        for projection in self._projections:
            for table_name in projection.tables:
                if not table_name.startswith("p_"):
                    raise ReeveError(f"projection table {table_name!r} must start with 'p_'")
            projection.create(self._connection)
        self._transaction_is_open = False

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    # -- database transaction control ---------------------------------------
    def _begin(self) -> None:
        if self._transaction_is_open:
            raise ReeveError("a transaction is already open; transactions do not nest")
        self._connection.execute("BEGIN IMMEDIATE")
        self._transaction_is_open = True

    def _commit(self) -> None:
        self._connection.execute("COMMIT")
        self._transaction_is_open = False

    def _rollback(self) -> None:
        if self._transaction_is_open:
            self._connection.execute("ROLLBACK")
            self._transaction_is_open = False

    # -- campaigns ----------------------------------------------------------
    def create_campaign(self, campaign_id: str, ruleset: str) -> None:
        if not _ID_RE.match(campaign_id):
            raise ReeveError(f"bad campaign id {campaign_id!r}")
        try:
            self._connection.execute("INSERT INTO campaigns(id, ruleset, created_at) VALUES (?,?,?)",
                                     (campaign_id, ruleset, self._clock()))
        except sqlite3.IntegrityError:
            raise ReeveError(f"campaign {campaign_id!r} already exists") from None

    def _require_campaign(self, campaign_id: str) -> None:
        found = self._connection.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if found is None:
            raise ReeveError(f"no such campaign {campaign_id!r}")

    # -- transactions -------------------------------------------------------
    def transaction(self, campaign_id: str, *, actor_id: str, role: str, command: str,
                    arguments: dict | None = None) -> Transaction:
        if role not in ROLES:
            raise ReeveError(f"unknown role {role!r}")
        if not _ID_RE.match(actor_id):
            raise ReeveError(f"bad actor id {actor_id!r}")
        arguments = {} if arguments is None else arguments
        _reject_floats(arguments, "arguments")
        self._require_campaign(campaign_id)
        return Transaction(self, campaign_id, actor_id, role, command, arguments)

    def _open_transaction(self, transaction: Transaction) -> None:
        next_sequence_number = self._connection.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM transactions WHERE campaign_id=?",
            (transaction.campaign_id,)).fetchone()[0]
        transaction.sequence_number = next_sequence_number
        self._connection.execute(
            "INSERT INTO transactions(campaign_id, sequence_number, actor_id, role, command, arguments_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (transaction.campaign_id, next_sequence_number, transaction.actor_id, transaction.role,
             transaction.command, canonical_json(transaction.arguments), self._clock()))

    def _close_transaction(self, transaction: Transaction) -> None:
        transaction_id = self._connection.execute(
            "SELECT id FROM transactions WHERE campaign_id=? AND sequence_number=?",
            (transaction.campaign_id, transaction.sequence_number)).fetchone()[0]
        last_event = self._connection.execute(
            "SELECT sequence_number, hash FROM events WHERE campaign_id=? ORDER BY sequence_number DESC LIMIT 1",
            (transaction.campaign_id,)).fetchone()
        if last_event:
            event_sequence_number, previous_hash = last_event
        else:
            event_sequence_number, previous_hash = 0, _genesis(transaction.campaign_id)
        stored_events: list[StoredEvent] = []
        for position_in_transaction, pending in enumerate(transaction._pending_events, start=1):
            event_sequence_number += 1
            hashed_body = dict(campaign=transaction.campaign_id, sequence_number=event_sequence_number,
                               transaction=transaction.sequence_number, actor=transaction.actor_id,
                               **pending)
            event_hash = _event_hash(previous_hash, hashed_body)
            self._connection.execute(
                "INSERT INTO events(campaign_id, sequence_number, transaction_id, position_in_transaction, type, payload_json,"
                " audience, subjects_json, world_time, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (transaction.campaign_id, event_sequence_number, transaction_id, position_in_transaction,
                 pending["type"], canonical_json(pending["payload"]), pending["audience"],
                 canonical_json(pending["subjects"]), pending["world_time"], previous_hash, event_hash))
            stored_events.append(StoredEvent(
                campaign_id=transaction.campaign_id, sequence_number=event_sequence_number,
                transaction_sequence_number=transaction.sequence_number, actor_id=transaction.actor_id,
                role=transaction.role, type=pending["type"], payload=pending["payload"],
                audience=pending["audience"], subjects=tuple(pending["subjects"]),
                world_time=pending["world_time"], prev_hash=previous_hash, hash=event_hash))
            previous_hash = event_hash
        for stored_event in stored_events:
            for projection in self._projections:
                projection.apply(self._connection, stored_event)

    # -- reads --------------------------------------------------------------
    def events(self, campaign_id: str, *, include_retracted: bool = False) -> list[StoredEvent]:
        self._require_campaign(campaign_id)
        return list(self._iter_events(campaign_id, include_retracted))

    def _iter_events(self, campaign_id: str | None, include_retracted: bool) -> Iterator[StoredEvent]:
        query = ("SELECT events.campaign_id, events.sequence_number, transactions.sequence_number, transactions.actor_id,"
                 " transactions.role, events.type, events.payload_json, events.audience,"
                 " events.subjects_json, events.world_time, events.prev_hash, events.hash"
                 " FROM events JOIN transactions ON transactions.id = events.transaction_id")
        conditions: list[str] = []
        parameters: list[str] = []
        if campaign_id is not None:
            conditions.append("events.campaign_id=?")
            parameters.append(campaign_id)
        if not include_retracted:
            conditions.append("transactions.status='active'")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY events.id"
        for row in self._connection.execute(query, parameters):
            (row_campaign_id, event_seq, transaction_sequence_number, actor_id, role, event_type, payload_json,
             audience, subjects_json, world_time, previous_hash, event_hash) = row
            yield StoredEvent(
                campaign_id=row_campaign_id, sequence_number=event_seq, transaction_sequence_number=transaction_sequence_number,
                actor_id=actor_id, role=role, type=event_type, payload=json.loads(payload_json),
                audience=audience, subjects=tuple(json.loads(subjects_json)), world_time=world_time,
                prev_hash=previous_hash, hash=event_hash)

    # -- integrity ----------------------------------------------------------
    def verify_chain(self, campaign_id: str) -> int:
        """Recompute every hash. Returns the number of events; raises ChainError on any mismatch."""
        self._require_campaign(campaign_id)
        expected_previous_hash = _genesis(campaign_id)
        events_checked = 0
        for event in self._iter_events(campaign_id, include_retracted=True):
            events_checked += 1
            if event.sequence_number != events_checked:
                raise ChainError(f"{campaign_id}: gap or reorder at event {events_checked}"
                                 f" (found {event.sequence_number})")
            if event.prev_hash != expected_previous_hash:
                raise ChainError(f"{campaign_id}: event {event.sequence_number} does not follow its predecessor")
            hashed_body = dict(campaign=campaign_id, sequence_number=event.sequence_number, transaction=event.transaction_sequence_number,
                               actor=event.actor_id, type=event.type, payload=event.payload,
                               audience=event.audience, subjects=list(event.subjects),
                               world_time=event.world_time)
            if _event_hash(expected_previous_hash, hashed_body) != event.hash:
                raise ChainError(f"{campaign_id}: event {event.sequence_number} has been altered")
            expected_previous_hash = event.hash
        return events_checked

    # -- projections --------------------------------------------------------
    def rebuild(self) -> None:
        """Clear every projection and replay all active events."""
        self._begin()
        try:
            self._replay()
        except BaseException:
            self._rollback()
            raise
        self._commit()

    def _replay(self) -> None:
        for projection in self._projections:
            projection.clear(self._connection)
        for event in self._iter_events(None, include_retracted=False):
            for projection in self._projections:
                projection.apply(self._connection, event)

    def verify_projections(self) -> bool:
        """True iff a from-scratch replay reproduces the live projection state exactly.
        Leaves the live state untouched (the replay is rolled back)."""
        state_before = {projection.name: projection.dump(self._connection) for projection in self._projections}
        self._begin()
        try:
            self._replay()
            state_after_replay = {projection.name: projection.dump(self._connection)
                                  for projection in self._projections}
        finally:
            self._rollback()
        return state_before == state_after_replay
