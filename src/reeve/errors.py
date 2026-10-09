"""Exceptions raised by the Reeve core."""

from __future__ import annotations


class ReeveError(Exception):
    """Base class for refusals raised by the core."""


class ChainError(ReeveError):
    """The hash chain does not verify: the log has been altered."""


class HistoryRefused(ReeveError):
    """An undo, redo or roll-void was refused.

    ``dependent_sequence_numbers`` lists later transactions that depend on the requested one,
    when that is the reason, so a caller can show them and offer a cascade.
    """

    def __init__(self, message: str, dependent_sequence_numbers: tuple[int, ...] = ()):
        super().__init__(message)
        self.dependent_sequence_numbers = dependent_sequence_numbers
