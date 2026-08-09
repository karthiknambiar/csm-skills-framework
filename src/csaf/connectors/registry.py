"""Explicit connector discovery registry."""

from collections.abc import Iterator

from csaf.connectors.base import Connector
from csaf.connectors.errors import ConnectorNotFoundError, DuplicateConnectorError


class ConnectorRegistry:
    """Discover configured connector instances by stable metadata name."""

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        name = connector.metadata.name
        if name in self._connectors:
            raise DuplicateConnectorError(f"connector is already registered: {name}")
        self._connectors[name] = connector

    def get(self, name: str) -> Connector:
        try:
            return self._connectors[name]
        except KeyError as error:
            raise ConnectorNotFoundError(f"connector is not registered: {name}") from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._connectors))

    def __iter__(self) -> Iterator[Connector]:
        for name in self.names():
            yield self._connectors[name]

    def __len__(self) -> int:
        return len(self._connectors)
