"""Cursor pagination iterator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from .client import ApiError, SyncClient


@dataclass(slots=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: str


class CursorPager:
    def __init__(self, client: SyncClient, limit: int | None = None) -> None:
        self.client = client
        self.limit = limit

    def fetch(self, cursor: str, since: str | None) -> Page:
        payload = self.client.get_items(cursor=cursor, limit=self.limit, since=since)
        items = payload.get("items")
        next_cursor = payload.get("next_cursor")
        if not isinstance(items, list) or not isinstance(next_cursor, str):
            raise ApiError("server response must contain 'items' and string 'next_cursor'")
        return Page(items=items, next_cursor=next_cursor)

    def iter_pages(self, since: str | None, start_cursor: str = "") -> Iterator[Page]:
        cursor = start_cursor
        while True:
            page = self.fetch(cursor, since)
            yield page
            if not page.next_cursor:
                return
            cursor = page.next_cursor
