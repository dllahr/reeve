import pytest

from reeve import HistoryRefused
from conftest import balances, pay


def sequence_numbers(store, campaign_id="c1", *, include_retracted=False):
    return [event.sequence_number for event in store.events(campaign_id, include_retracted=include_retracted)]


def test_undo_removes_the_effect_and_keeps_the_record(store):
    pay(store, "c1", "bank", "dave", 100)
    pay(store, "c1", "bank", "wend", 40)
    store.undo("c1", 2, actor_id="dm1", role="dm")
    assert balances(store) == {"bank": -100, "dave": 100}
    status = store.connection.execute("SELECT status FROM transactions WHERE sequence_number=2").fetchone()[0]
    assert status == "retracted"
    assert store.verify_chain("c1") == 3                       # two payments + the undo marker
    assert store.verify_statuses("c1") and store.verify_projections()


def test_redo_restores_exactly_what_the_undo_removed(store):
    pay(store, "c1", "bank", "dave", 100)
    undo_number = store.undo("c1", 1, actor_id="dm1", role="dm")
    assert balances(store) == {}
    store.redo("c1", undo_number, actor_id="dm1", role="dm")
    assert balances(store) == {"bank": -100, "dave": 100}
    assert store.verify_statuses("c1") and store.verify_projections()


def test_redo_can_be_undone_again_and_repeated(store):
    pay(store, "c1", "bank", "dave", 100)
    first_undo = store.undo("c1", 1, actor_id="dm1", role="dm")
    store.redo("c1", first_undo, actor_id="dm1", role="dm")
    second_undo = store.undo("c1", 1, actor_id="dm1", role="dm")
    assert balances(store) == {}
    store.redo("c1", second_undo, actor_id="dm1", role="dm")
    assert balances(store) == {"bank": -100, "dave": 100}


def test_double_undo_and_double_redo_are_refused(store):
    pay(store, "c1", "bank", "dave", 100)
    undo_number = store.undo("c1", 1, actor_id="dm1", role="dm")
    with pytest.raises(HistoryRefused, match="already undone"):
        store.undo("c1", 1, actor_id="dm1", role="dm")
    store.redo("c1", undo_number, actor_id="dm1", role="dm")
    with pytest.raises(HistoryRefused, match="already been redone"):
        store.redo("c1", undo_number, actor_id="dm1", role="dm")


def test_bookkeeping_transactions_cannot_be_undone(store):
    pay(store, "c1", "bank", "dave", 100)
    undo_number = store.undo("c1", 1, actor_id="dm1", role="dm")
    with pytest.raises(HistoryRefused, match="bookkeeping"):
        store.undo("c1", undo_number, actor_id="dm1", role="dm")


def test_undo_with_dependents_is_refused_until_cascaded(store):
    pay(store, "c1", "bank", "dave", 100)          # 1
    pay(store, "c1", "dave", "wend", 30)           # 2 depends on 1 (shares 'dave')
    pay(store, "c1", "bank", "ista", 5)            # 3 shares 'bank' with 1 as well
    pay(store, "c1", "x", "y", 1)                  # 4 unrelated
    with pytest.raises(HistoryRefused) as refusal:
        store.undo("c1", 1, actor_id="dm1", role="dm")
    assert refusal.value.dependent_sequence_numbers == (2, 3)
    store.undo("c1", 1, actor_id="dm1", role="dm", cascade=True)
    assert balances(store) == {"x": -1, "y": 1}
    assert store.verify_statuses("c1") and store.verify_projections()


def test_unrelated_later_transactions_do_not_block_undo(store):
    pay(store, "c1", "bank", "dave", 100)
    pay(store, "c1", "x", "y", 1)
    store.undo("c1", 1, actor_id="dm1", role="dm")
    assert balances(store) == {"x": -1, "y": 1}


