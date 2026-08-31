"""Deterministic, world-local simulation fault state."""

from typing import get_args

from csaf.simulations.schema import FaultName

_FAULT_NAMES = frozenset(get_args(FaultName))


class FaultRegistry:
    """Track bounded transient faults without shared mutable state."""

    def __init__(self) -> None:
        self._remaining: dict[FaultName, int] = {}

    def set(self, name: FaultName, remaining_calls: int = 1) -> None:
        """Set or replace the number of times a named fault will fire."""

        self._validate_name(name)
        if type(remaining_calls) is not int:
            raise TypeError("remaining_calls must be an integer")
        if not 1 <= remaining_calls <= 100:
            raise ValueError("remaining_calls must be between 1 and 100")
        self._remaining[name] = remaining_calls

    def clear(self) -> None:
        """Remove every active fault."""

        self._remaining.clear()

    def consume(self, name: FaultName) -> bool:
        """Consume one call of a fault, returning whether it was active."""

        self._validate_name(name)
        remaining = self._remaining.get(name)
        if remaining is None:
            return False
        if remaining == 1:
            del self._remaining[name]
        else:
            self._remaining[name] = remaining - 1
        return True

    @staticmethod
    def _validate_name(name: object) -> None:
        if type(name) is not str or name not in _FAULT_NAMES:
            raise ValueError(f"unsupported simulation fault: {name!r}")
