"""Typed inputs and outputs (DECISIONS.md D-005).

Pydantic at the LLM boundary, so model output is validated before anything uses it. Frozen dataclasses everywhere
else. The existing Request, Subject and EvidenceRecord are reused, not redefined.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from evidence import EvidenceRecord


class Intent(str, Enum):
    DELETION = "DELETION"
    NON_DELETION = "NON_DELETION"


class Source(str, Enum):
    RULES = "rules"
    PRIMARY = "primary"          # Luna
    ESCALATION = "escalation"    # Sol
    FALLBACK = "fallback"        # models unavailable: safe default, flagged for review


# ---- LLM boundary (pydantic) ----
class IntentVerdict(BaseModel):
    intent: Literal["DELETION", "NON_DELETION"]
    confidence: Literal["high", "medium", "low"]
    reason: str = Field(max_length=300)


# ---- internal (frozen dataclasses) ----
@dataclass(frozen=True)
class IntentDecision:
    intent: Intent
    source: Source
    needs_review: bool
    reasons: tuple[str, ...] = ()
    model: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    records: tuple[EvidenceRecord, ...]
    resource_ids: tuple[str, ...]
    unread_files: tuple[str, ...]


@dataclass(frozen=True)
class Step:
    step_no: int
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: str                      # COMPLETE | INCOMPLETE | NOT_APPLICABLE | UNDECIDED (until the planner exists)
    steps: tuple[Step, ...] = ()
    flags: tuple[str, ...] = ()

    def steps_json(self) -> str:
        return json.dumps([asdict(s) for s in self.steps], separators=(",", ":"), sort_keys=True)
