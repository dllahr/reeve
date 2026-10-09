"""Deterministic export of a campaign's log.

Same log => same bytes. The output is meant to be committed to git for history,
backup and diffing; it is output only and is never read back by hand-editing.
"""

from __future__ import annotations

import json

from .store import Store, canonical_json


def export_jsonl(store: Store, campaign_id: str) -> str:
    """One JSON object per line: the campaign, then each transaction followed by its events.
    Retracted transactions are included (with their status) because the past is evidence."""
    connection = store.connection
    campaign_row = connection.execute(
        "SELECT id, ruleset, created_at FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign_row is None:
        raise ValueError(f"no such campaign {campaign_id!r}")
    campaign_id, ruleset, campaign_created_at = campaign_row
    lines = [canonical_json(dict(kind="campaign", id=campaign_id, ruleset=ruleset,
                                 created_at=campaign_created_at))]
    transaction_rows = connection.execute(
        "SELECT id, sequence_number, actor_id, role, command, arguments_json, status, created_at"
        " FROM transactions WHERE campaign_id=? ORDER BY sequence_number", (campaign_id,)).fetchall()
    for (transaction_id, transaction_sequence_number, actor_id, role, command, arguments_json,
         status, transaction_created_at) in transaction_rows:
        lines.append(canonical_json(dict(
            kind="transaction", sequence_number=transaction_sequence_number, actor_id=actor_id, role=role,
            command=command, arguments=json.loads(arguments_json), status=status,
            created_at=transaction_created_at)))
        event_rows = connection.execute(
            "SELECT sequence_number, type, payload_json, audience, subjects_json, world_time, prev_hash, hash"
            " FROM events WHERE transaction_id=? ORDER BY position_in_transaction", (transaction_id,))
        for (event_sequence_number, event_type, payload_json, audience, subjects_json, world_time,
             previous_hash, event_hash) in event_rows:
            lines.append(canonical_json(dict(
                kind="event", sequence_number=event_sequence_number, type=event_type,
                payload=json.loads(payload_json), audience=audience, subjects=json.loads(subjects_json),
                world_time=world_time, prev_hash=previous_hash, hash=event_hash)))
    return "\n".join(lines) + "\n"
