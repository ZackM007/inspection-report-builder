"""Writer agent — one per unit, owns every owner-facing sentence in that unit.

The agent has two backends and the same contract either way: take a unit's
findings, hand back one short factual sentence and a priority for each, plus
the unit's closing line.

  * `claude` — one model call per finding, using the Steady Hand system prompt.
  * `rules`  — the deterministic keyword engine. Always available, no key.

The part that matters is `revise()`. When the reviewer objects, the agent gets
the objections back and writes again. The rules backend repairs the specific
defect named; the Claude backend is re-prompted with the reviewer's notes.
"""
from __future__ import annotations

import logging
import re

from ..pipeline.narrative import HIGH_KEYWORDS, MED_KEYWORDS, compose_bottom_line
from .protocol import Backend, FindingBrief, Issue, Sentence, WriterResult, WriterTask

log = logging.getLogger(__name__)

# Clauses that carry a recommendation. Cut at the clause, keep the observation.
_CUT_CLAUSE = re.compile(
    r"\s*(?:,|;|and|but)?\s*(?:it |this |which |that )?"
    r"(?:should|must|needs? to be|warrants?|is advised|are advised|we advise|we recommend|is recommended)\b.*$",
    re.I,
)
_CUT_WORK_STARTED = re.compile(
    r"\s*(?:,|;|and|but)?\s*(?:it |this |work )?"
    r"(?:is being|are being|is scheduled|are scheduled|has been scheduled|have been scheduled|is underway|is in progress)\b.*$",
    re.I,
)
_FILLER_STRIP = re.compile(
    r"\b(?:kind of|sort of|looks like|at this time|please note,?|we'll make sure to|we will make sure to)\b\s*",
    re.I,
)
_NUMBER = re.compile(r"\b\d[\d,.]*\b")
_COSMETIC = re.compile(r"\b(?:paint|scuff|cosmetic|bulb|battery|touch-?up)\b", re.I)
_MISSING_ALARM = re.compile(r"\b(?:missing|no |absent|non-?functional|not working|inoperative)\b", re.I)


def _classify(brief: FindingBrief) -> str:
    """Priority from the technician's note only — the observation is checklist
    boilerplate and scanning it produces false Highs on passed items."""
    text = (brief.tech_note or "").lower()
    if not brief.action:
        return "Low"
    if any(k in text for k in HIGH_KEYWORDS) or brief.category == "Smoke / CO Alarms":
        return "High"
    if any(k in text for k in MED_KEYWORDS) or brief.action == "Replace":
        return "Medium"
    return "Low"


def _sentence(brief: FindingBrief) -> str:
    note = (brief.tech_note or "").strip().rstrip(".")
    if not note:
        return f"{brief.category.lower().capitalize()} flagged during inspection."
    body = note[0].upper() + note[1:]
    return body if body.endswith(".") else body + "."


def _bottom_line(unit_number: str, sentences: list[Sentence]) -> str:
    actionable = [s for s in sentences if s.priority in {"High", "Medium", "Low"}]
    high = sum(1 for s in actionable if s.priority == "High")
    return compose_bottom_line(unit_number, len(actionable), high)


class Writer:
    """One writer agent. The orchestrator makes one per unit."""

    def __init__(self, use_claude: bool = False) -> None:
        self.use_claude = use_claude

    @property
    def backend(self) -> Backend:
        return "claude" if self.use_claude else "rules"

    # ---- first draft ----------------------------------------------------

    def write(self, task: WriterTask) -> WriterResult:
        sentences: list[Sentence] = []
        backend: Backend = "rules"
        for brief in task.findings:
            drafted = self._claude_sentence(brief) if self.use_claude else None
            if drafted is not None:
                sentences.append(drafted)
                backend = "claude"
            else:
                sentences.append(Sentence(brief.index, _classify(brief), _sentence(brief)))
        bottom = ""
        if task.needs_bottom_line:
            bottom = self._claude_bottom_line(task, sentences) if self.use_claude else ""
            if not bottom:
                bottom = _bottom_line(task.unit_number, sentences)
        return WriterResult(task.unit_number, sentences, bottom, backend=backend)

    # ---- second pass, after the reviewer objects -------------------------

    def revise(self, task: WriterTask, result: WriterResult, issues: list[Issue]) -> WriterResult:
        """Rewrite only what the reviewer objected to. Untouched sentences stay
        byte-identical, so a revision can never make a clean sentence worse."""
        by_index: dict[int, list[Issue]] = {}
        for issue in issues:
            by_index.setdefault(issue.index, []).append(issue)
        briefs = {b.index: b for b in task.findings}

        if self.use_claude:
            revised = self._claude_revise(task, result, by_index)
            if revised is not None:
                return revised

        out: list[Sentence] = []
        for s in result.sentences:
            problems = by_index.get(s.index)
            if not problems:
                out.append(s)
                continue
            brief = briefs.get(s.index)
            out.append(_repair(brief, s, problems) if brief else s)

        written = {s.index for s in out}
        for brief in task.findings:
            if brief.index not in written:
                out.append(Sentence(brief.index, _classify(brief), _sentence(brief)))
        out.sort(key=lambda s: s.index)

        bottom = result.bottom_line
        if any(i.is_bottom_line for i in issues):
            bottom = _bottom_line(task.unit_number, out)
        return WriterResult(task.unit_number, out, bottom, backend=result.backend)

    # ---- Claude backend --------------------------------------------------

    def _claude_sentence(self, brief: FindingBrief) -> Sentence | None:
        from . import llm_agents

        try:
            return llm_agents.write_sentence(brief)
        except Exception as exc:  # noqa: BLE001 - never fail a build on an API hiccup
            log.warning("Writer fell back to rules for finding %s (%s)", brief.index, exc)
            return None

    def _claude_bottom_line(self, task: WriterTask, sentences: list[Sentence]) -> str:
        from . import llm_agents

        try:
            return llm_agents.write_bottom_line(task, sentences) or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("Bottom line fell back to rules for unit %s (%s)", task.unit_number, exc)
            return ""

    def _claude_revise(self, task: WriterTask, result: WriterResult,
                       by_index: dict[int, list[Issue]]) -> WriterResult | None:
        from . import llm_agents

        try:
            return llm_agents.revise_unit(task, result, by_index)
        except Exception as exc:  # noqa: BLE001
            log.warning("Claude revision unavailable for unit %s (%s); repairing by rule", task.unit_number, exc)
            return None