def test_redo_is_refused_if_later_work_touches_the_same_subjects(store):
    pay(store, "c1", "bank", "dave", 100)
    undo_number = store.undo("c1", 1, actor_id="dm1", role="dm")
    pay(store, "c1", "dave", "wend", 10)           # touches 'dave' while the past is undone
    with pytest.raises(HistoryRefused, match="touch the same subjects"):
        store.redo("c1", undo_number, actor_id="dm1", role="dm")


def test_cascaded_undo_redoes_as_a_group(store):
    pay(store, "c1", "bank", "dave", 100)
    pay(store, "c1", "dave", "wend", 30)
    undo_number = store.undo("c1", 1, actor_id="dm1", role="dm", cascade=True)
    assert balances(store) == {}
    store.redo("c1", undo_number, actor_id="dm1", role="dm")
    assert balances(store) == {"bank": -100, "dave": 70, "wend": 30}


def test_players_may_undo_only_their_own_transactions_and_never_cascade(store):
    pay(store, "c1", "bank", "dave", 100, actor_id="dave-player")     # 1
    pay(store, "c1", "bank", "wend", 5, actor_id="wend-player")       # 2
    with pytest.raises(HistoryRefused, match="only undo their own"):
        store.undo("c1", 2, actor_id="dave-player", role="player")
    with pytest.raises(HistoryRefused, match="dependents|cascade"):
        store.undo("c1", 1, actor_id="dave-player", role="player")    # 2 shares 'bank'
    with pytest.raises(HistoryRefused, match="cascade"):
        store.undo("c1", 1, actor_id="dave-player", role="player", cascade=True)
    store.undo("c1", 2, actor_id="wend-player", role="player")
    assert balances(store) == {"bank": -100, "dave": 100}


def test_observers_cannot_undo(store):
    pay(store, "c1", "bank", "dave", 100)
    with pytest.raises(HistoryRefused, match="may not undo"):
        store.undo("c1", 1, actor_id="watcher", role="observer")


def test_undo_last_targets_the_latest_undoable_transaction(store):
    pay(store, "c1", "bank", "dave", 100, actor_id="dave-player")
    pay(store, "c1", "x", "y", 1, actor_id="dm1")
    store.undo_last("c1", actor_id="dave-player", role="player")      # skips dm1's, takes dave-player's
    assert balances(store) == {"x": -1, "y": 1}
    store.undo_last("c1", actor_id="dm1", role="dm")
    assert balances(store) == {}
    with pytest.raises(HistoryRefused, match="nothing to undo"):
        store.undo_last("c1", actor_id="dm1", role="dm")


def test_undoing_an_unknown_transaction_is_refused(store):
    with pytest.raises(HistoryRefused, match="no transaction 9"):
        store.undo("c1", 9, actor_id="dm1", role="dm")


def test_failed_undo_leaves_no_trace(store):
    pay(store, "c1", "bank", "dave", 100)
    count_before = store.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    with pytest.raises(HistoryRefused):
        store.undo("c1", 9, actor_id="dm1", role="dm")
    assert store.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == count_before


def test_reserved_event_types_cannot_be_forged(store):
    from reeve import ReeveError
    with pytest.raises(ReeveError, match="reserved"):
        with store.transaction("c1", actor_id="dm1", role="dm", command="x") as transaction:
            transaction.emit("TransactionsRetracted", {"target_sequence_numbers": [1]})


def test_status_cache_disagreeing_with_markers_is_detected(store):
    pay(store, "c1", "bank", "dave", 100)
    store.undo("c1", 1, actor_id="dm1", role="dm")
    store.connection.execute("UPDATE transactions SET status='active' WHERE sequence_number=1")
    assert store.verify_statuses("c1") is False


def test_undo_survives_rebuild_and_export(store):
    from reeve.export import export_jsonl
    pay(store, "c1", "bank", "dave", 100)
    pay(store, "c1", "bank", "wend", 1)
    store.undo("c1", 2, actor_id="dm1", role="dm")
    store.rebuild()
    assert balances(store) == {"bank": -100, "dave": 100}
    exported = export_jsonl(store, "c1")
    assert '"status":"retracted"' in exported and "TransactionsRetracted" in exported
