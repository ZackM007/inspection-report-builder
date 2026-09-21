"""Reviewer agent — reads every sentence before the owner ever could.

The QA gates in `inspection-report check` run on the finished PDF. By then a
bad sentence has already been written, rendered, and paginated, and the only
signal is a red FAIL with no way to fix it but a full rebuild. The reviewer
applies the same rules one sentence at a time, while the writer is still in
the loop and can be asked to try again.

Two backends:
  * `rules` — always on, no API key. Deterministic checks, the same ones the
    QA gates enforce plus grounding and shape checks the gates cannot do
    because they only see flattened PDF text.
  * `claude` — layered on top when ANTHROPIC_API_KEY is set. A second model
    call with its own prompt, reading the writer's sentence against the
    source note. It can only ADD objections; it can never clear one the
    deterministic pass raised.
"""
from __future__ import annotations

import logging
import re

from .protocol import BOTTOM_LINE, Backend, FindingBrief, Issue, ReviewResult, WriterResult

log = logging.getLogger(__name__)

# Mirrors cli.py gate 6. Kept here as the first line of defense.
RECOMMENDATION = [
    (r"\bshould\b", "recommends a fix"),
    (r"\brecommend(?:s|ed|ing)?\b", "recommends a fix"),
    (r"\bneeds to be\b", "recommends a fix"),
    (r"\b(?:we advise|is advised|are advised)\b", "recommends a fix"),
    (r"\bwarrants?\b", "recommends a fix"),
    (r"\bmust be\b", "recommends a fix"),
]

# Mirrors cli.py gate 5.
WORK_STARTED = [
    (r"\b(?:is|are) being (?:installed|replaced|repaired|handled|addressed|scheduled|done)\b", "implies work started"),
    (r"\b(?:is|are) scheduled\b", "implies work started"),
    (r"\bbeing scheduled\b", "implies work started"),
    (r"\bin progress\b", "implies work started"),
    (r"\bunderway\b", "implies work started"),
    (r"\bwe are (?:installing|replacing|repairing|handling|scheduling)\b", "implies work started"),
    (r"\b(?:has|have) been (?:scheduled|installed|replaced|handled|addressed)\b", "implies work started"),
    (r"\b(?:has|have) scheduled\b", "implies work started"),
]

# Voice rules the PDF gates cannot express.
FILLER = [
    (r"\bkind of\b", "casual phrasing"),
    (r"\bsort of\b", "casual phrasing"),
    (r"\bpretty (?:bad|good|worn|rough)\b", "casual phrasing"),
    (r"\blooks like\b", "hedged phrasing"),
    (r"\bwe(?:'ll| will) make sure\b", "filler"),
    (r"\bat this time\b", "filler"),
    (r"\bplease note\b", "filler"),
    (r"\brepair required\b", "checklist tone"),
    (r"^\s*(?:item|issue)s? (?:flagged|noted)\.?\s*$", "checklist tone"),
]

STUB = re.compile(r"flagged during inspection\.", re.I)
MAX_WORDS = 40
NUMBER = re.compile(r"\b\d[\d,.]*\b")
# Numbers that describe the report itself, not the finding.
GENERIC_NUMBERS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "one", "two"}

SMOKE_CATEGORY = "smoke / co alarms"
MISSING_ALARM = re.compile(r"\b(?:missing|no |absent|non-?functional|not working|inoperative)\b", re.I)
COSMETIC = re.compile(r"\b(?:paint|scuff|cosmetic|bulb|battery|touch-?up)\b", re.I)


def _scan(text: str, patterns: list[tuple[str, str]]) -> list[tuple[str, str]]:
    low = " ".join(text.lower().split())
    hits = []
    for pat, label in patterns:
        m = re.search(pat, low)
        if m:
            hits.append((m.group(0), label))
    return hits


def _ungrounded_numbers(narrative: str, source: str) -> list[str]:
    """Numbers in the sentence that appear nowhere in the technician's note.

    A model that writes "the 40-gallon heater" when the note never said 40 has
    invented a fact about someone's property. That is the failure mode this
    catches."""
    source_nums = set(NUMBER.findall(source))
    out = []
    for n in NUMBER.findall(narrative):
        if n in source_nums or n.lower() in GENERIC_NUMBERS:
            continue
        out.append(n)
    return out


