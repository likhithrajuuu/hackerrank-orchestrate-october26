"""Typed inputs and outputs (DECISIONS.md D-005).

Pydantic at the LLM boundary, so model output is validated before anything uses it. Frozen dataclasses everywhere
else. The existing Request, Subject and EvidenceRecord are reused, not redefined.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
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


# ---- stage 4: observed state of a resource ----
class State(str, Enum):
    PRESENT = "PRESENT"                  
    ABSENT = "ABSENT"                   
    EXPOSED = "EXPOSED"                 
    DEIDENTIFIED = "DEIDENTIFIED"       
    ACTIVE = "ACTIVE"                    
    DISABLED = "DISABLED"                
    RESTORABLE = "RESTORABLE"            
    RESTORE_BLOCKED = "RESTORE_BLOCKED"  
    UNKNOWN = "UNKNOWN"                  


@dataclass(frozen=True)
class Observation:
    """One piece of evidence about one resource, already read as a state."""
    resource_id: str
    system_id: str
    category: str
    state: State
    file_id: str
    kind: str                            # sql | json | log | text | pdf | image
    captured_at: datetime                # when the file was captured
    observed_at: datetime | None         # the event's own time, when it has one
    source: str                          # table, event or record type the state came from
    detail: str = ""

    @property
    def when(self) -> datetime:
        return self.observed_at or self.captured_at


@dataclass(frozen=True)
class Claim:
    """An activity-log row. It says what a worker reported, not what is true."""
    activity_id: str
    event_type: str
    result: str
    when: datetime
    details: str = ""


@dataclass(frozen=True)
class ResourceState:
    resource_id: str
    system_id: str
    category: str
    state: State
    evidence_id: str                     # a citable file for this state, or "" when none may be cited
    citable: bool
    observed_at: datetime | None
    conflict: bool                       # observations at the same moment disagree (for example primary versus replica)
    observations: tuple[Observation, ...] = ()
    claims: tuple[Claim, ...] = ()

    @property
    def last_seen(self) -> datetime | None:
        """The most recent moment any evidence spoke about this resource."""
        return max((o.when for o in self.observations), default=None)


@dataclass(frozen=True)
class StateResult:
    states: tuple[ResourceState, ...]

    def get(self, resource_id: str) -> ResourceState | None:
        return next((s for s in self.states if s.resource_id == resource_id), None)


# ---- stage 5: the required treatment ----
class Treatment(str, Enum):
    DELETE = "DELETE"
    REDACT = "REDACT"
    ANONYMIZE = "ANONYMIZE"
    RETAIN = "RETAIN"
    DISABLE = "DISABLE"          # data flows: not a rule in deletion_rules.csv, required by "no path may recreate data"


@dataclass(frozen=True)
class Requirement:
    """What policy requires for one resource, and the operations that go with that treatment."""
    resource_id: str
    category: str
    treatment: Treatment | None  # None: the category is not covered by any rule
    rule_id: str
    reason: str
    finding_op: str              # reported when the resource violates the treatment
    remediation_op: str          # proposed to fix the violation
    verification_op: str         # the check that proves the required final state
    needs_review: bool = False


# ---- stage 6: state compared with policy ----
class Verdict(str, Enum):
    COMPLIANT = "COMPLIANT"      # the state is the required one
    VIOLATION = "VIOLATION"      # it is not
    UNRESOLVED = "UNRESOLVED"    # the evidence cannot say
    STALE = "STALE"              # only evidence from before the request: it cannot show the effect of the request


@dataclass(frozen=True)
class Assessment:
    resource_id: str
    system_id: str
    category: str
    state: State
    treatment: Treatment | None
    verdict: Verdict
    finding_op: str | None       # set only for a violation
    remediation_op: str | None   # set only for a violation
    verification_op: str | None  # set for every resource that is assessed
    evidence_id: str             # a citable file for the observed state, or ""
    citable: bool
    reason: str
    needs_review: bool = False


@dataclass(frozen=True)
class AuditResult:
    status: str                  # COMPLETE | INCOMPLETE | UNDECIDED
    assessments: tuple[Assessment, ...]
    needs_review: bool
    reasons: tuple[str, ...] = ()

    def of(self, verdict: Verdict) -> tuple[Assessment, ...]:
        return tuple(a for a in self.assessments if a.verdict == verdict)
