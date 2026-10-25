"""Stage 7b: the validator (DECISIONS.md D-015). Checks every row before it can be written.

An "error" blocks the row; a "warning" is reported but does not. Nothing is repaired here: a bad row is a bug to fix, not
something to patch over.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping, Sequence

from contracts import Problem, Step
from planner import OPERATIONS, PHASE_OF, PHASES, STATE_OF, canonical_key

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


class Validator:
    def __init__(self, evidence_times: Mapping[str, datetime], activity_times: Mapping[str, datetime],
                 audit_times: Mapping[str, datetime]):
        """Each mapping is id -> the moment that item exists (file captured, activity happened, audit entry written)."""
        self._times = (evidence_times, activity_times, audit_times)

    def _cited_time(self, evidence_id: str) -> datetime | None:
        return next((t[evidence_id] for t in self._times if evidence_id in t), None)

    def validate(self, case_id: str, status: str, steps: Sequence[Step], audit_at: datetime) -> list[Problem]:
        out: list[Problem] = []
        err = lambda msg: out.append(Problem("error", f"{case_id}: {msg}"))
        warn = lambda msg: out.append(Problem("warning", f"{case_id}: {msg}"))

        if status not in STATUSES:
            err(f"status {status!r} is not one of {STATUSES}")
        if status == "NOT_APPLICABLE" and steps:
            err("NOT_APPLICABLE must have no steps")
        if status == "COMPLETE" and any(s.phase != "VERIFICATION" for s in steps):
            err("COMPLETE may contain only VERIFICATION steps")
        if status == "INCOMPLETE" and not any(s.phase == "FINDING" for s in steps):
            warn("INCOMPLETE has no finding")

        if [s.step_no for s in steps] != list(range(1, len(steps) + 1)):
            err("step_no must run 1, 2, 3 ... without gaps")
        seen = set()
        for s in steps:
            if s.phase not in PHASES:
                err(f"step {s.step_no}: unknown phase {s.phase!r}")
            if s.operation not in OPERATIONS:
                err(f"step {s.step_no}: unknown operation {s.operation!r}")
                continue
            if PHASE_OF[s.operation] != s.phase:
                err(f"step {s.step_no}: {s.operation} belongs to {PHASE_OF[s.operation]}, not {s.phase}")
            if STATE_OF[s.operation] != s.state:
                err(f"step {s.step_no}: {s.operation} must have state {STATE_OF[s.operation]}, not {s.state}")
            if not s.system_id or not s.resource_id:
                err(f"step {s.step_no}: system_id and resource_id are required")
            key = (s.phase, s.system_id, s.resource_id, s.operation)
            if key in seen:
                err(f"step {s.step_no}: duplicate {key}")
            seen.add(key)

            # Evidence rule: findings and COMPLETE checks cite evidence; remediation and future checks do not.
            needs_evidence = s.phase == "FINDING" or (s.phase == "VERIFICATION" and status == "COMPLETE")
            if needs_evidence and not s.evidence_id:
                (warn if s.phase == "FINDING" else err)(f"step {s.step_no}: {s.operation} on {s.resource_id} must cite evidence")
            if not needs_evidence and s.evidence_id:
                err(f"step {s.step_no}: {s.phase} step must leave evidence_id empty")
            if s.evidence_id:
                moment = self._cited_time(s.evidence_id)
                if moment is None:
                    err(f"step {s.step_no}: cited id {s.evidence_id!r} does not exist")
                elif moment > audit_at:
                    err(f"step {s.step_no}: {s.evidence_id} is from {moment:%Y-%m-%d %H:%M}, after audit_at {audit_at:%Y-%m-%d %H:%M}")

        ordered = sorted(steps, key=lambda s: canonical_key(s.phase, s.operation, s.system_id, s.resource_id)
                         if s.operation in OPERATIONS and s.phase in PHASES else (99, 99, "", ""))
        if list(steps) != ordered:
            err("steps are not in the canonical order (phase, operation order, system_id, resource_id)")
        return out
