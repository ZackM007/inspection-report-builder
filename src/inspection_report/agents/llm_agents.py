"""Claude-backed implementations of the writer and reviewer agents.

Each agent is a separate model call with its own system prompt and its own
job. The writer never sees the review rubric; the reviewer never sees the
writer's reasoning, only the finished sentence and the technician's note. That
separation is the point — a reviewer that shares the writer's context tends to
agree with it.

Every function here is optional. If no ANTHROPIC_API_KEY is set, or a call
fails, the caller falls back to the deterministic backend and the build still
finishes. The quality bar does not move.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from ..pipeline.llm_narrative import BOTTOM_LINE_SYSTEM_PROMPT, SYSTEM_PROMPT
from .protocol import BOTTOM_LINE, FindingBrief, Issue, Sentence, WriterResult, WriterTask

log = logging.getLogger(__name__)

MODEL = os.environ.get("REPORT_MODEL", "claude-opus-4-7")
MAX_TOKENS = 600

REVIEWER_SYSTEM_PROMPT = """You are the editor on an owner-facing property inspection report. A writer has turned a technician's note into one sentence for the property owner. Your job is to catch anything the owner should never see.

You are not rewriting. You are listing objections. A sentence with nothing wrong gets an empty list, and that is the common case.

Object only when one of these is true:

1. GROUNDING — the sentence states a fact that is not in the technician's note or the observation. An invented brand, room, measurement, cause, or date. This is the most serious objection.
2. FACTS ONLY — the sentence recommends, prescribes, or schedules a fix. The report states the condition; the owner decides the rest.
3. TRUTHFULNESS — the sentence implies work has started, is scheduled, or is finished. Nothing has been done yet except what the technician did on site during the inspection.
4. VOICE — casual, alarmed, salesy, or checklist phrasing. Or the sentence restates the Repair/Replace/Clean action word, which already appears as a label beside it.
5. PRIORITY — the severity does not match what was found. A missing or non-working smoke or CO alarm is High. A non-cosmetic replacement is at least Medium.

Do not object to a sentence for being short, plain, or unflattering. Plain is the house style.

Respond with JSON matching the schema: a list of objections, each with the finding index, one of the rule names above in lower case with a hyphen (grounding, facts-only, truthfulness, voice, priority), and one short sentence saying what is wrong. Use index -1 for an objection to the unit's closing bottom line."""


class Objection(BaseModel):
    index: int = Field(description="The finding index this objection is about, or -1 for the bottom line.")
    rule: Literal["grounding", "facts-only", "truthfulness", "voice", "priority"] = Field(
        description="Which rule the sentence breaks."
    )
    detail: str = Field(description="One short sentence saying what is wrong.")


class ReviewOutput(BaseModel):
    objections: list[Objection] = Field(description="Empty when the unit is clean.")


class NarrativeOutput(BaseModel):
    priority: Literal["High", "Medium", "Low"]
    narrative: str


class RevisionItem(BaseModel):
    index: int
    priority: Literal["High", "Medium", "Low"]
    narrative: str


class RevisionOutput(BaseModel):
    findings: list[RevisionItem]
    bottom_line: str = ""


class BottomLineOutput(BaseModel):
    bottom_line: str = Field(description="One calm closing sentence for the unit.")


@lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return anthropic.Anthropic()


def is_available() -> bool:
    return _client() is not None


def _brief_text(b: FindingBrief) -> str:
    parts = [f"Category: {b.category}", f"Observation: {b.observation}"]
    if b.action:
        parts.append(f"Suggested action: {b.action}")
    if b.tech_note:
        parts.append(f"Technician note: {b.tech_note}")
    return "\n".join(parts)


def _parse(system: str, user: str, schema):
    client = _client()
    if client is None:
        raise RuntimeError("no ANTHROPIC_API_KEY configured")
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    return resp.parsed_output


# ---- writer ---------------------------------------------------------------


