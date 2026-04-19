from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Listing:
    source_name: str
    url: str
    title: str

