"""Durable JSON checkpoint state.

The checkpoint records both the committed cursor and the watermark that was
active when that cursor was requested. A process killed between pages can
therefore resume without re-requesting committed pages.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Checkpoint:
    watermark: str | None = None
    next_cursor: str = ""
    resume_watermark: str | None = None
    active: bool = False
    completed_rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Checkpoint":
        return cls(
            watermark=payload.get("watermark"),
            next_cursor=payload.get("next_cursor", ""),
            resume_watermark=payload.get("resume_watermark"),
            active=bool(payload.get("active", False)),
            completed_rounds=int(payload.get("completed_rounds", 0)),
        )


class CheckpointStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> Checkpoint:
        if not self.path.exists():
            return Checkpoint()
        with self.path.open("r", encoding="utf-8") as handle:
            return Checkpoint.from_dict(json.load(handle))

    def save(self, checkpoint: Checkpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(checkpoint.to_dict(), indent=2, sort_keys=True) + "\n"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
