"""Shared data types passed between the orchestrator and its agents.

Everything here is a plain dataclass so a run can be serialised to JSON and
audited after the fact. The orchestrator writes the whole ledger to
`outputs/.work/<name>/agent_run.json` on every build.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Backend = Literal["claude", "rules"]
Outcome = Literal["accepted", "repaired", "escalated"]

BOTTOM_LINE = -1
"""Sentinel finding index used when an issue refers to a unit's bottom line."""


@dataclass
class FindingBrief:
    """The read-only facts a writer agent is allowed to work from."""

    index: int
    category: str
    observation: str
    action: str | None
    tech_note: str

    @property
    def source_text(self) -> str:
        """Everything the writer may ground a sentence in. Anything outside
        this string is invented detail as far as the reviewer is concerned."""
        return " ".join(p for p in (self.category, self.observation, self.action or "", self.tech_note) if p)


@dataclass
class WriterTask:
    """One unit of work. The orchestrator creates one per unit and fans out."""

    unit_number: str
    findings: list[FindingBrief]
    needs_bottom_line: bool = True
    feedback: list[str] = field(default_factory=list)  # reviewer notes from the prior attempt


@dataclass
class Sentence:
    """A single owner-facing sentence plus its priority, as written by an agent."""

    index: int
    priority: str
    narrative: str


@dataclass
class WriterResult:
    unit_number: str
    sentences: list[Sentence]
    bottom_line: str = ""
    backend: Backend = "rules"


@dataclass
class Issue:
    """One reviewer objection. `index` is a finding index, or BOTTOM_LINE."""

    index: int
    rule: str
    detail: str

    @property
    def is_bottom_line(self) -> bool:
        return self.index == BOTTOM_LINE


@dataclass
class ReviewResult:
    unit_number: str
    issues: list[Issue]
    backend: Backend = "rules"

    @property
    def clean(self) -> bool:
        return not self.issues


@dataclass
class UnitRecord:
    """The audit trail for one unit: every attempt, every objection, the outcome."""

    unit_number: str
    attempts: int = 0
    outcome: Outcome = "accepted"
    writer_backend: Backend = "rules"
    reviewer_backend: Backend = "rules"
    issues_raised: list[dict[str, Any]] = field(default_factory=list)
    issues_remaining: list[dict[str, Any]] = field(default_factory=list)
    findings_written: int = 0


@dataclass
class RunLedger:
    units: list[UnitRecord] = field(default_factory=list)
    workers: int = 1
    max_revisions: int = 2

    @property
    def sentences_written(self) -> int:
        return sum(u.findings_written for u in self.units)

    @property
    def issues_caught(self) -> int:
        return sum(len(u.issues_raised) for u in self.units)

    @property
    def repaired(self) -> int:
        return sum(1 for u in self.units if u.outcome == "repaired")

    @property
    def escalated(self) -> int:
        return sum(1 for u in self.units if u.outcome == "escalated")

    def to_dict(self) -> dict[str, Any]:
        return {
            "workers": self.workers,
            "max_revisions": self.max_revisions,
            "summary": {
                "units": len(self.units),
                "sentences_written": self.sentences_written,
                "issues_caught": self.issues_caught,
                "units_repaired": self.repaired,
                "units_escalated": self.escalated,
            },
            "units": [asdict(u) for u in self.units],
        }
