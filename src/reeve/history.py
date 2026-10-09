"""Undo, redo and roll-voiding: the rules, as pure reads of the log.

Nothing here writes. ``Store`` calls these planners inside an open database
transaction, then emits the marker events they describe.

Model (DESIGN.md §7):

* ``TransactionsRetracted`` marks transactions as undone; ``TransactionsRestored`` reverses one
  earlier retraction (redo). Whether a transaction is retracted is a *fold* over those markers
  in order; the ``status`` column on ``transactions`` is only a cache of that fold.
* Two transactions are *dependent* when a later one shares a subject with an earlier one, directly
  or through a chain. Undoing a transaction with active dependents is refused unless the caller
  asks for a cascade (DM only).
* A roll made in a retracted transaction can be reused by a later roll with the same key and dice.
  It is consumed once any roll has reused it, and unusable once the DM has voided it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .errors import HistoryRefused

RETRACTED_EVENT_TYPE = "TransactionsRetracted"
RESTORED_EVENT_TYPE = "TransactionsRestored"
ROLL_MADE_EVENT_TYPE = "RollMade"
ROLL_VOIDED_EVENT_TYPE = "RollVoided"

# Only the core may emit these; they carry the meaning of history itself.
RESERVED_EVENT_TYPES = frozenset({
    RETRACTED_EVENT_TYPE, RESTORED_EVENT_TYPE, ROLL_MADE_EVENT_TYPE, ROLL_VOIDED_EVENT_TYPE})

# A transaction containing any of these is bookkeeping: it can be redone or reversed, never undone.
_BOOKKEEPING_EVENT_TYPES = (RETRACTED_EVENT_TYPE, RESTORED_EVENT_TYPE, ROLL_VOIDED_EVENT_TYPE)
_BOOKKEEPING_PLACEHOLDERS = ", ".join("?" for _ in _BOOKKEEPING_EVENT_TYPES)


@dataclass(frozen=True)
class UndoPlan:
    target_sequence_numbers: tuple[int, ...]


@dataclass(frozen=True)
class RedoPlan:
    undo_sequence_number: int
    restored_sequence_numbers: tuple[int, ...]


def retracted_sequence_numbers(connection: sqlite3.Connection, campaign_id: str) -> set[int]:
    """Fold the retraction/restoration markers into the set of currently retracted transactions."""
    retracted: set[int] = set()
    rows = connection.execute(
        "SELECT type, payload_json FROM events WHERE campaign_id=? AND type IN (?, ?) ORDER BY sequence_number",
        (campaign_id, RETRACTED_EVENT_TYPE, RESTORED_EVENT_TYPE))
    for event_type, payload_json in rows:
        targets = json.loads(payload_json)["target_sequence_numbers"]
        if event_type == RETRACTED_EVENT_TYPE:
            retracted.update(targets)
        else:
            retracted.difference_update(targets)
    return retracted


def _transaction_row(connection: sqlite3.Connection, campaign_id: str, sequence_number: int):
    row = connection.execute(
        "SELECT id, actor_id, status FROM transactions WHERE campaign_id=? AND sequence_number=?",
        (campaign_id, sequence_number)).fetchone()
    if row is None:
        raise HistoryRefused(f"no transaction {sequence_number} in campaign {campaign_id!r}")
    return row


def _is_bookkeeping(connection: sqlite3.Connection, transaction_id: int) -> bool:
    return connection.execute(
        f"SELECT 1 FROM events WHERE transaction_id=? AND type IN ({_BOOKKEEPING_PLACEHOLDERS}) LIMIT 1",
        (transaction_id, *_BOOKKEEPING_EVENT_TYPES)).fetchone() is not None


def _subjects_of_active_transactions_from(
        connection: sqlite3.Connection, campaign_id: str, first_sequence_number: int) -> dict[int, set[str]]:
    """Subjects touched by each active, non-bookkeeping transaction at or after the given one."""
    subjects_by_sequence_number: dict[int, set[str]] = {}
    rows = connection.execute(
        "SELECT transactions.sequence_number, events.subjects_json FROM transactions"
        " LEFT JOIN events ON events.transaction_id = transactions.id"
        " WHERE transactions.campaign_id=? AND transactions.sequence_number>=? AND transactions.status='active'"
        f" AND NOT EXISTS (SELECT 1 FROM events marker WHERE marker.transaction_id = transactions.id"
        f"   AND marker.type IN ({_BOOKKEEPING_PLACEHOLDERS}))"
        " ORDER BY transactions.sequence_number, events.position_in_transaction",
        (campaign_id, first_sequence_number, *_BOOKKEEPING_EVENT_TYPES))
    for sequence_number, subjects_json in rows:
        subjects = subjects_by_sequence_number.setdefault(sequence_number, set())
        if subjects_json is not None:
            subjects.update(json.loads(subjects_json))
    return subjects_by_sequence_number


def dependent_sequence_numbers(
        connection: sqlite3.Connection, campaign_id: str, target_sequence_number: int) -> list[int]:
    """Later active transactions that depend on the target, found in one forward pass:
    dependency only ever flows from earlier to later."""
    subjects_by_sequence_number = _subjects_of_active_transactions_from(
        connection, campaign_id, target_sequence_number)
    touched_subjects = set(subjects_by_sequence_number.get(target_sequence_number, set()))
    dependents: list[int] = []
    for sequence_number in sorted(subjects_by_sequence_number):
        if sequence_number == target_sequence_number:
            continue
        subjects = subjects_by_sequence_number[sequence_number]
        if subjects & touched_subjects:
            dependents.append(sequence_number)
            touched_subjects |= subjects
    return dependents


def plan_undo(connection: sqlite3.Connection, campaign_id: str, target_sequence_number: int, *,
              actor_id: str, role: str, cascade: bool) -> UndoPlan:
    if role not in ("dm", "player"):
        raise HistoryRefused(f"role {role!r} may not undo")
    transaction_id, target_actor_id, status = _transaction_row(connection, campaign_id, target_sequence_number)
    if _is_bookkeeping(connection, transaction_id):
        raise HistoryRefused(f"transaction {target_sequence_number} is bookkeeping; use redo to reverse an undo")
    if status != "active":
        raise HistoryRefused(f"transaction {target_sequence_number} is already undone")
    if role == "player" and target_actor_id != actor_id:
        raise HistoryRefused(f"a player may only undo their own transactions; {target_sequence_number}"
                             f" belongs to {target_actor_id!r}")
    dependents = dependent_sequence_numbers(connection, campaign_id, target_sequence_number)
    if dependents and not cascade:
        raise HistoryRefused(
            f"transaction {target_sequence_number} has later dependents {dependents}; "
            f"undo them too (the DM may cascade)", tuple(dependents))
    if dependents and role != "dm":
        raise HistoryRefused("only the DM may cascade an undo across dependent transactions", tuple(dependents))
    return UndoPlan(tuple([target_sequence_number, *dependents]))


def latest_undoable_sequence_number(
        connection: sqlite3.Connection, campaign_id: str, *, actor_id: str, role: str) -> int:
    """The most recent active, non-bookkeeping transaction this actor may undo."""
    query = ("SELECT transactions.sequence_number FROM transactions WHERE transactions.campaign_id=?"
             " AND transactions.status='active'"
             f" AND NOT EXISTS (SELECT 1 FROM events marker WHERE marker.transaction_id = transactions.id"
             f"   AND marker.type IN ({_BOOKKEEPING_PLACEHOLDERS}))")
    parameters: list = [campaign_id, *_BOOKKEEPING_EVENT_TYPES]
    if role == "player":
        query += " AND transactions.actor_id=?"
        parameters.append(actor_id)
    query += " ORDER BY transactions.sequence_number DESC LIMIT 1"
    row = connection.execute(query, parameters).fetchone()
    if row is None:
        raise HistoryRefused("nothing to undo")
    return row[0]


def _roll_is_referenced_by_another_roll(
        connection: sqlite3.Connection, campaign_id: str, roll_sequence_number: int) -> bool:
    return connection.execute(
        "SELECT 1 FROM events WHERE campaign_id=? AND type=? AND json_extract(payload_json, '$.reused_from')=?",
        (campaign_id, ROLL_MADE_EVENT_TYPE, roll_sequence_number)).fetchone() is not None


def _roll_is_voided(connection: sqlite3.Connection, campaign_id: str, roll_sequence_number: int) -> bool:
    return connection.execute(
        "SELECT 1 FROM events WHERE campaign_id=? AND type=?"
        " AND json_extract(payload_json, '$.roll_sequence_number')=?",
        (campaign_id, ROLL_VOIDED_EVENT_TYPE, roll_sequence_number)).fetchone() is not None


def plan_redo(connection: sqlite3.Connection, campaign_id: str, undo_sequence_number: int, *,
              actor_id: str, role: str) -> RedoPlan:
    if role not in ("dm", "player"):
        raise HistoryRefused(f"role {role!r} may not redo")
    undo_transaction_id, undo_actor_id, _ = _transaction_row(connection, campaign_id, undo_sequence_number)
    retraction = connection.execute(
        "SELECT payload_json FROM events WHERE transaction_id=? AND type=?",
        (undo_transaction_id, RETRACTED_EVENT_TYPE)).fetchone()
    if retraction is None:
        raise HistoryRefused(f"transaction {undo_sequence_number} is not an undo")
    if role == "player" and undo_actor_id != actor_id:
        raise HistoryRefused("a player may only redo their own undo")
    targets = tuple(json.loads(retraction[0])["target_sequence_numbers"])
    already_reversed = connection.execute(
        "SELECT 1 FROM events WHERE campaign_id=? AND type=? AND json_extract(payload_json, '$.undo_sequence_number')=?",
        (campaign_id, RESTORED_EVENT_TYPE, undo_sequence_number)).fetchone()
    if already_reversed:
        raise HistoryRefused(f"undo {undo_sequence_number} has already been redone")
    currently_retracted = retracted_sequence_numbers(connection, campaign_id)
    if not set(targets) <= currently_retracted:
        raise HistoryRefused(f"undo {undo_sequence_number} no longer applies: some of its transactions are active")
    # Anything done since that touches the same subjects would be inconsistent with the restored past.
    subjects_by_sequence_number = _subjects_of_active_transactions_from(connection, campaign_id, targets[0])
    restored_subjects: set[str] = set()
    for target in targets:
        restored_subjects |= _subjects_of_retracted_transaction(connection, campaign_id, target)
    conflicts = [sequence_number for sequence_number, subjects in subjects_by_sequence_number.items()
                 if sequence_number not in targets and subjects & restored_subjects]
    if conflicts:
        raise HistoryRefused(f"cannot redo: later transactions {sorted(conflicts)} touch the same subjects",
                             tuple(sorted(conflicts)))
    for target in targets:
        for roll_sequence_number in _roll_sequence_numbers_of(connection, campaign_id, target):
            if _roll_is_referenced_by_another_roll(connection, campaign_id, roll_sequence_number):
                raise HistoryRefused(f"cannot redo: roll {roll_sequence_number} has been reused elsewhere")
            if _roll_is_voided(connection, campaign_id, roll_sequence_number):
                raise HistoryRefused(f"cannot redo: roll {roll_sequence_number} has been voided")
    return RedoPlan(undo_sequence_number, targets)


def _subjects_of_retracted_transaction(
        connection: sqlite3.Connection, campaign_id: str, sequence_number: int) -> set[str]:
    subjects: set[str] = set()
    rows = connection.execute(
        "SELECT events.subjects_json FROM events JOIN transactions ON transactions.id = events.transaction_id"
        " WHERE transactions.campaign_id=? AND transactions.sequence_number=?", (campaign_id, sequence_number))
    for (subjects_json,) in rows:
        subjects.update(json.loads(subjects_json))
    return subjects


def _roll_sequence_numbers_of(
        connection: sqlite3.Connection, campaign_id: str, transaction_sequence_number: int) -> list[int]:
    rows = connection.execute(
        "SELECT events.sequence_number FROM events JOIN transactions ON transactions.id = events.transaction_id"
        " WHERE transactions.campaign_id=? AND transactions.sequence_number=? AND events.type=?",
        (campaign_id, transaction_sequence_number, ROLL_MADE_EVENT_TYPE))
    return [row[0] for row in rows]


def plan_void_roll(connection: sqlite3.Connection, campaign_id: str, roll_sequence_number: int, *,
                   role: str) -> None:
    if role != "dm":
        raise HistoryRefused("only the DM may void a roll")
    row = connection.execute(
        "SELECT transactions.status FROM events JOIN transactions ON transactions.id = events.transaction_id"
        " WHERE events.campaign_id=? AND events.sequence_number=? AND events.type=?",
        (campaign_id, roll_sequence_number, ROLL_MADE_EVENT_TYPE)).fetchone()
    if row is None:
        raise HistoryRefused(f"event {roll_sequence_number} is not a roll")
    if row[0] != "retracted":
        raise HistoryRefused("a roll in an active transaction cannot be voided; undo the transaction first")
    if _roll_is_voided(connection, campaign_id, roll_sequence_number):
        raise HistoryRefused(f"roll {roll_sequence_number} is already voided")


def find_reusable_rolls(connection: sqlite3.Connection, campaign_id: str, key: str, notation: str,
                        excluded_sequence_numbers: frozenset[int] = frozenset()) -> list[tuple[int, dict]]:
    """Rolls with this key and dice, made in retracted transactions, neither consumed nor voided,
    oldest first, as (event sequence number, payload)."""
    rows = connection.execute(
        "SELECT roll.sequence_number, roll.payload_json FROM events roll"
        " JOIN transactions ON transactions.id = roll.transaction_id"
        " WHERE roll.campaign_id=? AND roll.type=? AND transactions.status='retracted'"
        "   AND json_extract(roll.payload_json, '$.key')=? AND json_extract(roll.payload_json, '$.dice')=?"
        "   AND NOT EXISTS (SELECT 1 FROM events later WHERE later.campaign_id = roll.campaign_id"
        "     AND later.type=? AND json_extract(later.payload_json, '$.reused_from') = roll.sequence_number)"
        "   AND NOT EXISTS (SELECT 1 FROM events voided WHERE voided.campaign_id = roll.campaign_id"
        "     AND voided.type=? AND json_extract(voided.payload_json, '$.roll_sequence_number') = roll.sequence_number)"
        " ORDER BY roll.sequence_number",
        (campaign_id, ROLL_MADE_EVENT_TYPE, key, notation, ROLL_MADE_EVENT_TYPE, ROLL_VOIDED_EVENT_TYPE))
    return [(sequence_number, json.loads(payload_json)) for sequence_number, payload_json in rows
            if sequence_number not in excluded_sequence_numbers]
