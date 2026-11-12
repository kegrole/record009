"""Helpers for RFC 3339 watermark comparison."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_newer(candidate: str | None, current: str | None) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    return parse_timestamp(candidate) > parse_timestamp(current)


def max_timestamp(left: str | None, right: str | None) -> str | None:
    if is_newer(right, left):
        return right
    return left
