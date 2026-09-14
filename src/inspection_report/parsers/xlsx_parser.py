"""Parse Yardi-style xlsx inspection reports into a Property object.

Handles the "Inspection Analytics Report" export (the standard going forward,
2026-06) and its older sibling. Both are a flat single-sheet form repeating per
unit. Each unit block:
  1. Header fields (Inspection ID, Inspector, Inspected Date, Property Code,
     Unit Code, Unit Address, Tenant Name)
  2. Findings checklist starting at 'Annual Maintenace Unit Inspections'
     with value columns: Results | Observation | Work Order | Charge |
     Responsibility | Detail Level Notes | Photo
  3. Footer (Overall Result, Overall Notes, Tenant Response, Signatures)

Alignment quirk: on some blocks (typically vacant/cancelled units) Yardi's
merged-cell rendering lands every VALUE one row ABOVE its label. We detect the
offset per block by type-checking known header fields against both alignments.

Photos are embedded in xl/media and positioned via xl/drawings/drawing1.xml.
We zip-extract images ourselves to retain anchor row+column info, since
openpyxl's read-side image support drops the binary data. Item photos anchor
exactly at the row holding that item's values, so matching is exact.
"""
from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import openpyxl

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

PHOTO_ROW_TOLERANCE = 0  # items are 1 row apart and anchors sit exactly on the value row — exact only
PHOTOS_PER_FINDING_MAX = 6  # cap per-finding grid for predictable page layout
MAX_SUPPLEMENTAL_PHOTOS = 6

log = logging.getLogger(__name__)

