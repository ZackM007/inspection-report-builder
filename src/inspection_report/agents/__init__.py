"""Multi-agent narrative layer: an orchestrator, a writer agent per unit, and a
reviewer agent that reads every sentence before the QA gates ever see it."""
from .orchestrator import DEFAULT_MAX_REVISIONS, DEFAULT_WORKERS, Orchestrator
from .protocol import RunLedger, UnitRecord
from .reviewer import Reviewer
from .writer import Writer

__all__ = [
    "Orchestrator",
    "Reviewer",
    "Writer",
    "RunLedger",
    "UnitRecord",
    "DEFAULT_WORKERS",
    "DEFAULT_MAX_REVISIONS",
]
