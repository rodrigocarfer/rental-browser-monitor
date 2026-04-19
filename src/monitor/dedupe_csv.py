from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from monitor.models import Listing


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class NotifiedRow:
    url: str
    title: str
    first_seen_at: str
    notified_at: str


CSV_HEADERS = ["url", "title", "first_seen_at", "notified_at"]


def load_notified_urls(csv_path: Path) -> set[str]:
    if not csv_path.is_file():
        return set()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        urls: set[str] = set()
        for row in r:
            url = (row.get("url") or "").strip()
            if url:
                urls.add(url)
        return urls


def ensure_csv_initialized(csv_path: Path) -> None:
    if csv_path.is_file():
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        w.writeheader()


def filter_new_listings(*, listings: list[Listing], notified_urls: set[str]) -> list[Listing]:
    out: list[Listing] = []
    for li in listings:
        if li.url in notified_urls:
            continue
        out.append(li)
    return out


def append_notified(*, csv_path: Path, listings: list[Listing]) -> None:
    if not listings:
        return
    ensure_csv_initialized(csv_path)
    now = _utc_now_iso()
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        for li in listings:
            w.writerow(
                {
                    "url": li.url,
                    "title": li.title,
                    "first_seen_at": now,
                    "notified_at": now,
                }
            )

