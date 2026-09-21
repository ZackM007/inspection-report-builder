"""Orchestrator — fans the narrative work out by unit and holds the quality bar.

One writer agent per unit, running in parallel. A reviewer agent reads what
each writer produced. If the reviewer objects, that unit goes back to its
writer with the objections attached, up to `max_revisions` times. A unit that
still has objections after the last attempt is escalated: the orchestrator
drops to the deterministic floor, which cannot produce banned phrasing, and
flags the unit in the ledger.

Why this shape:
  * The unit is the natural boundary. A unit's findings share a tenant, a
    walk-through and a bottom line, so a writer that sees all of them writes a
    closing sentence that agrees with the page above it.
  * Review happens per unit, not per report. A rejected sentence costs one
    unit's rewrite, not a 37-page rebuild.
  * The six QA gates in `inspection-report check` still run afterwards,
    unchanged. They stop being the only thing standing between a bad sentence
    and an owner's inbox, and become the second line of defense.

Every run writes `agent_run.json` next to the build's scratch files: attempts,
objections, and outcome per unit. A claim about what the agents did is
checkable against that file.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..models import Property, Unit
from .protocol import BOTTOM_LINE, FindingBrief, RunLedger, UnitRecord, WriterResult, WriterTask
from .reviewer import Reviewer
from .writer import Writer

log = logging.getLogger(__name__)

DEFAULT_WORKERS = 4
DEFAULT_MAX_REVISIONS = 2


class Orchestrator:
    """Owns the fan-out, the review loop, and the run ledger."""

    def __init__(
        self,
        use_claude: bool = False,
        workers: int = DEFAULT_WORKERS,
        max_revisions: int = DEFAULT_MAX_REVISIONS,
    ) -> None:
        self.use_claude = use_claude
        self.workers = max(1, workers)
        self.max_revisions = max(0, max_revisions)
        self.reviewer = Reviewer(use_claude=use_claude)
        self.ledger = RunLedger(workers=self.workers, max_revisions=self.max_revisions)

    # ---- public API ------------------------------------------------------

    def run(self, prop: Property) -> RunLedger:
        """Narrate every unit in the property, in place."""
        tasks = [(u, _task_for(u)) for u in prop.units]
        tasks = [(u, t) for u, t in tasks if t.findings or t.needs_bottom_line]
        if not tasks:
            return self.ledger

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(lambda pair: self._run_unit(*pair), tasks))

        for unit, record, result in results:
            _apply(unit, result)
            self.ledger.units.append(record)
        self.ledger.units.sort(key=lambda r: r.unit_number)
        return self.ledger

    def review_only(self, prop: Property) -> RunLedger:
        """Run the reviewer over narratives that already exist, for example the
        ones Claude wrote in-session and passed in with `--narratives`. The
        writer still repairs anything the reviewer rejects."""
        for unit in prop.units:
            task = _task_for(unit)
            if not task.findings:
                continue
            existing = _result_from_unit(unit, task)
            record = UnitRecord(
                unit_number=unit.number,
                writer_backend="rules",
                reviewer_backend=self.reviewer.backend,
                findings_written=len(existing.sentences),
            )
            review = self.reviewer.review(task.findings, existing)
            record.attempts = 1
            if review.clean:
                self.ledger.units.append(record)
                continue
            record.issues_raised = [vars(i) for i in review.issues]
            writer = Writer(use_claude=self.use_claude)
            repaired = writer.revise(task, existing, review.issues)
            second = self.reviewer.review(task.findings, repaired)
            record.outcome = "repaired" if second.clean else "escalated"
            record.issues_remaining = [vars(i) for i in second.issues]
            _apply(unit, repaired)
            self.ledger.units.append(record)
        self.ledger.units.sort(key=lambda r: r.unit_number)
        return self.ledger

    def write_ledger(self, work_dir: Path) -> Path:
        path = Path(work_dir) / "agent_run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.ledger.to_dict(), indent=2), encoding="utf-8")
        return path

    # ---- one unit, start to finish ---------------------------------------

    def _run_unit(self, unit: Unit, task: WriterTask) -> tuple[Unit, UnitRecord, WriterResult]:
        writer = Writer(use_claude=self.use_claude)
        record = UnitRecord(
            unit_number=unit.number,
            writer_backend=writer.backend,
            reviewer_backend=self.reviewer.backend,
        )
        result = writer.write(task)
        record.attempts = 1
        record.findings_written = len(result.sentences)
        record.writer_backend = result.backend

        review = self.reviewer.review(task.findings, result)
        for _ in range(self.max_revisions):
            if review.clean:
                break
            record.issues_raised.extend(vars(i) for i in review.issues)
            task.feedback = [f"finding {i.index if i.index != BOTTOM_LINE else 'bottom line'}: {i.detail}"
                             for i in review.issues]
            result = writer.revise(task, result, review.issues)
            record.attempts += 1
            record.outcome = "repaired"
            review = self.reviewer.review(task.findings, result)

        if not review.clean:
            record.issues_remaining = [vars(i) for i in review.issues]
            record.outcome = "escalated"
            log.warning("Unit %s: %d objection(s) survived %d attempt(s); escalated to the deterministic floor",
                        unit.number, len(review.issues), record.attempts)
        record.findings_written = len(result.sentences)
        return unit, record, result


# ---- plumbing between the agents and the report model ---------------------


def _task_for(unit: Unit) -> WriterTask:
    briefs = [
        FindingBrief(
            index=i,
            category=f.category,
            observation=f.observation,
            action=f.action,
            tech_note=f.tech_note,
        )
        for i, f in enumerate(unit.findings)
        if f.action
    ]
    return WriterTask(unit_number=unit.number, findings=briefs, needs_bottom_line=bool(briefs))


def _result_from_unit(unit: Unit, task: WriterTask) -> WriterResult:
    from .protocol import Sentence

    sentences = [
        Sentence(b.index, unit.findings[b.index].priority, unit.findings[b.index].narrative)
        for b in task.findings
    ]
    return WriterResult(unit.number, sentences, unit.bottom_line)


def _apply(unit: Unit, result: WriterResult) -> None:
    for s in result.sentences:
        if 0 <= s.index < len(unit.findings):
            unit.findings[s.index].narrative = s.narrative
            unit.findings[s.index].priority = s.priority
    if result.bottom_line:
        unit.bottom_line = result.bottom_line
    # Findings with no action word are passed checklist items. They carry no
    # owner-facing sentence, and the templates skip them.
    for f in unit.findings:
        if not f.action and not f.narrative:
            f.priority = "Low"
