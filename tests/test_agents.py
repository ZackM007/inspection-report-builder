"""The reviewer agent has to catch what the QA gates catch, one sentence
earlier, plus the things a flattened PDF cannot show. These tests feed it
sentences that break each rule and check that it objects, then check that the
writer agent's repair actually clears the objection.

No API key. Everything here runs on the deterministic backend, which is the
floor the whole layer falls back to.
"""
from __future__ import annotations

import pytest

from inspection_report.agents.orchestrator import Orchestrator
from inspection_report.agents.protocol import BOTTOM_LINE, FindingBrief, Sentence, WriterResult, WriterTask
from inspection_report.agents.reviewer import Reviewer, review_sentence
from inspection_report.agents.writer import Writer
from inspection_report.models import Finding, Property, Unit


def brief(**kw) -> FindingBrief:
    base = dict(index=0, category="Water Heater", observation="Water heater shows rust at the base",
                action="Replace", tech_note="corrosion at the burner pan, tank holding pressure")
    base.update(kw)
    return FindingBrief(**base)


def rules(issues) -> set[str]:
    return {i.rule for i in issues}


# ---- the reviewer objects to what it should -------------------------------

@pytest.mark.parametrize("sentence,rule", [
    ("The water heater should be replaced before winter.", "facts-only"),
    ("We recommend replacing the water heater.", "facts-only"),
    ("The water heater needs to be swapped out.", "facts-only"),
    ("The water heater is corroded and a replacement is scheduled.", "truthfulness"),
    ("Replacement of the water heater is underway.", "truthfulness"),
    ("The water heater is being replaced this week.", "truthfulness"),
    ("The water heater looks like it is pretty bad.", "voice"),
    ("Water heater flagged during inspection.", "stub"),
    ("the water heater is corroded at the burner pan.", "shape"),
    ("The water heater is corroded", "shape"),
])
def test_reviewer_objects(sentence, rule):
    issues = review_sentence(brief(), "Medium", sentence)
    assert rule in rules(issues), f"expected a {rule} objection to {sentence!r}"


def test_reviewer_catches_invented_numbers():
    """A model that writes a capacity the technician never measured has made up
    a fact about someone's property. This is the objection that matters most."""
    issues = review_sentence(brief(), "Medium",
                             "The 40-gallon water heater is corroded at the burner pan.")
    assert "grounding" in rules(issues)


def test_reviewer_allows_numbers_that_are_in_the_note():
    b = brief(tech_note="40-gallon tank, corrosion at the burner pan")
    issues = review_sentence(b, "Medium", "The 40-gallon tank is corroded at the burner pan.")
    assert "grounding" not in rules(issues)


def test_reviewer_objects_to_restating_the_action_word():
    issues = review_sentence(brief(), "Medium", "Replace the corroded water heater.")
    assert "voice" in rules(issues)


def test_missing_alarm_must_be_high():
    b = brief(category="Smoke / CO Alarms", observation="No smoke alarm present in hallway",
              action="Replace", tech_note="bracket present but unit missing")
    issues = review_sentence(b, "Low", "The hallway smoke alarm is missing from its bracket.")
    assert "priority" in rules(issues)


def test_non_cosmetic_replace_is_at_least_medium():
    assert "priority" in rules(review_sentence(brief(), "Low", "The water heater is corroded at the base."))


def test_cosmetic_replace_may_be_low():
    b = brief(category="Smoke / CO Alarms", observation="Smoke detector chirping",
              action="Replace", tech_note="low battery, battery replaced on site")
    issues = review_sentence(b, "High", "The smoke detector signaled a low battery during inspection.")
    assert "priority" not in rules(issues)


def test_clean_sentence_passes():
    good = "The water heater is corroded at the burner pan, though the tank is still holding pressure."
    assert review_sentence(brief(), "Medium", good) == []


def test_reviewer_flags_a_missing_sentence():
    r = Reviewer()
    result = WriterResult("07", [], "")
    assert "missing" in rules(r.review([brief()], result).issues)


def test_reviewer_reads_the_bottom_line_too():
    r = Reviewer()
    result = WriterResult("07", [Sentence(0, "Medium", "The water heater is corroded at the burner pan.")],
                          "The heater should be replaced first.")
    issues = r.review([brief()], result).issues
    assert any(i.is_bottom_line and i.rule == "facts-only" for i in issues)
    assert BOTTOM_LINE == -1


# ---- the writer repairs what the reviewer objects to ----------------------

