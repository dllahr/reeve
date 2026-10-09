import sqlite3

import pytest

from reeve import Store, ReeveError, ChainError
from reeve.export import export_jsonl
from conftest import Ledger, make_ticking_clock, pay


def balances(store):
    rows = store.connection.execute("SELECT holder, coin FROM p_balance")
    return {holder: coin for holder, coin in rows}


def test_transaction_appends_events_atomically_and_updates_projection(store):
    pay(store, "c1", "bank", "dave", 100)
    pay(store, "c1", "dave", "wend", 30)
    assert balances(store) == {"bank": -100, "dave": 70, "wend": 30}
    events = store.events("c1")
    assert [event.sequence_number for event in events] == [1, 2]
    assert [event.transaction_sequence_number for event in events] == [1, 2]


def test_failed_transaction_leaves_nothing_behind(store):
    with pytest.raises(RuntimeError):
        with store.transaction("c1", actor_id="dm1", role="dm", command="pay") as transaction:
            transaction.emit("CoinTransferred", {"from": "a", "to": "b", "amount": 5})
            raise RuntimeError("boom")
    assert store.events("c1") == []
    assert store.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert balances(store) == {}


def test_projection_error_rolls_back_the_whole_transaction(store):
    with pytest.raises(KeyError):
        with store.transaction("c1", actor_id="dm1", role="dm", command="pay") as transaction:
            transaction.emit("CoinTransferred", {"from": "a", "to": "b", "amount": 5})
            transaction.emit("CoinTransferred", {"from": "a"})  # malformed: projection raises
    assert store.events("c1") == []
    assert balances(store) == {}


def test_transactions_do_not_nest(store):
    with store.transaction("c1", actor_id="dm1", role="dm", command="x"):
        with pytest.raises(ReeveError):
            with store.transaction("c1", actor_id="dm1", role="dm", command="y"):
                pass


def test_events_cannot_be_updated_or_deleted(store):
    pay(store, "c1", "a", "b", 1)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.connection.execute("UPDATE events SET payload_json='{}'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.connection.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.connection.execute("DELETE FROM transactions")


def test_only_transaction_status_may_change(store):
    pay(store, "c1", "a", "b", 1)
    with pytest.raises(sqlite3.DatabaseError, match="status"):
        store.connection.execute("UPDATE transactions SET command='other'")
    store.connection.execute("UPDATE transactions SET status='retracted'")  # allowed; undo will use this path


def test_validation_refuses_bad_input(store):
    def emit(**event_fields):
        with store.transaction("c1", actor_id="dm1", role="dm", command="x") as transaction:
            transaction.emit(**event_fields)
    with pytest.raises(ReeveError, match="floats"):
        emit(event_type="Thing", payload={"x": 1.5})
    with pytest.raises(ReeveError, match="CamelCase"):
        emit(event_type="bad_name", payload={})
    with pytest.raises(ReeveError, match="audience"):
        emit(event_type="Thing", payload={}, audience="everyone")
    with pytest.raises(ReeveError, match="unsupported"):
        emit(event_type="Thing", payload={"x": object()})
    with pytest.raises(ReeveError, match="role"):
        store.transaction("c1", actor_id="a", role="wizard", command="x")
    with pytest.raises(ReeveError, match="no such campaign"):
        store.transaction("nope", actor_id="a", role="dm", command="x")


def test_chain_verifies_and_detects_tampering(store):
    for index in range(5):
        pay(store, "c1", "a", "b", index + 1)
    assert store.verify_chain("c1") == 5
    # Bypass the trigger the way a hostile editor of the file would.
    store.connection.execute("DROP TRIGGER events_no_update")
    store.connection.execute("UPDATE events SET payload_json=? WHERE sequence_number=3",
                       ('{"amount":999,"from":"a","to":"b"}',))
    with pytest.raises(ChainError, match="event 3 has been altered"):
        store.verify_chain("c1")


def test_chain_detects_deleted_event(store):
    for index in range(3):
        pay(store, "c1", "a", "b", 1)
    store.connection.execute("DROP TRIGGER events_no_delete")
    store.connection.execute("DELETE FROM events WHERE sequence_number=2")
    with pytest.raises(ChainError):
        store.verify_chain("c1")


def test_chains_are_per_campaign(store):
    store.create_campaign("c2", "toy")
    pay(store, "c1", "a", "b", 1)
    pay(store, "c2", "a", "b", 1)
    assert store.events("c1")[0].prev_hash != store.events("c2")[0].prev_hash
    assert store.verify_chain("c1") == store.verify_chain("c2") == 1


def test_rebuild_reproduces_live_projection(store):
    for index in range(10):
        pay(store, "c1", "bank", f"p{index % 3}", index + 1)
    live = balances(store)
    assert store.verify_projections() is True
    assert balances(store) == live            # verification must not disturb live state
    store.connection.execute("UPDATE p_balance SET coin = coin + 1")  # simulate drift
    assert store.verify_projections() is False
    store.rebuild()
    assert balances(store) == live
    assert store.verify_projections() is True


def test_rebuild_skips_retracted_transactions(store):
    pay(store, "c1", "bank", "dave", 100)
    pay(store, "c1", "bank", "dave", 50)
    store.connection.execute("UPDATE transactions SET status='retracted' WHERE sequence_number=2")
    store.rebuild()
    assert balances(store) == {"bank": -100, "dave": 100}
    assert [event.sequence_number for event in store.events("c1")] == [1]
    assert [event.sequence_number for event in store.events("c1", include_retracted=True)] == [1, 2]
    assert store.verify_chain("c1") == 2       # retracted events remain in the chain


def test_export_is_deterministic(ticking_clock):
    def build_store():
        store = Store(":memory:", projections=[Ledger()], clock=make_ticking_clock())
        store.create_campaign("c1", "toy")
        pay(store, "c1", "bank", "dave", 100)
        pay(store, "c1", "dave", "wend", 30)
        return store
    first_export = export_jsonl(build_store(), "c1")
    second_export = export_jsonl(build_store(), "c1")
    assert first_export == second_export
    assert first_export.count("\n") == 1 + 2 + 2          # campaign + 2 transactions + 2 events
    assert first_export.endswith("\n")


def test_zero_event_transaction_is_recorded(store):
    with store.transaction("c1", actor_id="p1", role="player", command="move", arguments={"to": "wall"}):
        pass  # a refused command still leaves a trace
    assert store.connection.execute("SELECT command FROM transactions").fetchone() == ("move",)
    assert store.events("c1") == []


def test_file_backed_store_persists(tmp_path, ticking_clock):
    path = str(tmp_path / "r.sqlite")
    first_session = Store(path, projections=[Ledger()], clock=ticking_clock)
    first_session.create_campaign("c1", "toy")
    pay(first_session, "c1", "a", "b", 7)
    first_session.close()
    second_session = Store(path, projections=[Ledger()], clock=ticking_clock)
    assert second_session.verify_chain("c1") == 1
    assert second_session.verify_projections() is True