NS = {
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a":   "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r":   "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

UNIT_MARKER = "Inspection ID"
FINDINGS_MARKERS = (
    "Annual Maintenace Unit Inspections",    # Analytics export (Yardi's typo, verbatim)
    "Annual Unit Inspection - Maintenance",  # older export variant (e.g. prop 3915)
)
END_OF_FINDINGS = "Overall Result"
SIGNATURES_MARKER = "Signatures"
ACTION_VALUES: set[str] = {"Repair", "Replace", "Inspect", "Monitor", "Clean"}
RESULT_VALUES: set[str] = {"Yes", "No", "Cancel"}
HEADER_LABELS = {
    "Inspector", "Inspected Date", "Entity Type", "Tenant Name",
    "Property Code", "Unit Code", "Unit Address",
    "Overall Result", "Overall Notes", "Tenant Response",
}


def parse_xlsx(path: Path, media_out_dir: Path) -> Property:
    media_out_dir.mkdir(parents=True, exist_ok=True)
    photos_by_row = _extract_embedded_photos(path, media_out_dir)

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = [[c.value for c in row] for row in ws.iter_rows()]

    boundaries = _find_unit_boundaries(rows)
    units: list[Unit] = []
    for start, end in zip(boundaries, boundaries[1:] + [len(rows)]):
        unit = _parse_unit_block(rows, start, end, photos_by_row)
        if unit:
            units.append(unit)

    return _build_property(units, path)


# ---------- photo extraction --------------------------------------------------

def _extract_embedded_photos(xlsx_path: Path, out_dir: Path) -> dict[int, list[Photo]]:
    """Return a mapping of zero-indexed row -> list of Photo objects anchored at that row.

    Dedupes by file-content hash: if the same image is anchored at multiple rows
    (a Yardi quirk), we keep only the first anchor we see, so the report
    doesn't show the same photo twice.
    """
    by_row: dict[int, list[Photo]] = {}
    seen_hashes: set[str] = set()

    with zipfile.ZipFile(xlsx_path) as z:
        names = set(z.namelist())
        rels_path = "xl/drawings/_rels/drawing1.xml.rels"
        drawing_path = "xl/drawings/drawing1.xml"
        if rels_path not in names or drawing_path not in names:
            log.warning("No drawing relationships found in %s", xlsx_path.name)
            return by_row

        rel_map: dict[str, str] = {}
        for rel in ET.fromstring(z.read(rels_path)).findall(f"{{{NS['rel']}}}Relationship"):
            rel_map[rel.get("Id")] = rel.get("Target")

        drawing = ET.fromstring(z.read(drawing_path))
        for anchor_tag in ("oneCellAnchor", "twoCellAnchor", "absoluteAnchor"):
            for anchor in drawing.findall(f"{{{NS['xdr']}}}{anchor_tag}"):
                frm = anchor.find(f"{{{NS['xdr']}}}from")
                if frm is None:
                    continue
                row_el = frm.find(f"{{{NS['xdr']}}}row")
                if row_el is None:
                    continue
                anchor_row = int(row_el.text)
                col_el = frm.find(f"{{{NS['xdr']}}}col")
                anchor_col = int(col_el.text) if col_el is not None else None

                blip = anchor.find(f".//{{{NS['a']}}}blip")
                if blip is None:
                    continue
                embed = blip.get(f"{{{NS['r']}}}embed")
                target = rel_map.get(embed)
                if not target:
                    continue
                media_name = target.split("/")[-1]
                zip_media_path = f"xl/media/{media_name}"
                if zip_media_path not in names:
                    continue

                img_bytes = z.read(zip_media_path)
                digest = hashlib.md5(img_bytes).hexdigest()
                if digest in seen_hashes:
                    continue  # duplicate image somewhere else in the workbook
                seen_hashes.add(digest)

                out_path = out_dir / media_name
                if not out_path.exists():
                    out_path.write_bytes(img_bytes)
                by_row.setdefault(anchor_row, []).append(
                    Photo(path=out_path, anchor_row=anchor_row, anchor_col=anchor_col)
                )

    log.info(
        "Extracted %d unique photos across %d anchor rows",
        sum(len(v) for v in by_row.values()),
        len(by_row),
    )
    return by_row


# ---------- block detection ---------------------------------------------------

def _find_unit_boundaries(rows: list[list]) -> list[int]:
    return [i for i, row in enumerate(rows) if _row_starts_with(row, UNIT_MARKER)]


def _row_starts_with(row: list, label: str) -> bool:
    for c in row[:3]:
        if isinstance(c, str) and c.strip() == label:
            return True
    return False


def _find_label_row(rows: list[list], start: int, end: int, label: str) -> int | None:
    for ri in range(start, min(end, len(rows))):
        for c in rows[ri][:3]:
            if isinstance(c, str) and c.strip() == label:
                return ri
    return None


# ---------- value/label alignment ---------------------------------------------

def _detect_value_offset(rows: list[list], start: int, end: int) -> int:
    """Return 0 if values share the row with their label, -1 if they sit one
    row above (Yardi merged-cell quirk on vacant/cancelled units). Scores both
    alignments by type-checking known header fields."""
    checks = (
        ("Inspection ID", lambda v: v.replace(".0", "").isdigit()),
        ("Inspected Date", lambda v: parse_date(v) is not None),
        ("Entity Type", lambda v: v.strip().lower() == "unit"),
    )

    def score(voff: int) -> int:
        s = 0
        for label, ok in checks:
            lrow = _find_label_row(rows, start, end, label)
            if lrow is None:
                continue
            vrow = lrow + voff
            if vrow < 0 or vrow >= len(rows):
                continue
            val = _next_non_empty(rows[vrow], 1)
            if val and ok(val):
                s += 1
        return s

    return -1 if score(-1) > score(0) else 0


def _value_for_label(rows: list[list], start: int, end: int, label: str, voff: int) -> str:
    lrow = _find_label_row(rows, start, end, label)
    if lrow is None:
        return ""
    vrow = lrow + voff
    if vrow < 0 or vrow >= len(rows):
        return ""
    return _next_non_empty(rows[vrow], 1)


# ---------- per-unit parsing --------------------------------------------------

def _parse_unit_block(
    rows: list[list],
    start: int,
    end: int,
    photos_by_row: dict[int, list[Photo]],
) -> Unit | None:
    voff = _detect_value_offset(rows, start, end)
    header = _extract_header_fields(rows, start, end, voff)
    unit_code = (header.get("Unit Code") or "").strip()
    if not unit_code:
        return None

    findings, finding_value_rows, work_order_id, n_answered = _extract_findings(rows, start, end, voff)

    overall_result = _value_for_label(rows, start, end, "Overall Result", voff)
    overall_note = _value_for_label(rows, start, end, "Overall Notes", voff)

    sig_label_row = _find_label_row(rows, start, end, SIGNATURES_MARKER)
    all_photos = _collect_unit_photos(rows, start, end, photos_by_row, sig_label_row, voff)
    non_sig = [p for p in all_photos if not p.is_signature]

    supplemental = _assign_photos_to_findings(findings, finding_value_rows, non_sig)

    # Status: vacant/cancelled units must not read as "No Action Required".
    has_action = any(f.action for f in findings)
    if n_answered == 0 or "cancel" in overall_result.lower():
        status: Status = "Not Inspected"
    elif has_action:
        status = "Action Required"
    else:
        status = "No Action Required"

    return Unit(
        number=unit_code,
        inspector=(header.get("Inspector") or "").strip(),
        tenant=(header.get("Tenant Name") or "").strip(),
        address=(header.get("Unit Address") or "").strip(),
        inspected_at=parse_date(header.get("Inspected Date")),
        work_order_id=work_order_id,
        status=status,
        overall_result=overall_result.strip(),
        overall_note=overall_note.strip(),
        findings=findings,
        photos=supplemental,
    )


def _assign_photos_to_findings(
    findings: list[Finding],
    finding_value_rows: list[int],
    photos: list[Photo],
) -> list[Photo]:
    """Assign each photo to the actionable finding whose value row matches its
    anchor row. Checklist items are one row apart in the Analytics format, so
    only exact / ±PHOTO_ROW_TOLERANCE matches count. Leftovers (photos on
    passed items, mostly) become the unit's small supplemental gallery.
    """
    actionable = [(f, vrow) for f, vrow in zip(findings, finding_value_rows) if f.action]
    if not actionable:
        return [p for p in photos if p.anchor_row is not None][:MAX_SUPPLEMENTAL_PHOTOS]

    leftover: list[Photo] = []
    for photo in photos:
        if photo.anchor_row is None:
            leftover.append(photo)
            continue
        best_finding: Finding | None = None
        best_dist = PHOTO_ROW_TOLERANCE + 1
        for f, vrow in actionable:
            if len(f.photos) >= PHOTOS_PER_FINDING_MAX:
                continue
            dist = abs(photo.anchor_row - vrow)
            if dist < best_dist:
                best_dist = dist
                best_finding = f
        if best_finding is not None:
            best_finding.photos.append(photo)
        else:
            leftover.append(photo)
    return leftover[:MAX_SUPPLEMENTAL_PHOTOS]


def _extract_header_fields(rows: list[list], start: int, end: int, voff: int) -> dict[str, str]:
    """Pull label/value pairs out of the header rows, honoring the block's
    value-row offset.

    Yardi cells often combine multiple labels in a single cell separated by \\n,
    e.g. 'Property Code\\nUnit Code' with value '4820    \\n310-07  '. We split
    both sides on \\n and pair them by position so 'Unit Code' resolves to '310-07'.
    """
    out: dict[str, str] = {}
    for ri in range(start, min(end, len(rows))):
        for cell in rows[ri][:3]:
            if not isinstance(cell, str):
                continue
            label_text = cell.strip()
            if not label_text:
                continue
            label_parts = [s.strip() for s in label_text.split("\n") if s.strip()]
            if not any(lp in HEADER_LABELS for lp in label_parts):
                continue
            vrow = ri + voff
            if vrow < 0 or vrow >= len(rows):
                continue
            value_cell = _next_non_empty(rows[vrow], 1)
            if not value_cell:
                continue
            value_parts = [s.strip() for s in value_cell.split("\n") if s.strip()]
            for j, lp in enumerate(label_parts):
                if lp in HEADER_LABELS and lp not in out and j < len(value_parts):
                    out[lp] = value_parts[j]

    if voff == -1:
        _fix_shifted_unit_address(rows, start, end, out)
    return out


def _fix_shifted_unit_address(rows: list[list], start: int, end: int, out: dict[str, str]) -> None:
    """In shifted blocks the real unit address gets merged into the column-header
    row as 'address\\nResults' on the 'Unit Address' label row itself. Prefer it
    over the offset-extracted value (which holds a property-level address)."""
    lrow = _find_label_row(rows, start, end, "Unit Address")
    if lrow is None:
        return
    same_row_val = _next_non_empty(rows[lrow], 1)
    if same_row_val and "\n" in same_row_val:
        lines = [ln.strip() for ln in same_row_val.split("\n")]
        if lines[-1] == "Results" and lines[0]:
            out["Unit Address"] = lines[0]


def _next_non_empty(row: list, start: int) -> str:
    for v in row[start:]:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


_TIMESTAMP_RX = re.compile(r"\[[^\]]*\d{1,2}:\d{2}[^\]]*\]")


def _strip_timestamp_prefix(text: str) -> str:
    """Strip Yardi's '[Inspector M/D/YYYY HH:MM?PM]' annotation, which can sit at
    the start ('[ts]\\nnote') OR inline ('context [ts]\\nnote'). After removing
    the bracketed block, return the longest non-empty line — that's the note."""
    if not text:
        return text
    cleaned = _TIMESTAMP_RX.sub("", text)
    parts = [p.strip() for p in cleaned.split("\n") if p.strip()]
    return max(parts, key=len) if parts else ""


def _extract_findings(
    rows: list[list],
    start: int,
    end: int,
    voff: int,
) -> tuple[list[Finding], list[int], str | None, int]:
    """Return (findings, absolute_value_row_per_finding, first_work_order_id,
    n_answered). The checklist question lives in column A of its own row; the
    item's values (result/action/notes/photos) live on that row + voff."""
    findings: list[Finding] = []
    finding_value_rows: list[int] = []
    work_order_id: str | None = None
    n_answered = 0

    table_start = None
    for marker in FINDINGS_MARKERS:
        table_start = _find_label_row(rows, start, end, marker)
        if table_start is not None:
            break
    if table_start is None:
        # generic fallback: Yardi names this section "Annual ... Inspection ..."
        # with wording that drifts between export versions
        for ri in range(start, min(end, len(rows))):
            c = rows[ri][0] if rows[ri] else None
            if isinstance(c, str):
                t = c.strip()
                if t.startswith("Annual") and "Inspection" in t:
                    table_start = ri
                    break
    table_end = _find_label_row(rows, start, end, END_OF_FINDINGS)
    if table_start is None:
        log.warning("No findings-table marker found in block at row %d", start + 1)
        return findings, finding_value_rows, work_order_id, n_answered
    if table_end is None:
        table_end = min(end, len(rows))

    for ri in range(table_start + 1, table_end):
        observation = _question_text(rows[ri])
        if not observation:
            continue
        vrow = ri + voff
        if vrow < 0 or vrow >= len(rows):
            continue
        values = rows[vrow][1:]  # col A of the value row may hold a neighboring question

        result = _find_result(values)
        if result in ("Yes", "No"):
            n_answered += 1

        action = _find_action(values)
        tech_note = _find_tech_note(values)
        if not action and not tech_note:
            continue  # passed item with no notes — not interesting to render

        wo = _find_work_order(values)
        if wo and not work_order_id:
            work_order_id = wo

        findings.append(
            Finding(
                category=categorize(observation),
                observation=observation,
                action=action,
                tech_note=tech_note,
            )
        )
        finding_value_rows.append(vrow)
    return findings, finding_value_rows, work_order_id, n_answered


def _question_text(row: list) -> str:
    """The checklist question sits in column A, indented, reasonably long."""
    c = row[0] if row else None
    if not isinstance(c, str):
        return ""
    t = c.strip()
    if len(t) > 20 and not t.startswith("["):
        return t
    return ""


def _find_result(values: list) -> str | None:
    for c in values:
        if isinstance(c, str) and c.strip() in RESULT_VALUES:
            return c.strip()
    return None


def _find_action(values: list) -> Action | None:
    for c in values:
        if isinstance(c, str) and c.strip() in ACTION_VALUES:
            return c.strip()  # type: ignore[return-value]
    return None


def _find_tech_note(values: list) -> str:
    """Return the technician's free-text note, stripping the [timestamp] prefix."""
    skip = RESULT_VALUES | ACTION_VALUES
    for c in values:
        if not isinstance(c, str):
            continue
        if c.strip() in skip:
            continue
        t = _strip_timestamp_prefix(c.strip())
        if t and t not in skip and len(t) >= 4:
            return t
    return ""


def _find_work_order(values: list) -> str | None:
    """Look for a numeric work-order id (5+ digits)."""
    for c in values:
        if isinstance(c, int) and c >= 10000:
            return str(c)
        if isinstance(c, str):
            t = c.strip()
            if t.isdigit() and len(t) >= 5:
                return t
    return None


def _collect_unit_photos(
    rows: list[list],
    start: int,
    end: int,
    photos_by_row: dict[int, list[Photo]],
    sig_label_row: int | None,
    voff: int,
) -> list[Photo]:
    """Collect photos anchored inside this block, flagging signatures.

    Signature pads anchor at/below the Signatures row (offset-adjusted) and in
    low columns (C/H); inspection photos anchor in the Photo column (M)."""
    out: list[Photo] = []
    sig_start = sig_label_row + voff - 1 if sig_label_row is not None else None
    for absolute_row in range(start, min(end, len(rows))):
        for p in photos_by_row.get(absolute_row, []):
            is_sig = sig_start is not None and absolute_row >= sig_start
            if not is_sig and p.anchor_col is not None and p.anchor_col < 10:
                is_sig = True  # inspection photos live in the Photo column (M=12)
            out.append(Photo(
                path=p.path,
                caption=p.caption,
                is_signature=is_sig,
                anchor_row=absolute_row,
                anchor_col=p.anchor_col,
            ))
    return out


# ---------- property assembly -------------------------------------------------

_REPORT_SUFFIX_RX = re.compile(
    r"\s*[-–—]?\s*Inspection(\s+Analytics)?(\s+Reports?)?\s*$", re.IGNORECASE
)


def _dedupe_units(units: list[Unit]) -> list[Unit]:
    """A unit can appear twice when a cancelled visit was re-inspected later.
    Keep the completed record (or the latest, if both completed); the earlier
    attempt is operational noise the owner doesn't need."""
    by_number: dict[str, Unit] = {}
    order: list[str] = []
    for u in units:
        prev = by_number.get(u.number)
        if prev is None:
            by_number[u.number] = u
            order.append(u.number)
            continue
        keep, drop = _pick_unit(prev, u)
        by_number[u.number] = keep
        log.info(
            "Unit %s appears twice — keeping the %s record from %s, dropping %s/%s",
            u.number, keep.status, keep.inspected_at, drop.status, drop.inspected_at,
        )
    return [by_number[n] for n in order]


def _pick_unit(a: Unit, b: Unit) -> tuple[Unit, Unit]:
    def rank(u: Unit):
        from datetime import date
        return (u.status != "Not Inspected", u.inspected_at or date.min)
    return (a, b) if rank(a) >= rank(b) else (b, a)


def _build_property(units: list[Unit], path: Path) -> Property:
    units = _dedupe_units(units)
    if not units:
        return Property(name=path.stem, address="", inspector="", kpis=KPIs())
    first = units[0]
    inspected_dates = [u.inspected_at for u in units if u.inspected_at]
    return Property(
        name=_property_name(path),
        address=_property_address(first.address, first.number),
        inspector=first.inspector,
        report_date=max(inspected_dates) if inspected_dates else None,
        kpis=compute_kpis(units),
        common_issues=common_issues(units),
        units=units,
    )


def _property_name(path: Path) -> str:
    """'Prop 4820 -1400-1410 Alderpath Ave Inspection Analytics Report' ->
    'Prop 4820 - 1400-1410 Alderpath Ave'."""
    name = _REPORT_SUFFIX_RX.sub("", path.stem).strip()
    name = re.sub(r"\s+-(?=\S)", " - ", name)  # ' -1400' -> ' - 1400'
    return name or path.stem


# Unit-designator label words that belong to a single unit, not the building.
# Once the unit number is stripped these would otherwise dangle, e.g.
# '620 Larkmoor Ave Apt 01 ...' -> '620 Larkmoor Ave Apt ...'. "st"/"st." stays
# out so street names ("1st St.") survive.
_UNIT_LABEL_TOKENS = {"apt", "apt.", "apartment", "unit", "ste", "ste.", "suite", "#"}


def _property_address(unit_address: str, unit_number: str = "") -> str:
    """Strip unit-specific bits from an address so it represents the building.

    e.g. '1400 Alderpath Ave. 04 Torrance, CA - 90503 us' (unit 1400-04)
      -> '1400 Alderpath Ave. Torrance, CA 90503'
    and  '820 Wrenfield Ave Apt 01 Torrance, CA 90504 us' (unit 01)
      -> '820 Wrenfield Ave Torrance, CA 90504'
    """
    if not unit_address:
        return ""
    parts = unit_address.replace("\n", " ").split()
    if parts and parts[-1].lower() == "us":
        parts = parts[:-1]

    # Remove the standalone unit-number token ('07' for unit '310-07', '#03', ...)
    suffix = unit_number.split("-")[-1].strip() if unit_number else ""
    cleaned = []
    for tok in parts:
        if tok.startswith("#"):
            continue
        if tok.lower() in _UNIT_LABEL_TOKENS:
            continue
        if suffix and tok in (suffix, suffix.lstrip("0"), suffix.zfill(2)):
            continue
        cleaned.append(tok)
    out = " ".join(cleaned)
    out = out.replace(" - ", " ")
    return out.strip()
