"""Injectable time and identifier dependencies."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

Now = Callable[[], datetime]
IdFactory = Callable[[], UUID]


def utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""

    return datetime.now(UTC)
