from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fixtures.builder import (
    apply_second_round_changes,
    build_default_dataset,
    build_versioned_dataset,
    save_dataset,
)
from mock_api.server import create_server
from syncer.runner import run_sync
from syncer.store import ItemStore


REPO_ROOT = Path(__file__).resolve().parents[1]


class ServerThread:
    def __init__(self, fixture_path: Path) -> None:
        self.server = create_server(fixture_path, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return self.server.base_url

    @property
    def item_requests(self) -> int:
        return self.server.counters.items_requests

    def start(self) -> "ServerThread":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class SynchronizerAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.fixture_path = self.temp_path / "dataset.json"
        save_dataset(build_default_dataset(), self.fixture_path)
        self.server = ServerThread(self.fixture_path).start()
        self.db_path = self.temp_path / "sync.db"
        self.state_path = self.temp_path / "state.json"

    def tearDown(self) -> None:
        self.server.stop()
        self.temp_dir.cleanup()

    def run_syncer(self) -> object:
        return run_sync(self.server.base_url, self.db_path, self.state_path)

    def row_count(self) -> int:
        with sqlite3.connect(self.db_path) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    def row(self, record_id: str) -> tuple[str, str] | None:
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(
                "SELECT name, updated_at FROM items WHERE id = ?", (record_id,)
            ).fetchone()

    def test_01_server_dataset_matches_required_three_page_rule(self) -> None:
        dataset = build_default_dataset()
        pages = dataset["pages"]
        records = [record for page in pages for record in page]

        self.assertEqual(len(records), 9)
        self.assertEqual(len(pages), 3)
        self.assertTrue(all(len(page) == 3 for page in pages))

        identifiers = [record["id"] for record in records]
        self.assertEqual(len(set(identifiers)), 8)
        duplicated_id = next(
            record_id for record_id in set(identifiers) if identifiers.count(record_id) == 2
        )
        first_page_ids = {record["id"] for record in pages[0]}
        second_page_ids = {record["id"] for record in pages[1]}
        self.assertIn(duplicated_id, first_page_ids)
        self.assertIn(duplicated_id, second_page_ids)
        self.assertEqual(sum(1 for record in pages[1] if record["deleted"]), 1)
        self.assertEqual(sum(1 for record in records if record["deleted"]), 1)

    def test_02_first_sync_deduplicates_and_physically_deletes(self) -> None:
        stats = self.run_syncer()

        self.assertEqual(self.row_count(), 7)
        with sqlite3.connect(self.db_path) as connection:
            duplicate_count = connection.execute(
                "SELECT COUNT(*) FROM items WHERE id = 'item-03'"
            ).fetchone()[0]
        self.assertEqual(duplicate_count, 1)
        self.assertIsNone(self.row("item-07"))
        self.assertEqual(stats.inserted, 7)
        self.assertEqual(stats.conflicts, 0)

        with ItemStore(self.db_path) as seeded_store:
            seeded_store.connection.execute(
                "INSERT INTO items (id, name, updated_at, deleted) VALUES "
                "('item-07', 'stale tombstone target', '2026-01-01T00:00:00Z', 0)"
            )
            seeded_store.commit()
        self.state_path.unlink()
        propagation_stats = self.run_syncer()
        self.assertIsNone(self.row("item-07"))
        self.assertEqual(propagation_stats.deleted, 1)

    def test_03_repeated_sync_is_idempotent_and_creates_no_duplicate_rows(self) -> None:
        self.run_syncer()
        repeated_stats = self.run_syncer()

        self.assertEqual(self.row_count(), 7)
        self.assertEqual(repeated_stats.inserted, 0)
        self.assertEqual(repeated_stats.deleted, 0)
        self.assertEqual(repeated_stats.conflicts, 0)
        with sqlite3.connect(self.db_path) as connection:
            duplicate_rows = connection.execute(
                """
                SELECT id, COUNT(*) AS occurrences
                FROM items
                GROUP BY id
                HAVING occurrences > 1
                """
            ).fetchall()
        self.assertEqual(duplicate_rows, [])

    def test_04_watermark_fetches_only_changed_records_in_two_requests(self) -> None:
        first_stats = self.run_syncer()
        self.assertEqual(first_stats.requests, 3)

        changed_dataset = apply_second_round_changes(json.loads(self.fixture_path.read_text()))
        save_dataset(changed_dataset, self.fixture_path)
        second_stats = self.run_syncer()

        self.assertEqual(second_stats.requests, 2)
        self.assertEqual(second_stats.inserted, 1)
        self.assertEqual(second_stats.deleted, 1)
        self.assertEqual(self.row_count(), 7)
        self.assertIsNone(self.row("item-04"))
        self.assertIsNotNone(self.row("item-09"))
        checkpoint = json.loads(self.state_path.read_text())
        self.assertEqual(checkpoint["watermark"], "2026-01-01T00:21:00Z")

    def test_05_unchanged_third_round_makes_zero_item_requests(self) -> None:
        self.run_syncer()
        changed_dataset = apply_second_round_changes(json.loads(self.fixture_path.read_text()))
        save_dataset(changed_dataset, self.fixture_path)
        self.run_syncer()

        requests_before = self.server.item_requests
        third_stats = self.run_syncer()

        self.assertEqual(third_stats.requests, 0)
        self.assertEqual(self.server.item_requests, requests_before)
        self.assertEqual(self.row_count(), 7)

    def test_06_checkpoint_resumes_after_kill_without_replaying_pages(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["SYNCER_KILL_AFTER_PAGE"] = "2"
        command = [
            sys.executable,
            "-m",
            "syncer.runner",
            "--base",
            self.server.base_url,
            "--db",
            str(self.db_path),
            "--state",
            str(self.state_path),
        ]
        killed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(killed.returncode, 2)
        self.assertEqual(self.server.item_requests, 2)
        checkpoint = json.loads(self.state_path.read_text())
        self.assertTrue(checkpoint["active"])
        self.assertNotEqual(checkpoint["next_cursor"], "")

        resumed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=os.environ.copy() | {"PYTHONPATH": str(REPO_ROOT)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertEqual(self.server.item_requests, 3)
        self.assertLessEqual(self.server.item_requests, 3 + 1)
        self.assertEqual(self.row_count(), 7)
        self.assertIsNone(self.row("item-07"))
        summary_pattern = (
            r"requests=(?P<requests>\d+).*inserted=(?P<inserted>\d+).*"
            r"deleted=(?P<deleted>\d+).*conflicts=(?P<conflicts>\d+)"
        )
        self.assertRegex(resumed.stdout.strip(), summary_pattern)

    def test_07_newer_remote_version_wins_and_conflict_is_counted(self) -> None:
        save_dataset(build_versioned_dataset(), self.fixture_path)
        stats = self.run_syncer()

        self.assertGreaterEqual(stats.conflicts, 1)
        name, updated_at = self.row("item-03")
        self.assertEqual(name, "charlie-new")
        self.assertEqual(updated_at, "2026-01-01T00:09:00Z")
        self.assertEqual(self.row_count(), 7)

    def test_08_id_has_a_database_unique_constraint(self) -> None:
        with ItemStore(self.db_path) as store:
            store.connection.execute(
                "INSERT INTO items (id, name, updated_at) VALUES "
                "('same-id', 'first', '2026-01-01T00:01:00Z')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(
                    "INSERT INTO items (id, name, updated_at) VALUES "
                    "('same-id', 'second', '2026-01-01T00:02:00Z')"
                )

        with sqlite3.connect(self.db_path) as connection:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
            ).fetchone()[0]
        self.assertIn("PRIMARY KEY", sql)

    def test_09_summary_contains_all_four_required_metrics(self) -> None:
        stats = self.run_syncer()
        summary = stats.summary()

        for metric in ("requests", "inserted", "deleted", "conflicts"):
            self.assertTrue(
                re.search(rf"\b{metric}=\d+", summary),
                f"summary missing metric {metric}: {summary}",
            )


if __name__ == "__main__":
    unittest.main()