def _repair(brief: FindingBrief, sentence: Sentence, issues: list[Issue]) -> Sentence:
    """Deterministic repair, one rule at a time. This is the floor: whatever
    else fails, a sentence leaves here clean or it falls back to the note."""
    text = sentence.narrative
    priority = sentence.priority
    rules = {i.rule for i in issues}

    if "empty" in rules or "stub" in rules or "missing" in rules:
        text = _sentence(brief)

    if "facts-only" in rules:
        text = _CUT_CLAUSE.sub("", text)
    if "truthfulness" in rules:
        text = _CUT_WORK_STARTED.sub("", text)
    if "voice" in rules:
        text = _FILLER_STRIP.sub("", text)
        if brief.action:
            text = re.sub(rf"^{brief.action}\b\s*", "", text, flags=re.I)
    if "grounding" in rules:
        text = _drop_ungrounded(text, brief.source_text)
    if "shape" in rules:
        first = text.split(".")[0].strip()
        if first:
            text = first + "."
        words = text.split()
        if len(words) > 40:
            text = " ".join(words[:40]).rstrip(",;") + "."

    text = _tidy(text)
    if not text or len(text.split()) < 3:
        text = ""

    # The floor. Whatever the writer produced, a sentence only leaves here if
    # the reviewer's own deterministic rules accept it. If the repaired text
    # still breaks a rule, fall back through progressively plainer sources
    # until one passes. The last candidate is grounded in the finding's own
    # category, so it can never be dirty and can never invent a fact.
    if not text or _dirty(brief, text):
        text = _safe_text(brief)

    if "priority" in rules:
        src = brief.source_text
        if brief.category.lower() == "smoke / co alarms" and _MISSING_ALARM.search(src):
            priority = "High"
        elif brief.action == "Replace" and not _COSMETIC.search(src):
            priority = "Medium"
        elif priority not in {"High", "Medium", "Low"}:
            priority = _classify(brief)

    return Sentence(brief.index, priority, text)


def _tidy(text: str) -> str:
    text = " ".join(text.split()).strip().rstrip(",;")
    if not text:
        return ""
    if not text.endswith("."):
        text += "."
    return text[0].upper() + text[1:]


def _drop_ungrounded(text: str, source: str) -> str:
    """Remove any whitespace token carrying a number the note never mentions.

    Dropping the whole token matters: "40-gallon" is not a bare number, so
    stripping digits alone would leave "-gallon" behind."""
    source_nums = set(_NUMBER.findall(source))
    kept = []
    for token in text.split():
        nums = _NUMBER.findall(token)
        if nums and any(n not in source_nums for n in nums):
            continue
        kept.append(token)
    return " ".join(kept)


def _dirty(brief: FindingBrief, text: str) -> bool:
    """True when the reviewer would still object to the sentence itself.
    Priority objections are excluded — they are handled separately and say
    nothing about the wording."""
    from .reviewer import review_sentence

    return any(i.rule != "priority" for i in review_sentence(brief, "Medium", text))


def _safe_text(brief: FindingBrief) -> str:
    """Progressively plainer candidates, first clean one wins.

    Needed because a technician note can itself contain banned phrasing
    ("should be replaced"). Cutting the offending clause out of such a note can
    leave nothing usable, so the writer needs somewhere else to go."""
    candidates = [
        _tidy(_scrub(_sentence(brief))),
        _tidy(_scrub(brief.observation)),
        f"The inspection recorded a {brief.category.lower()} item in this unit.",
    ]
    for c in candidates:
        if c and len(c.split()) >= 3 and not _dirty(brief, c):
            return c
    return f"The inspection recorded a {brief.category.lower()} item in this unit."


def _scrub(text: str) -> str:
    text = _CUT_CLAUSE.sub("", text or "")
    text = _CUT_WORK_STARTED.sub("", text)
    text = _FILLER_STRIP.sub("", text)
    return text.split(".")[0]
