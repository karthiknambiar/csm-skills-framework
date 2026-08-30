"""Shared framework primitives."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from csaf.core.runtime import Runtime as Runtime
    from csaf.core.runtime import create_runtime as create_runtime

__all__ = ["Runtime", "create_runtime"]


def __getattr__(name: str) -> object:
    """Load runtime exports lazily to keep leaf-module imports cycle-safe."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from csaf.core.runtime import Runtime, create_runtime

    exports = {"Runtime": Runtime, "create_runtime": create_runtime}
    value = exports[name]
    globals()[name] = value
    return value
