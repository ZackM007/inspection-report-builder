"""Steady Hand narrative engine.

Three narrative sources, in priority order:
  1. Pre-written narratives injected via `inspection-report build --narratives x.json`
     (the Claude Code skill path — no API key needed; Claude writes them
     in-session). populate() never overwrites a finding that already has one.
  2. The Claude API (via llm_narrative) when ANTHROPIC_API_KEY is configured.
  3. Keyword-based stubs as the always-available fallback.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..models import Finding, Priority, Property, Unit
from . import llm_narrative

log = logging.getLogger(__name__)

HIGH_KEYWORDS = (
    "leak", "flood", "fire", "broken", "expose", "danger", "fall",
    "carbon", "co alarm", "no smoke", "missing smoke", "shock", "mold",
)
MED_KEYWORDS = (
    "worn", "missing", "rust", "crack", "damage", "loose", "torn",
    "stained", "chip", "scratch", "deteriorat", "weather strip",
)


def classify_priority(f: Finding) -> Priority:
    # Classify on the technician's note only. The observation is the standard
    # checklist question wording ("...free of leaks", "...free from trip
    # hazards"), so scanning it produces false Highs on passed items.
    text = (f.tech_note or "").lower()
    if not f.action:
        return "Low"
    if any(k in text for k in HIGH_KEYWORDS) or f.category == "Smoke / CO Alarms":
        return "High"
    if any(k in text for k in MED_KEYWORDS) or f.action == "Replace":
        return "Medium"
    return "Low"


def narrative_for(f: Finding) -> str:
    note = (f.tech_note or "").strip().rstrip(".")
    if note:
        sentence = note[0].upper() + note[1:]
        if not sentence.endswith("."):
            sentence += "."
        return sentence
    cat = f.category.lower()
    return f"{cat.capitalize()} flagged during inspection."


def compose_bottom_line(unit_number: str, n: int, high: int) -> str:
    """The deterministic closing line for a unit.

    This is the floor the whole narrative layer falls back to, so it has to
    read in the Steady Hand voice on its own: plain counts, no checklist tone,
    no recommendation, nothing invented. The reviewer agent checks it against
    the same rules as every other sentence."""
    if n <= 0:
        return ""
    if high:
        items = "item" if n == 1 else "items"
        return f"Unit {unit_number} has {n} {items} noted from this inspection, {high} of them high priority."
    if n == 1:
        return f"Unit {unit_number} has one item noted from this inspection."
    return f"Unit {unit_number} has {n} items noted from this inspection, none of them high priority."


def bottom_line_for(unit: Unit) -> str:
    actionable = [f for f in unit.findings if f.action]
    if not actionable:
        return ""
    high = sum(1 for f in actionable if f.priority == "High")
    return compose_bottom_line(unit.number, len(actionable), high)


def populate(unit: Unit) -> Unit:
    use_llm = llm_narrative.is_available()
    llm_hits = 0
    for f in unit.findings:
        if f.narrative:
            continue  # pre-written (skill path / --narratives) — leave untouched
        if use_llm:
            result = llm_narrative.generate_finding(f)
        else:
            result = None
        if result is not None:
            f.priority = result.priority
            f.narrative = result.narrative
            llm_hits += 1
        else:
            f.priority = classify_priority(f)
            f.narrative = narrative_for(f)

    if not unit.bottom_line:
        bottom = llm_narrative.generate_bottom_line(unit) if use_llm else None
        unit.bottom_line = bottom if bottom else bottom_line_for(unit)

    if use_llm and unit.findings:
        log.info("Unit %s: %d/%d findings narrated via Claude", unit.number, llm_hits, len(unit.findings))
    return unit


# ---------- pre-written narrative injection ------------------------------------

VALID_PRIORITIES = {"High", "Medium", "Low"}


def apply_provided(prop: Property, narratives_path: Path) -> int:
    """Apply a narratives JSON (written by Claude Code in-session) onto the
    parsed property. Returns how many finding narratives were applied.

    Expected shape — finding indices refer to the parse order of the SAME
    input file the JSON was extracted from:
    {
      "units": {
        "310-07": {
          "bottom_line": "...",
          "findings": [{"index": 0, "priority": "Medium", "narrative": "..."}]
        }
      }
    }
    """
    data = json.loads(Path(narratives_path).read_text(encoding="utf-8"))
    units_map = data.get("units", {})
    applied = 0
    for u in prop.units:
        spec = units_map.get(u.number)
        if not spec:
            continue
        if spec.get("bottom_line"):
            u.bottom_line = str(spec["bottom_line"]).strip()
        omit: list[int] = []  # indices to drop (duplicates / no-detail items)
        for item in spec.get("findings", []):
            idx = item.get("index")
            if idx is None or not (0 <= idx < len(u.findings)):
                log.warning("Narratives JSON: bad finding index %r for unit %s", idx, u.number)
                continue
            if item.get("omit"):
                omit.append(idx)
                continue
            f = u.findings[idx]
            if item.get("narrative"):
                f.narrative = str(item["narrative"]).strip()
                applied += 1
            if item.get("priority") in VALID_PRIORITIES:
                f.priority = item["priority"]
        # Remove omitted findings last, descending, so earlier indices stay valid.
        for idx in sorted(set(omit), reverse=True):
            del u.findings[idx]
    return applied
