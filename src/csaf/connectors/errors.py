"""Domain errors raised by connector discovery and ingestion."""


class ConnectorError(Exception):
    """Base error for connector operations."""


class ConnectorAuthenticationError(ConnectorError):
    """Raised when credentials are missing or invalid."""


class ConnectorDataError(ConnectorError):
    """Raised when source data cannot be parsed or normalized."""


class DuplicateConnectorError(ConnectorError):
    """Raised when a registry already contains a connector name."""


class ConnectorNotFoundError(ConnectorError):
    """Raised when discovery cannot find a requested connector."""