def review_sentence(brief: FindingBrief, priority: str, narrative: str) -> list[Issue]:
    """Every deterministic rule, applied to one finding's sentence."""
    issues: list[Issue] = []
    add = lambda rule, detail: issues.append(Issue(brief.index, rule, detail))  # noqa: E731

    text = (narrative or "").strip()
    if not text:
        add("empty", "no sentence was written")
        return issues

    if STUB.search(text):
        add("stub", "placeholder text reached the reviewer")

    for hit, label in _scan(text, RECOMMENDATION):
        add("facts-only", f"{label}: {hit!r}")
    for hit, label in _scan(text, WORK_STARTED):
        add("truthfulness", f"{label}: {hit!r}")
    for hit, label in _scan(text, FILLER):
        add("voice", f"{label}: {hit!r}")

    words = len(text.split())
    if words > MAX_WORDS:
        add("shape", f"{words} words; the voice is one short sentence")
    if text.count(".") > 1 or ";" in text and text.count(";") > 1:
        add("shape", "more than one idea in the sentence")
    if not text.endswith("."):
        add("shape", "sentence does not end in a period")
    if text[0].islower():
        add("shape", "sentence does not start with a capital")

    invented = _ungrounded_numbers(text, brief.source_text)
    if invented:
        add("grounding", f"number(s) not in the source note: {', '.join(invented)}")

    if brief.action and re.match(rf"^{brief.action}\b", text, re.I):
        add("voice", f"restates the Yardi action word {brief.action!r}; describe the condition, not the fix")

    issues.extend(_review_priority(brief, priority))
    return issues


def _review_priority(brief: FindingBrief, priority: str) -> list[Issue]:
    if priority not in {"High", "Medium", "Low"}:
        return [Issue(brief.index, "priority", f"{priority!r} is not High, Medium or Low")]
    src = brief.source_text
    if brief.category.lower() == SMOKE_CATEGORY and MISSING_ALARM.search(src) and priority != "High":
        return [Issue(brief.index, "priority", "a missing or non-working alarm is High, not " + priority)]
    if brief.action == "Replace" and priority == "Low" and not COSMETIC.search(src):
        return [Issue(brief.index, "priority", "a non-cosmetic Replace is at least Medium")]
    return []


def review_bottom_line(unit_number: str, text: str) -> list[Issue]:
    issues: list[Issue] = []
    body = (text or "").strip()
    if not body:
        return issues  # a unit with nothing actionable legitimately has none
    for hit, label in _scan(body, RECOMMENDATION):
        issues.append(Issue(BOTTOM_LINE, "facts-only", f"{label}: {hit!r}"))
    for hit, label in _scan(body, WORK_STARTED):
        issues.append(Issue(BOTTOM_LINE, "truthfulness", f"{label}: {hit!r}"))
    for hit, label in _scan(body, FILLER):
        issues.append(Issue(BOTTOM_LINE, "voice", f"{label}: {hit!r}"))
    if len(body.split()) > MAX_WORDS + 10:
        issues.append(Issue(BOTTOM_LINE, "shape", "bottom line runs long; one sentence"))
    return issues


class Reviewer:
    """Checks one unit's worth of written sentences against the voice rules."""

    def __init__(self, use_claude: bool = False) -> None:
        self.use_claude = use_claude

    @property
    def backend(self) -> Backend:
        return "claude" if self.use_claude else "rules"

    def review(self, briefs: list[FindingBrief], result: WriterResult) -> ReviewResult:
        by_index = {b.index: b for b in briefs}
        issues: list[Issue] = []
        for s in result.sentences:
            brief = by_index.get(s.index)
            if brief is None:
                issues.append(Issue(s.index, "orphan", "sentence written for a finding that is not in this unit"))
                continue
            issues.extend(review_sentence(brief, s.priority, s.narrative))
        written = {s.index for s in result.sentences}
        for b in briefs:
            if b.index not in written:
                issues.append(Issue(b.index, "missing", "no sentence written for this finding"))
        issues.extend(review_bottom_line(result.unit_number, result.bottom_line))

        if self.use_claude and not issues:
            issues.extend(self._claude_pass(by_index, result))

        return ReviewResult(result.unit_number, issues, backend=self.backend)

    def _claude_pass(self, by_index: dict[int, FindingBrief], result: WriterResult) -> list[Issue]:
        """Second opinion from the model. Additive only — it never clears a
        deterministic objection, so a reviewer outage can never loosen the bar."""
        from . import llm_agents

        try:
            return llm_agents.review_unit(by_index, result)
        except Exception as exc:  # noqa: BLE001 - a reviewer outage must not fail the build
            log.warning("Unit %s: Claude reviewer unavailable (%s); deterministic review stands",
                        result.unit_number, exc)
            return []
