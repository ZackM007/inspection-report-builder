"""Parse a Yardi inspection PDF into the same Property graph as xlsx_parser.

The Yardi PDF layout (verified against Prop 4655, 12 units, 112 pages):
  - Each unit spans ~10 pages.
  - First page of a unit contains 'Inspection ID' and the header fields
    (Inspector / Inspected Date / Property Code / Unit Code / Tenant Name /
    Unit Address) as label-then-value lines.
  - Findings table starts after the 'Annual Unit Inspection - Maintenance' header.
    Each finding is a multi-line observation followed by a 'Yes' / 'No' status word,
    optionally followed by an Action (Repair/Replace), Work Order number, and
    [Inspector M/D/YYYY HH:MM PM]\\nNote pair.
  - Last page of a unit contains 'Overall Result' and the signature section.
  - Photos sit on every page with bboxes; we extract them dedup'd by content hash
    and assign one per actionable finding by document order (v1).

Photos in the PDF are embedded at ~229x305 px — 2.3x the xlsx thumbnail size — the
main reason we prefer the PDF as the primary input.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import fitz

from ..models import (
    Action,
    Finding,
    KPIs,
    Photo,
    Property,
    Status,
    Unit,
    common_issues,
    compute_kpis,
)
from .common import categorize, parse_date

log = logging.getLogger(__name__)

UNIT_START_MARKER = "Inspection ID"
UNIT_END_MARKER = "Overall Result"
SECTION_HEADER_HINT = ("Annual", "Inspection")  # both substrings appear on findings-table header

HEADER_FIELDS = (
    # Ordered list — PDF lays labels then values in document order, so the
    # parser uses this order to know which value belongs to which label when
    # consecutive labels appear with no value between them.
    "Inspector", "Inspected Date", "Entity Type",
    "Property Code", "Unit Code", "Unit Address",
    "Overall Result", "Overall Notes", "Tenant Response",
)
HEADER_FIELD_SET = set(HEADER_FIELDS)
# Tenant Name omitted deliberately — it's often blank and breaks the
# label/value pairing heuristic by stealing the next field's value.
COLUMN_HEADERS = {
    "Results", "Observation", "Work Order", "Charge",
    "Responsibility", "Detail Level Notes", "Photo",
}
STATUS_WORDS = {"Yes", "No", "N/A"}
ACTION_WORDS = {"Repair", "Replace", "Inspect", "Monitor"}

TIMESTAMP_RX = re.compile(r"\[[^\]]*\d{1,2}:\d{2}[^\]]*\]")

MAX_SUPPLEMENTAL_PHOTOS = 6
PHOTOS_PER_FINDING_MAX = 6


def parse_pdf(path: Path, media_out_dir: Path) -> Property:
    media_out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(path)

    unit_ranges = _find_unit_ranges(doc)
    log.info("Found %d unit(s) across %d pages of %s", len(unit_ranges), len(doc), path.name)

    units: list[Unit] = []
    for start, end in unit_ranges:
        unit = _parse_unit(doc, start, end, media_out_dir)
        if unit:
            units.append(unit)

    return _build_property(units, path)


# ---------- unit page-range detection ---------------------------------------

def _find_unit_ranges(doc) -> list[tuple[int, int]]:
    """Return [(start_page_idx, end_page_idx)] for each unit, both inclusive."""
    starts: list[int] = []
    ends: list[int] = []
    for i, page in enumerate(doc):
        text = page.get_text()
        if UNIT_START_MARKER in text:
            starts.append(i)
        if UNIT_END_MARKER in text:
            ends.append(i)

    ranges: list[tuple[int, int]] = []
    used_ends: set[int] = set()
    for s in starts:
        for e in ends:
            if e >= s and e not in used_ends:
                ranges.append((s, e))
                used_ends.add(e)
                break
    return ranges


# ---------- per-unit parsing -------------------------------------------------

def _parse_unit(doc, start_page: int, end_page: int, media_dir: Path) -> Unit | None:
    lines: list[str] = []
    photos: list[Photo] = []
    seen_hashes: set[str] = set()

    for pi in range(start_page, end_page + 1):
        page = doc[pi]
        for line in page.get_text().splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)

        for img_ref in page.get_images(full=True):
            xref = img_ref[0]
            info = doc.extract_image(xref)
            img_bytes = info.get("image") or b""
            digest = hashlib.md5(img_bytes).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            out_name = f"u{start_page:03d}_x{xref}.{info.get('ext', 'jpg')}"
            out_path = media_dir / out_name
            if not out_path.exists():
                out_path.write_bytes(img_bytes)
            photos.append(Photo(path=out_path))

    lines = _join_bracket_lines(lines)

    header = _extract_header_fields(lines)
    unit_code = (header.get("Unit Code") or "").strip()
    if not unit_code:
        return None

    findings, work_order_id = _extract_findings(lines)
    supplemental = _assign_photos_to_findings(findings, photos)

    status: Status = "Action Required" if any(f.action for f in findings) else "No Action Required"

    return Unit(
        number=unit_code,
        inspector=(header.get("Inspector") or "").strip(),
        tenant=(header.get("Tenant Name") or "").strip(),
        address=_clean_address(header.get("Unit Address") or ""),
        inspected_at=parse_date(header.get("Inspected Date")),
        work_order_id=work_order_id,
        status=status,
        findings=findings,
        photos=supplemental,
    )


# ---------- header field extraction -----------------------------------------

def _extract_header_fields(lines: list[str]) -> dict[str, str]:
    """Yardi PDF puts header labels first (one per line, several in a row), then
    values (one per line, in the same order). 'Property Code' and 'Unit Code'
    are consecutive labels with values '4655' and '101' on consecutive lines.

    Strategy: queue labels as we see them; pop one per value line that follows.
    """
    out: dict[str, str] = {}
    pending: list[str] = []
    i = 0
    while i < min(80, len(lines)):
        line = lines[i]
        if line in COLUMN_HEADERS:
            break  # findings table starts here
        if line in HEADER_FIELD_SET:
            pending.append(line)
        elif pending and line and line not in STATUS_WORDS:
            label = pending.pop(0)
            if label not in out:
                out[label] = line
        i += 1
    return out


def _clean_address(addr: str) -> str:
    """'820 Wrenfield Ave. 101 Torrance, CA - 90504 us' -> tidier form."""
    a = addr.strip()
    if a.lower().endswith(" us"):
        a = a[:-3].strip()
    a = a.replace(" - ", " ")
    # Drop the unit-number token that Yardi sometimes embeds (e.g. '101 Torrance').
    # Heuristic: a 1-4 digit token that sits BETWEEN a street suffix and a city.
    a = re.sub(r"(Ave\.|St\.|Blvd\.|Rd\.|Dr\.|Ln\.|Way|Ct\.)\s+\d{1,4}\s+", r"\1, ", a)
    return " ".join(a.split())


# ---------- findings extraction ---------------------------------------------

def _extract_findings(lines: list[str]) -> tuple[list[Finding], str | None]:
    findings: list[Finding] = []
    work_order_id: str | None = None

    # Find the line after the section header where findings start
    start_idx = 0
    for i, ln in enumerate(lines):
        if all(h in ln for h in SECTION_HEADER_HINT) or ln.startswith("Annual Unit Inspection"):
            start_idx = i + 1
            break

    accumulator: list[str] = []
    i = start_idx
    while i < len(lines):
        line = lines[i]

        # End of findings table
        if line.startswith(UNIT_END_MARKER) or "Signatures" in line:
            break

        # Skip recurring page-chrome lines
        if line.startswith("Inspection Analytics Report") or line.startswith("Page "):
            i += 1
            continue
        if line in COLUMN_HEADERS:
            i += 1
            continue

        if line in STATUS_WORDS:
            observation = " ".join(accumulator).strip()
            accumulator = []

            if not observation or observation == "-Maintenance" or observation.startswith("-"):
                i += 1
                continue

            # Yes/N/A items are passed — skip their lookahead entirely.
            if line == "Yes":
                i += 1
                continue

            # 'No': look ahead for action, WO, and note(s).
            # Notes only collected AFTER a [timestamp] line and only until we see
            # what looks like a new observation (capitalized prose that doesn't
            # follow a timestamp).
            action: Action | None = None
            wo: str | None = None
            note_parts: list[str] = []
            collecting_note = False
            j = i + 1
            while j < len(lines):
                nx = lines[j]
                if nx in STATUS_WORDS:
                    break
                if nx.startswith(UNIT_END_MARKER) or "Signatures" in nx:
                    break
                if nx in COLUMN_HEADERS or nx.startswith("Inspection Analytics") or nx.startswith("Page "):
                    j += 1
                    continue
                if nx in ACTION_WORDS:
                    action = nx  # type: ignore[assignment]
                elif nx.isdigit() and len(nx) >= 5:
                    wo = nx
                    if not work_order_id:
                        work_order_id = wo
                elif TIMESTAMP_RX.search(nx) or (nx.startswith("[") and nx.endswith("]")):
                    if note_parts:
                        note_parts.append("·")  # separator between multiple notes
                    collecting_note = True
                elif collecting_note:
                    if _looks_like_new_observation(nx, note_parts):
                        break  # the next finding's observation has started
                    note_parts.append(nx)
                j += 1

            if action or note_parts:
                findings.append(Finding(
                    category=categorize(observation),
                    observation=observation,
                    action=action,
                    tech_note=_clean_note(" ".join(note_parts)),
                ))
            i = j
            continue

        # Otherwise it's observation text; accumulate
        accumulator.append(line)
        i += 1

    return findings, work_order_id


def _clean_note(note: str) -> str:
    cleaned = TIMESTAMP_RX.sub("", note)
    return " ".join(cleaned.split()).strip()


def _join_bracket_lines(lines: list[str]) -> list[str]:
    """PDF text reflow often splits '[Inspector M/D/YYYY HH:MM PM]' across
    two lines. Stitch any opening-bracket line to subsequent lines until we
    find the closing ']'."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "[" in line and "]" not in line:
            combined = line
            j = i + 1
            while j < len(lines) and j < i + 4:
                combined += " " + lines[j]
                if "]" in lines[j]:
                    j += 1
                    break
                j += 1
            out.append(combined)
            i = j
        else:
            out.append(line)
            i += 1
    return out


