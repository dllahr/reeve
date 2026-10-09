"""Dice notation and rolling.

Supports ``NdS`` with an optional ``+K`` / ``-K`` modifier, and ``d%`` for percentile.
The count may be omitted (``d20`` means ``1d20``). Ruleset-specific notation, such as
1e's range form ("3-18" meaning 3d6), belongs to the ruleset, which translates it into
this notation before asking the core to roll.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from .errors import ReeveError

_NOTATION = re.compile(r"^(?P<count>\d*)d(?P<sides>\d+|%)(?P<modifier>[+-]\d+)?$")
MAXIMUM_DICE_COUNT = 100
MAXIMUM_SIDES = 1000


@dataclass(frozen=True)
class DiceSpecification:
    count: int
    sides: int
    modifier: int

    @property
    def notation(self) -> str:
        """Canonical text form, used so equivalent spellings match when looking for reusable rolls."""
        modifier_text = f"{self.modifier:+d}" if self.modifier else ""
        return f"{self.count}d{self.sides}{modifier_text}"


@dataclass(frozen=True)
class RollOutcome:
    notation: str
    individual_results: tuple[int, ...]
    modifier: int
    total: int


def parse_dice(notation: str) -> DiceSpecification:
    match = _NOTATION.match(notation.replace(" ", ""))
    if match is None:
        raise ReeveError(f"cannot read dice notation {notation!r}; expected forms like 1d20, 3d6+1, d%")
    count = int(match["count"]) if match["count"] else 1
    sides = 100 if match["sides"] == "%" else int(match["sides"])
    modifier = int(match["modifier"]) if match["modifier"] else 0
    if not 1 <= count <= MAXIMUM_DICE_COUNT:
        raise ReeveError(f"dice count must be 1..{MAXIMUM_DICE_COUNT}, got {count}")
    if not 2 <= sides <= MAXIMUM_SIDES:
        raise ReeveError(f"dice sides must be 2..{MAXIMUM_SIDES}, got {sides}")
    return DiceSpecification(count, sides, modifier)


def roll_dice(specification: DiceSpecification, random_source: random.Random) -> RollOutcome:
    individual_results = tuple(random_source.randint(1, specification.sides) for _ in range(specification.count))
    return RollOutcome(
        notation=specification.notation,
        individual_results=individual_results,
        modifier=specification.modifier,
        total=sum(individual_results) + specification.modifier)
