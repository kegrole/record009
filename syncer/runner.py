"""Synchronization orchestration and command-line entry point."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from .client import SyncClient
from .pager import CursorPager
from .state import Checkpoint, CheckpointStore
from .store import ItemStore
from .watermark import max_timestamp

KILL_AFTER_PAGE_ENV = "SYNCER_KILL_AFTER_PAGE"


@dataclass(slots=True)
class SyncStats:
    requests: int = 0
    inserted: int = 0
    deleted: int = 0
    conflicts: int = 0

    def summary(self) -> str:
        return (
            f"sync summary: requests={self.requests} inserted={self.inserted} "
            f"deleted={self.deleted} conflicts={self.conflicts}"
        )


def _server_has_changes(client: SyncClient, watermark: str | None) -> bool:
    if watermark is None:
        return True
    meta = client.get_meta()
    latest = meta.get("latest_updated_at")
    return latest is not None and latest > watermark


def _kill_after_current_page(page_number: int) -> bool:
    value = os.environ.get(KILL_AFTER_PAGE_ENV)
    if value is None:
        return False
    try:
        return int(value) == page_number
    except ValueError:
        return False


def run_sync(
    base_url: str,
    db_path: str | Path,
    state_path: str | Path,
    limit: int | None = None,
) -> SyncStats:
    client = SyncClient(base_url)
    state_store = CheckpointStore(state_path)
    checkpoint = state_store.load()
    stats = SyncStats()

    if not checkpoint.active:
        if not _server_has_changes(client, checkpoint.watermark):
            stats.requests = client.items_requests
            return stats
        checkpoint.active = True
        checkpoint.resume_watermark = checkpoint.watermark
        checkpoint.next_cursor = ""
        state_store.save(checkpoint)

    pager = CursorPager(client, limit=limit)
    round_high_watermark = checkpoint.watermark
    cursor = checkpoint.next_cursor
    page_number = 0

    with ItemStore(db_path) as item_store:
        while True:
            page = pager.fetch(cursor, checkpoint.resume_watermark)
            for record in page.items:
                record_id = record["id"]
                round_high_watermark = max_timestamp(
                    round_high_watermark, record.get("updated_at")
                )
                if bool(record.get("deleted", False)):
                    if item_store.delete(record_id):
                        stats.deleted += 1
                    continue

                written, conflict = item_store.upsert(record)
                if written:
                    stats.inserted += 1
                if conflict:
                    stats.conflicts += 1

            page_number += 1
            item_store.commit()
            checkpoint.next_cursor = page.next_cursor
            state_store.save(checkpoint)

            if _kill_after_current_page(page_number):
                os._exit(2)

            if not page.next_cursor:
                break
            cursor = page.next_cursor

        checkpoint.watermark = round_high_watermark
        checkpoint.resume_watermark = None
        checkpoint.next_cursor = ""
        checkpoint.active = False
        checkpoint.completed_rounds += 1
        item_store.commit()
        state_store.save(checkpoint)

    stats.requests = client.items_requests
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incrementally synchronize a local pagination API.")
    parser.add_argument("--base", required=True, help="Base URL of the local mock API.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--state", required=True, help="JSON checkpoint path.")
    parser.add_argument("--limit", type=int, default=None, help="Optional page size.")
    return parser


def main(argv: list[str] | None = None) -> SyncStats:
    args = build_parser().parse_args(argv)
    stats = run_sync(args.base, args.db, args.state, limit=args.limit)
    print(stats.summary(), flush=True)
    return stats


if __name__ == "__main__":
    main()