def write_sentence(brief: FindingBrief) -> Sentence | None:
    out = _parse(SYSTEM_PROMPT, _brief_text(brief), NarrativeOutput)
    if out is None:
        return None
    return Sentence(brief.index, out.priority, out.narrative.strip())


def write_bottom_line(task: WriterTask, sentences: list[Sentence]) -> str | None:
    if not sentences:
        return None
    order = {"High": 0, "Medium": 1, "Low": 2}
    top = min(sentences, key=lambda s: order.get(s.priority, 9))
    by_index = {b.index: b for b in task.findings}
    counts = {p: sum(1 for s in sentences if s.priority == p) for p in ("High", "Medium", "Low")}
    top_cat = by_index[top.index].category if top.index in by_index else "General"
    user = (
        f"Unit: {task.unit_number}\n"
        f"Actionable findings: {len(sentences)} ({counts['High']} High, {counts['Medium']} Medium, {counts['Low']} Low)\n"
        f"Highest-priority category: {top_cat} ({top.priority})"
    )
    out = _parse(BOTTOM_LINE_SYSTEM_PROMPT, user, BottomLineOutput)
    if out is None:
        return None
    return out.bottom_line.strip() or None


def revise_unit(task: WriterTask, result: WriterResult,
                by_index: dict[int, list[Issue]]) -> WriterResult | None:
    """Hand the writer its own sentences back with the editor's objections."""
    briefs = {b.index: b for b in task.findings}
    blocks: list[str] = []
    for s in result.sentences:
        problems = by_index.get(s.index)
        if not problems:
            continue
        brief = briefs.get(s.index)
        if brief is None:
            continue
        objections = "\n".join(f"  - {i.rule}: {i.detail}" for i in problems)
        blocks.append(
            f"Finding index {s.index}\n{_brief_text(brief)}\n"
            f"Your sentence: {s.narrative}\nYour priority: {s.priority}\n"
            f"Editor's objections:\n{objections}"
        )
    bl_problems = by_index.get(BOTTOM_LINE)
    if bl_problems:
        objections = "\n".join(f"  - {i.rule}: {i.detail}" for i in bl_problems)
        blocks.append(f"Unit bottom line: {result.bottom_line}\nEditor's objections:\n{objections}")
    if not blocks:
        return None

    user = (
        "An editor rejected some of your sentences for this unit. Rewrite only the ones listed. "
        "Keep everything else as it was. Return every rewritten finding with its index, its priority, "
        "and the new sentence.\n\n" + "\n\n".join(blocks)
    )
    out = _parse(SYSTEM_PROMPT, user, RevisionOutput)
    if out is None:
        return None

    revised = {item.index: item for item in out.findings}
    sentences = [
        Sentence(s.index, revised[s.index].priority, revised[s.index].narrative.strip())
        if s.index in revised else s
        for s in result.sentences
    ]
    bottom = out.bottom_line.strip() if (bl_problems and out.bottom_line.strip()) else result.bottom_line
    return WriterResult(result.unit_number, sentences, bottom, backend="claude")


# ---- reviewer -------------------------------------------------------------


def review_unit(by_index: dict[int, FindingBrief], result: WriterResult) -> list[Issue]:
    blocks = []
    for s in result.sentences:
        brief = by_index.get(s.index)
        if brief is None:
            continue
        blocks.append(
            f"Finding index {s.index}\n{_brief_text(brief)}\n"
            f"Writer's sentence: {s.narrative}\nWriter's priority: {s.priority}"
        )
    if result.bottom_line:
        blocks.append(f"Unit bottom line (index -1): {result.bottom_line}")
    if not blocks:
        return []
    out = _parse(REVIEWER_SYSTEM_PROMPT, "\n\n".join(blocks), ReviewOutput)
    if out is None:
        return []
    return [Issue(o.index, o.rule, o.detail) for o in out.objections]