@pytest.mark.parametrize("bad", [
    "The water heater is corroded at the burner pan and should be replaced before winter.",
    "The water heater is corroded, and a replacement is scheduled.",
    "The 40-gallon water heater is corroded at the burner pan.",
    "Replace the corroded water heater.",
    "Water heater flagged during inspection.",
])
def test_writer_repair_clears_the_objection(bad):
    b = brief()
    task = WriterTask("07", [b])
    result = WriterResult("07", [Sentence(0, "Medium", bad)], "")
    reviewer = Reviewer()
    issues = reviewer.review([b], result).issues
    assert issues, "fixture should have been rejected"

    repaired = Writer().revise(task, result, issues)
    assert reviewer.review([b], repaired).clean, repaired.sentences[0].narrative
    assert repaired.sentences[0].narrative.strip()


def test_repair_leaves_clean_sentences_byte_identical():
    """A revision must never make a good sentence worse."""
    good = FindingBrief(1, "Flooring", "Carpet stained", "Replace", "set-in stains, end of useful life")
    bad = brief(index=0)
    task = WriterTask("07", [bad, good])
    result = WriterResult("07", [
        Sentence(0, "Medium", "The water heater should be replaced."),
        Sentence(1, "Medium", "The living room carpet has multiple set-in stains."),
    ], "")
    issues = Reviewer().review([bad, good], result).issues
    repaired = Writer().revise(task, result, issues)
    kept = next(s for s in repaired.sentences if s.index == 1)
    assert kept.narrative == "The living room carpet has multiple set-in stains."


# ---- the orchestrator holds the loop together -----------------------------

def _property_with(findings: list[Finding]) -> Property:
    unit = Unit(number="07", status="Action Required", findings=findings)
    return Property(name="Prop 4820", address="", inspector="", units=[unit])


def test_orchestrator_narrates_every_actionable_finding():
    prop = _property_with([
        Finding("Water Heater", "rust at base", "Replace", "corrosion at the burner pan"),
        Finding("Flooring", "carpet stained", "Replace", "set-in stains throughout"),
        Finding("Windows", "window seals intact", None, ""),  # a passed item
    ])
    ledger = Orchestrator(workers=2).run(prop)
    unit = prop.units[0]
    assert unit.findings[0].narrative and unit.findings[1].narrative
    assert unit.bottom_line
    assert ledger.sentences_written == 2
    assert ledger.escalated == 0


def test_orchestrator_repairs_pre_written_narratives():
    """The --narratives path: Claude wrote these in-session, and they still go
    through the reviewer before anything reaches the QA gates."""
    prop = _property_with([
        Finding("Water Heater", "rust at base", "Replace", "corrosion at the burner pan",
                priority="Medium", narrative="The water heater should be replaced before winter."),
    ])
    orch = Orchestrator(workers=1)
    ledger = orch.review_only(prop)
    assert ledger.issues_caught >= 1
    assert "should" not in prop.units[0].findings[0].narrative.lower()
    assert ledger.escalated == 0


def test_orchestrator_output_survives_the_qa_gate_patterns():
    """End to end on the rule backend: nothing the orchestrator emits may trip
    the banned-phrase gates in `inspection-report check`."""
    import re

    from inspection_report.agents.reviewer import RECOMMENDATION, WORK_STARTED

    prop = _property_with([
        Finding("Water Heater", "rust at base", "Replace", "should be replaced, corrosion at the base"),
        Finding("Smoke / CO Alarms", "No smoke alarm present", "Replace", "missing, needs to be installed"),
        Finding("Plumbing", "leak under sink", "Repair", "P-trap dripping, repair is scheduled"),
    ])
    Orchestrator(workers=2).run(prop)
    text = " ".join(f.narrative for f in prop.units[0].findings) + " " + prop.units[0].bottom_line
    low = text.lower()
    for pat, _ in RECOMMENDATION + WORK_STARTED:
        assert not re.search(pat, low), f"{pat} survived into {text!r}"


def test_ledger_is_serialisable():
    prop = _property_with([Finding("Flooring", "carpet stained", "Replace", "set-in stains")])
    ledger = Orchestrator(workers=1).run(prop)
    payload = ledger.to_dict()
    assert payload["summary"]["units"] == 1
    assert payload["units"][0]["unit_number"] == "07"


def test_parallel_and_serial_agree():
    """Fan-out must not change the report. Same input, same sentences."""
    def build():
        return _property_with([
            Finding("Water Heater", "rust", "Replace", "corrosion at the burner pan"),
            Finding("Flooring", "carpet", "Replace", "set-in stains"),
        ])

    a, b = build(), build()
    Orchestrator(workers=1).run(a)
    Orchestrator(workers=8).run(b)
    assert [f.narrative for f in a.units[0].findings] == [f.narrative for f in b.units[0].findings]
    assert a.units[0].bottom_line == b.units[0].bottom_line
