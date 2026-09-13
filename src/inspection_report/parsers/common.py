from __future__ import annotations

from datetime import date, datetime


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%B %d, %Y",
        "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# The Analytics-format checklist is a fixed set of standard questions; map them
# by prefix first so keyword collisions can't mis-bucket them (e.g. "Paint ...
# free from water damage" must not land in Plumbing via the "water" keyword).
CHECKLIST_PREFIXES = [
    ("front door", "Front Door"),
    ("carpet and flooring", "Flooring"),
    ("paint, ceiling and walls", "Paint & Walls"),
    ("kitchen cabinets and countertops", "Kitchen"),
    ("windows and screens", "Windows"),
    ("smoke alarms and co2", "Smoke / CO Alarms"),
    ("all gfci", "Electrical"),
    ("wall heater", "HVAC"),
    ("water heater", "Water Heater"),
    ("toilet", "Plumbing"),
    ("bathroom faucet", "Plumbing"),
    ("shower head", "Bathroom"),
    ("kitchen faucet", "Plumbing"),
    ("stove/ refrigerator", "Appliances"),
    ("signs of hoarding", "Housekeeping"),
]

CATEGORY_KEYWORDS = [
    ("front door", "Front Door"),
    ("smoke", "Smoke / CO Alarms"),
    ("co2", "Smoke / CO Alarms"),
    ("co ", "Smoke / CO Alarms"),
    ("stove", "Appliances"),
    ("refrigerator", "Appliances"),
    ("dishwasher", "Appliances"),
    ("microwave", "Appliances"),
    ("window", "Windows"),
    ("plumb", "Plumbing"),
    ("water heater", "Water Heater"),
    ("water", "Plumbing"),
    ("toilet", "Plumbing"),
    ("kitchen", "Kitchen"),
    ("paint", "Paint & Walls"),
    ("wall", "Paint & Walls"),
    ("floor", "Flooring"),
    ("electric", "Electrical"),
    ("outlet", "Electrical"),
    ("bath", "Bathroom"),
    ("tub", "Bathroom"),
    ("hvac", "HVAC"),
    ("heat", "HVAC"),
    ("a/c", "HVAC"),
]


def categorize(observation: str) -> str:
    obs = (observation or "").strip().lower()
    for prefix, cat in CHECKLIST_PREFIXES:
        if obs.startswith(prefix):
            return cat
    for kw, cat in CATEGORY_KEYWORDS:
        if kw in obs:
            return cat
    return "Other"
