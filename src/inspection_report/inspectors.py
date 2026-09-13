"""Inspector master-data lookup.

Reads assets/inspectors.csv and exposes a name -> Inspector record map.
Used by the cover (to fill certification number) and the appendix (to render
cert image proofs).
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Inspector:
    first_name: str
    last_name: str
    certification_number: str
    cert_image: str  # relative path under assets/

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


def load(csv_path: Path) -> dict[str, Inspector]:
    """Return a dict keyed by lowercase 'first last' display name."""
    if not csv_path.exists():
        log.warning("inspectors.csv not found at %s — cert lookup disabled", csv_path)
        return {}
    out: dict[str, Inspector] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            insp = Inspector(
                first_name=row["first_name"].strip(),
                last_name=row["last_name"].strip(),
                certification_number=row["certification_number"].strip(),
                cert_image=row.get("cert_image", "").strip(),
            )
            out[insp.display_name.lower()] = insp
    return out


FUZZY_THRESHOLD = 0.85


def find(directory: dict[str, Inspector], name: str | None) -> Inspector | None:
    """Look up an inspector by name, tolerating spelling drift between Yardi
    and the cert roster (e.g. Yardi's 'Tomás Herrera' vs the certificate's
    'Tomas Herrera'). Exact match first, then closest fuzzy match."""
    if not name:
        return None
    key = name.strip().lower()
    exact = directory.get(key)
    if exact:
        return exact

    best: Inspector | None = None
    best_score = FUZZY_THRESHOLD
    for known, insp in directory.items():
        score = SequenceMatcher(None, key, known).ratio()
        if score > best_score:
            best_score = score
            best = insp
    if best:
        log.info("Inspector %r fuzzy-matched to %r (%.0f%%)", name, best.display_name, best_score * 100)
    return best
