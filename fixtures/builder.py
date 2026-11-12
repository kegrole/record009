"""Deterministic rule-driven fixtures for the local mock pagination API.

The fixture files are regenerated from the rules in :mod:`fixtures.builder`,
so the acceptance suite can rebuild an equivalent server dataset instead of
depending on hand-written record ids.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PAGE_SIZE = 3
FIXTURE_DIR = Path(__file__).resolve().parent
DEFAULT_FIXTURE = FIXTURE_DIR / "dataset.json"

BASE_UPDATED_AT = "2026-01-01T00:00:00Z"


def stamp(minute: int) -> str:
    return f"2026-01-01T00:{minute:02d}:00Z"


def item(record_id: str, name: str, minute: int, deleted: bool = False) -> dict[str, Any]:
    return {
        "id": record_id,
        "name": name,
        "updated_at": stamp(minute),
        "deleted": deleted,
    }


def build_default_dataset() -> dict[str, Any]:
    """Build the nine record slots required by the acceptance rules.

    There are three pages of three slots. ``item-03`` is delivered on both
    the first and second pages to model duplicate delivery after a cursor
    becomes invalid. ``item-07`` is a tombstone. The eight distinct ids
    therefore leave seven live rows once the tombstone is propagated.
    """
    records = [
        item("item-01", "alpha", 1),
        item("item-02", "bravo", 2),
        item("item-03", "charlie", 3),
        item("item-03", "charlie", 3),
        item("item-04", "delta", 4),
        item("item-07", "golf-tombstone", 7, deleted=True),
        item("item-05", "echo", 5),
        item("item-06", "foxtrot", 6),
        item("item-08", "hotel", 8),
    ]
    return {"page_size": PAGE_SIZE, "pages": [records[0:3], records[3:6], records[6:9]]}


def build_versioned_dataset() -> dict[str, Any]:
    """Build a dataset whose duplicate delivery contains two versions.

    The second copy of ``item-03`` has a larger ``updated_at`` and must win,
    while the sync also records at least one conflict.
    """
    records = [
        item("item-01", "alpha", 1),
        item("item-02", "bravo", 2),
        item("item-03", "charlie-old", 3),
        item("item-03", "charlie-new", 9),
        item("item-04", "delta", 4),
        item("item-07", "golf-tombstone", 7, deleted=True),
        item("item-05", "echo", 5),
        item("item-06", "foxtrot", 6),
        item("item-08", "hotel", 8),
    ]
    return {"page_size": PAGE_SIZE, "pages": [records[0:3], records[3:6], records[6:9]]}


def save_dataset(dataset: dict[str, Any], path: str | Path = DEFAULT_FIXTURE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_dataset(path: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def apply_second_round_changes(dataset: dict[str, Any]) -> dict[str, Any]:
    """Mutate one existing record and add one record for the watermark test.

    The mutation turns an existing live record into a tombstone, while the new
    record is live. The number of live local rows therefore stays at seven.
    Both changes have timestamps above the first-round watermark.
    """
    changed_id = "item-04"
    for page in dataset["pages"]:
        for record in page:
            if record["id"] == changed_id:
                record["name"] = "delta-deleted"
                record["updated_at"] = stamp(20)
                record["deleted"] = True

    new_record = item("item-09", "india", 21)
    dataset["pages"][-1].append(new_record)
    return dataset


def main() -> None:
    path = save_dataset(build_default_dataset())
    print(path)


if __name__ == "__main__":
    main()