# Common observation-starting phrases in Yardi inspections. If a line during
# note-collection starts with one of these, the note has ended and the next
# finding's observation has begun.
_OBSERVATION_STARTS = (
    "Front door", "Paint, ceiling", "Carpet and flooring", "Kitchen cabinets",
    "Windows and screens", "Smoke alarms", "All gfci", "All plumbing",
    "Bathtub", "Refrigerator", "Stove", "Microwave", "Dishwasher",
    "Tenant has", "Tenants have", "Hot water", "HVAC",
)


def _looks_like_new_observation(line: str, note_parts: list[str]) -> bool:
    """Heuristic: is this line the start of the next finding's observation,
    rather than a continuation of the current note?"""
    if not line:
        return False
    for prefix in _OBSERVATION_STARTS:
        if line.startswith(prefix):
            return True
    # If we already have a fair amount of note and this line starts with a
    # capital + isn't list-y, treat as new observation.
    if (
        len(note_parts) >= 6
        and line[:1].isupper()
        and not line.startswith("- ")
        and len(line) >= 25
    ):
        return True
    return False


# ---------- photo assignment -------------------------------------------------

def _assign_photos_to_findings(findings: list[Finding], photos: list[Photo]) -> list[Photo]:
    """Distribute photos across actionable findings in document order.

    PDF photos have no row anchors, so we split evenly: with N photos and K
    actionable findings, each finding gets up to ceil(N/K) photos (capped at
    PHOTOS_PER_FINDING_MAX). Owners read the narrative to know which item is
    flagged; the grid shows the inspection scope.
    """
    actionable_idx = [i for i, f in enumerate(findings) if f.action]
    if not actionable_idx:
        return photos[:MAX_SUPPLEMENTAL_PHOTOS]

    per_finding = min(
        PHOTOS_PER_FINDING_MAX,
        max(1, -(-len(photos) // len(actionable_idx))),  # ceil div
    )
    used = 0
    for idx in actionable_idx:
        chunk = photos[used:used + per_finding]
        findings[idx].photos.extend(chunk)
        used += len(chunk)
    return photos[used:][:MAX_SUPPLEMENTAL_PHOTOS]


# ---------- property assembly ------------------------------------------------

def _build_property(units: list[Unit], path: Path) -> Property:
    if not units:
        return Property(name=path.stem, address="", inspector="", kpis=KPIs())
    first = units[0]
    return Property(
        name=path.stem,
        address=first.address,
        inspector=first.inspector,
        report_date=first.inspected_at,
        kpis=compute_kpis(units),
        common_issues=common_issues(units),
        units=units,
    )
