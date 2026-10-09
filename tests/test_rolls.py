import random

import pytest

from reeve import HistoryRefused, ReeveError, Store
from conftest import Ledger, make_ticking_clock, pay


def roll_in_new_transaction(store, key="attack:dave:goblin-1:round-1", dice="1d20", *, count=1,
                            actor_id="dave-player", role="player", subjects=("dave",)):
    outcomes = []
    with store.transaction("c1", actor_id=actor_id, role=role, command="attack") as transaction:
        for _ in range(count):
            outcomes.append(transaction.roll(key, dice, subjects=subjects))
    return outcomes


def roll_events(store, include_retracted=True):
    return [event for event in store.events("c1", include_retracted=include_retracted) if event.type == "RollMade"]


def test_a_roll_is_recorded_as_an_event(store):
    outcome, = roll_in_new_transaction(store, dice="3d6+1")
    event, = roll_events(store)
    assert event.payload["key"] == "attack:dave:goblin-1:round-1"
    assert event.payload["dice"] == "3d6+1"
    assert event.payload["total"] == outcome.total
    assert event.payload["results"] == list(outcome.individual_results)
    assert event.payload["reused_from"] is None
    assert store.verify_chain("c1") == 1


def test_rolls_are_reproducible_with_a_seeded_source():
    def run():
        store = Store(":memory:", clock=make_ticking_clock(), random_source=random.Random(99))
        store.create_campaign("c1", "toy")
        return roll_in_new_transaction(store, count=5)
    assert run() == run()


def test_bad_roll_keys_and_dice_are_refused(store):
    with pytest.raises(ReeveError, match="roll key"):
        roll_in_new_transaction(store, key="has spaces")
    with pytest.raises(ReeveError, match="dice notation"):
        roll_in_new_transaction(store, dice="3-18")


def test_an_undone_roll_is_reused_by_the_same_key_and_dice(store):
    original, = roll_in_new_transaction(store)
    store.undo("c1", 1, actor_id="dave-player", role="player")
    again, = roll_in_new_transaction(store)
    assert again == original
    original_event, reused_event = roll_events(store)
    assert reused_event.payload["reused_from"] == original_event.sequence_number


def test_a_roll_is_not_reused_while_its_transaction_is_active(store):
    roll_in_new_transaction(store, count=1)
    roll_in_new_transaction(store, count=1)
    assert [event.payload["reused_from"] for event in roll_events(store)] == [None, None]


def test_a_roll_is_reused_only_once(store):
    roll_in_new_transaction(store)
    store.undo("c1", 1, actor_id="dave-player", role="player")
    roll_in_new_transaction(store)                       # consumes the original (transaction 3)
    roll_in_new_transaction(store)                       # nothing left to reuse: fresh dice
    reused_from_values = [event.payload["reused_from"] for event in roll_events(store)]
    assert reused_from_values[0] is None
    assert reused_from_values[1] == roll_events(store)[0].sequence_number
    assert reused_from_values[2] is None


def test_different_key_or_different_dice_means_fresh_dice(store):
    roll_in_new_transaction(store, key="attack:a", dice="1d20")
    store.undo("c1", 1, actor_id="dave-player", role="player")
    roll_in_new_transaction(store, key="attack:b", dice="1d20")
    roll_in_new_transaction(store, key="attack:a", dice="1d8")
    assert [event.payload["reused_from"] for event in roll_events(store)] == [None, None, None]


def test_equivalent_dice_spellings_still_match(store):
    first, = roll_in_new_transaction(store, dice="d20")
    store.undo("c1", 1, actor_id="dave-player", role="player")
    second, = roll_in_new_transaction(store, dice="1d20")
    assert second == first


def test_several_unused_rolls_are_reused_oldest_first_each_once(store):
    first, second = roll_in_new_transaction(store, count=2)
    store.undo("c1", 1, actor_id="dave-player", role="player")
    reused = roll_in_new_transaction(store, count=2)
    assert reused == [first, second]
    roll_in_new_transaction(store)                        # both consumed already
    assert [event.payload["reused_from"] for event in roll_events(store)][-1] is None


def test_a_voided_roll_is_never_reused(store):
    roll_in_new_transaction(store)
    store.undo("c1", 1, actor_id="dave-player", role="player")
    original_roll_number = roll_events(store)[0].sequence_number
    store.void_roll("c1", original_roll_number, actor_id="dm1", role="dm", reason="rolled for the wrong target")
    roll_in_new_transaction(store)
    assert [event.payload["reused_from"] for event in roll_events(store)] == [None, None]
    assert store.verify_statuses("c1") and store.verify_chain("c1")


def test_void_is_dm_only_needs_a_reason_and_an_undone_roll(store):
    roll_in_new_transaction(store)
    roll_number = roll_events(store)[0].sequence_number
    with pytest.raises(HistoryRefused, match="only the DM"):
        store.void_roll("c1", roll_number, actor_id="dave-player", role="player", reason="nope")
    with pytest.raises(HistoryRefused, match="reason"):
        store.void_roll("c1", roll_number, actor_id="dm1", role="dm", reason="")
    with pytest.raises(HistoryRefused, match="undo the transaction first"):
        store.void_roll("c1", roll_number, actor_id="dm1", role="dm", reason="wrong")
    with pytest.raises(HistoryRefused, match="not a roll"):
        store.void_roll("c1", 99, actor_id="dm1", role="dm", reason="wrong")
    store.undo("c1", 1, actor_id="dave-player", role="player")
    store.void_roll("c1", roll_number, actor_id="dm1", role="dm", reason="wrong")
    with pytest.raises(HistoryRefused, match="already voided"):
        store.void_roll("c1", roll_number, actor_id="dm1", role="dm", reason="again")


def test_redo_is_refused_if_its_roll_was_reused_or_voided(store):
    roll_in_new_transaction(store, subjects=("dave",))
    undo_number = store.undo("c1", 1, actor_id="dave-player", role="player")
    roll_in_new_transaction(store, subjects=("someone-else",))     # reuses the roll; no subject overlap
    with pytest.raises(HistoryRefused, match="reused elsewhere"):
        store.redo("c1", undo_number, actor_id="dave-player", role="player")

    roll_in_new_transaction(store, key="other", subjects=("dave",))
    second_undo = store.undo("c1", 4, actor_id="dave-player", role="player")
    voided_number = [event for event in roll_events(store) if event.payload["key"] == "other"][0].sequence_number
    store.void_roll("c1", voided_number, actor_id="dm1", role="dm", reason="wrong")
    with pytest.raises(HistoryRefused, match="voided"):
        store.redo("c1", second_undo, actor_id="dave-player", role="player")


def test_redo_of_an_undone_roll_brings_the_same_result_back(store):
    original, = roll_in_new_transaction(store)
    undo_number = store.undo("c1", 1, actor_id="dave-player", role="player")
    store.redo("c1", undo_number, actor_id="dave-player", role="player")
    event, = roll_events(store, include_retracted=False)
    assert event.payload["total"] == original.total


def test_roll_subjects_make_rolls_part_of_dependency_tracking(store):
    pay(store, "c1", "bank", "dave", 5)                              # 1
    roll_in_new_transaction(store, subjects=("dave",))               # 2 touches 'dave' => depends on 1
    with pytest.raises(HistoryRefused) as refusal:
        store.undo("c1", 1, actor_id="dm1", role="dm")
    assert refusal.value.dependent_sequence_numbers == (2,)
