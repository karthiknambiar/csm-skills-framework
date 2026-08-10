"""Durable filesystem delivery for CLI-generated artifacts."""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from csaf.skills import Artifact


def deliver_artifacts(
    artifacts: tuple[Artifact, ...],
    destinations: Mapping[str, Path],
) -> None:
    """Stage selected artifacts and atomically replace their destinations."""

    staged: list[tuple[Path, Path]] = []
    try:
        for artifact in artifacts:
            if Path(artifact.filename).name != artifact.filename:
                raise OSError(f"unsafe artifact filename: {artifact.filename}")
            destination = destinations.get(artifact.filename)
            if destination is None:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                staged.append((Path(temporary.name), destination))
                temporary.write(artifact.content)
                temporary.flush()
                os.fsync(temporary.fileno())

        for temporary, destination in staged:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass