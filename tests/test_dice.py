import random

import pytest

from reeve import ReeveError
from reeve.dice import parse_dice, roll_dice


@pytest.mark.parametrize("notation, count, sides, modifier", [
    ("1d20", 1, 20, 0), ("d20", 1, 20, 0), ("3d6+1", 3, 6, 1), ("2d4 - 2", 2, 4, -2), ("d%", 1, 100, 0),
])
def test_valid_notation(notation, count, sides, modifier):
    specification = parse_dice(notation)
    assert (specification.count, specification.sides, specification.modifier) == (count, sides, modifier)


@pytest.mark.parametrize("notation", ["", "d", "3-18", "1d1", "0d6", "101d6", "1d1001", "d20+", "banana"])
def test_invalid_notation_is_refused(notation):
    with pytest.raises(ReeveError):
        parse_dice(notation)


def test_equivalent_spellings_share_a_canonical_form():
    assert parse_dice("d20").notation == parse_dice("1d20").notation == "1d20"
    assert parse_dice("3d6 + 1").notation == "3d6+1"


def test_rolls_are_in_range_and_reproducible_with_a_seed():
    specification = parse_dice("3d6+2")
    first = [roll_dice(specification, random.Random(7)) for _ in range(1)][0]
    second = roll_dice(specification, random.Random(7))
    assert first == second
    assert all(1 <= die <= 6 for die in first.individual_results)
    assert first.total == sum(first.individual_results) + 2


def test_distribution_hits_every_face():
    random_source = random.Random(1)
    faces = {roll_dice(parse_dice("1d6"), random_source).total for _ in range(200)}
    assert faces == {1, 2, 3, 4, 5, 6}
