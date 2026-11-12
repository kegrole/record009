"""Small JSON HTTP client built only from the Python standard library."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    pass


class SyncClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.items_requests = 0
        self.meta_requests = 0

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"GET {path} failed with HTTP {exc.code}: {body}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ApiError(f"GET {path} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(f"GET {path} returned a non-object JSON payload")
        return payload

    def get_meta(self) -> dict[str, Any]:
        self.meta_requests += 1
        return self._get_json("/meta", {})

    def get_items(
        self,
        cursor: str = "",
        limit: int | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        self.items_requests += 1
        params: dict[str, Any] = {"cursor": cursor, "limit": limit, "since": since}
        return self._get_json("/items", params)
