"""Standard-library HTTP implementation of the cursor-paginated mock API."""

from __future__ import annotations

import argparse
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fixtures.builder import DEFAULT_FIXTURE, PAGE_SIZE


def load_fixture(fixture_path: str | Path) -> dict[str, Any]:
    with Path(fixture_path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class ApiCounters:
    """Process-local request counters used by tests and the demo summary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items_requests = 0
        self.meta_requests = 0

    def record_items(self) -> None:
        with self._lock:
            self.items_requests += 1

    def record_meta(self) -> None:
        with self._lock:
            self.meta_requests += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "items_requests": self.items_requests,
                "meta_requests": self.meta_requests,
            }


def encode_cursor(cursor: dict[str, int]) -> str:
    raw = json.dumps(cursor, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(value: str) -> dict[str, int]:
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        cursor = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(cursor, dict) or "group" not in cursor or "offset" not in cursor:
        raise ValueError("invalid cursor")
    if not isinstance(cursor["group"], int) or not isinstance(cursor["offset"], int):
        raise ValueError("invalid cursor")
    if cursor["group"] < 0 or cursor["offset"] < 0:
        raise ValueError("invalid cursor")
    return cursor


def filtered_groups(dataset: dict[str, Any], since: str | None) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for page in dataset["pages"]:
        if since:
            page = [record for record in page if record["updated_at"] > since]
        if page:
            groups.append(list(page))
    return groups


def latest_updated_at(dataset: dict[str, Any]) -> str | None:
    values = [
        record["updated_at"]
        for page in dataset.get("pages", [])
        for record in page
    ]
    return max(values) if values else None


class MockApiHandler(BaseHTTPRequestHandler):
    server: "MockApiServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - HTTP handler naming
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if parsed.path == "/meta":
            self._handle_meta()
        elif parsed.path == "/items":
            self._handle_items(query)
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_meta(self) -> None:
        self.server.counters.record_meta()
        dataset = load_fixture(self.server.fixture_path)
        total_slots = sum(len(page) for page in dataset["pages"])
        self._send_json(
            200,
            {
                "latest_updated_at": latest_updated_at(dataset),
                "total_slots": total_slots,
            },
        )

    def _handle_items(self, query: dict[str, str]) -> None:
        dataset = load_fixture(self.server.fixture_path)
        default_limit = int(dataset.get("page_size", PAGE_SIZE))
        try:
            limit = int(query.get("limit", default_limit))
            if limit <= 0:
                raise ValueError
            cursor = decode_cursor(query["cursor"]) if query.get("cursor") else {"group": 0, "offset": 0}
        except ValueError:
            self._send_json(400, {"error": "invalid pagination parameters"})
            return

        self.server.counters.record_items()
        since = query.get("since") or None
        groups = filtered_groups(dataset, since)
        group_index = cursor["group"]
        offset = cursor["offset"]

        if group_index >= len(groups):
            self._send_json(200, {"items": [], "next_cursor": ""})
            return

        group = groups[group_index]
        page_items = group[offset:offset + limit]
        remaining = len(group) - offset - len(page_items)
        if remaining > 0:
            next_cursor = encode_cursor({"group": group_index, "offset": offset + limit})
        elif group_index + 1 < len(groups):
            next_cursor = encode_cursor({"group": group_index + 1, "offset": 0})
        else:
            next_cursor = ""
        self._send_json(200, {"items": page_items, "next_cursor": next_cursor})


class MockApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], fixture_path: str | Path) -> None:
        super().__init__(address, MockApiHandler)
        self.fixture_path = str(fixture_path)
        self.counters = ApiCounters()

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


def create_server(
    fixture_path: str | Path = DEFAULT_FIXTURE,
    host: str = "127.0.0.1",
    port: int = 0,
) -> MockApiServer:
    return MockApiServer((host, port), fixture_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local mock pagination API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    args = parser.parse_args()

    server = create_server(args.fixture, args.host, args.port)
    print(f"Mock API listening on {server.base_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
