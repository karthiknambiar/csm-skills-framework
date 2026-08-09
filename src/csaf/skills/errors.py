"""Errors raised by the Skills SDK."""


class SkillError(Exception):
    """Base error for skill registration and execution failures."""


class DuplicateSkillError(SkillError):
    """Raised when a registry already contains the requested skill name."""


class SkillNotFoundError(SkillError):
    """Raised when a caller requests a skill that is not registered."""


class SkillContractError(SkillError):
    """Raised when a skill violates its declared input, output, or effect contract."""


class SkillExecutionError(SkillError):
    """Raised when an injected provider cannot complete skill execution."""
