from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

Priority = Literal["High", "Medium", "Low"]
Status = Literal["Action Required", "No Action Required", "Not Inspected"]
Action = Literal["Repair", "Replace", "Inspect", "Monitor", "Clean"]


@dataclass
class Photo:
    path: Path
    caption: str | None = None
    is_signature: bool = False
    anchor_row: int | None = None  # zero-indexed row in the source xlsx, if known
    anchor_col: int | None = None  # zero-indexed column of the anchor, if known


@dataclass
class Finding:
    category: str
    observation: str
    action: Action | None
    tech_note: str
    priority: Priority = "Low"
    narrative: str = ""
    photos: list[Photo] = field(default_factory=list)  # representative photos for this finding


@dataclass
class Unit:
    number: str
    type: str = ""
    inspected_at: date | None = None
    inspector: str = ""
    tenant: str = ""
    address: str = ""
    work_order_id: str | None = None
    status: Status = "No Action Required"
    overall_result: str = ""  # raw Yardi verdict, e.g. "Fail - Corrective action needed", "Cancel"
    overall_note: str = ""    # inspector's free-text overall note, e.g. "Vacant unit"
    findings: list[Finding] = field(default_factory=list)
    photos: list[Photo] = field(default_factory=list)  # supplemental photos (not tied to a finding)
    bottom_line: str = ""

    @property
    def has_action(self) -> bool:
        return any(f.action for f in self.findings)

    @property
    def not_inspected_reason(self) -> str:
        """Human-readable reason a unit wasn't inspected (vacant, cancelled, ...)."""
        return self.overall_note or self.overall_result or "Cancelled"


@dataclass
class KPIs:
    units_total: int = 0
    action_required: int = 0
    no_action: int = 0
    not_inspected: int = 0
    work_orders: int = 0


@dataclass
class Property:
    name: str
    address: str
    inspector: str
    cert_id: str = ""
    manager: str = ""   # property Manager (operator-supplied; not in Yardi data)
    rps: str = ""       # Regional Property Supervisor (operator-supplied)
    period: str = ""
    report_date: date | None = None
    kpis: KPIs = field(default_factory=KPIs)
    common_issues: list[tuple[str, int]] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)


def compute_kpis(units: list[Unit]) -> KPIs:
    not_inspected = sum(1 for u in units if u.status == "Not Inspected")
    action = sum(1 for u in units if u.status != "Not Inspected" and u.has_action)
    return KPIs(
        units_total=len(units),
        action_required=action,
        no_action=len(units) - action - not_inspected,
        not_inspected=not_inspected,
        work_orders=sum(1 for u in units if u.work_order_id),
    )


def common_issues(units: list[Unit], top_n: int = 7) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for u in units:
        for f in u.findings:
            if f.action:
                counts[f.category] = counts.get(f.category, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
