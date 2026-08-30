"""Shared framework primitives."""

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
