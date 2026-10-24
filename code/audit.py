"""Stage 6: compare each resource's observed state with what policy requires, and decide the case status.

A case is COMPLETE only when nothing violates policy. Anything that does makes it INCOMPLETE. When no evidence at all
can be assessed, the status is UNDECIDED: an unverified case is never reported as complete.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from contracts import Assessment, AuditResult, Requirement, ResourceState, State, Treatment, Verdict

EXISTS = {State.PRESENT, State.EXPOSED}


def judge(requirement: Requirement, resource: ResourceState) -> tuple[Verdict, str]:
    """The verdict for one resource, with a plain-language reason."""
    treatment, state = requirement.treatment, resource.state
    if treatment is None:
        return Verdict.UNRESOLVED, requirement.reason
    if state == State.UNKNOWN:
        return Verdict.UNRESOLVED, "no readable evidence shows its state"
    backup = resource.category == "BACKUP_ARCHIVE"

    if treatment == Treatment.DELETE:
        if state in EXISTS:
            return Verdict.VIOLATION, "the record still exists but policy requires it to be deleted"
        if state == State.ABSENT:
            return Verdict.COMPLIANT, "the record is gone, as required"

    elif treatment in (Treatment.REDACT, Treatment.ANONYMIZE):
        verb = "redacted" if treatment == Treatment.REDACT else "anonymized"
        if state == State.EXPOSED:
            return Verdict.VIOLATION, f"the record still carries direct identifiers but must be {verb}"
        if state == State.DEIDENTIFIED:
            return Verdict.COMPLIANT, f"the record is kept and carries no direct identifiers, as required"
        if state == State.ABSENT:
            return Verdict.UNRESOLVED, f"policy keeps this record {verb}, but it is gone: review"

    elif treatment == Treatment.RETAIN:
        if backup:
            if state == State.RESTORABLE:
                return Verdict.VIOLATION, "the backup is retained but can still be restored, so it can recreate deleted data"
            if state == State.RESTORE_BLOCKED:
                return Verdict.COMPLIANT, "the backup is retained and its restore is blocked, as required"
            if state == State.ABSENT:
                return Verdict.UNRESOLVED, "a retained backup is gone: review"
        else:
            if state == State.ABSENT:
                return Verdict.VIOLATION, "policy requires this record to be retained but it is gone"
            if state in (State.PRESENT, State.EXPOSED, State.DEIDENTIFIED):
                return Verdict.COMPLIANT, "the record is retained, as required"

    elif treatment == Treatment.DISABLE:
        if state == State.ACTIVE:
            return Verdict.VIOLATION, "the data flow is active and can copy or recreate the data"
        if state in (State.DISABLED, State.ABSENT):
            return Verdict.COMPLIANT, "the data flow is not running, as required"

    return Verdict.UNRESOLVED, f"the observed state {state.value} does not settle the {treatment.value} requirement"


class Auditor:
    def assess(self, resource: ResourceState, requirement: Requirement, requested_at: datetime) -> Assessment:
        last = resource.last_seen
        if last is not None and last < requested_at:
            return Assessment(resource.resource_id, resource.system_id, resource.category, resource.state,
                              requirement.treatment, Verdict.STALE, None, None, None, "", False,
                              f"last evidence is from {last:%Y-%m-%d}, before the request on {requested_at:%Y-%m-%d}: "
                              f"it cannot show the effect of the request")

        verdict, reason = judge(requirement, resource)
        violation = verdict == Verdict.VIOLATION
        has_ops = bool(requirement.finding_op)                         # a missing retained record has no operation to fix it
        review = requirement.needs_review or resource.conflict or (violation and not has_ops)
        if resource.conflict:
            reason += "; observations at the same moment disagree (for example primary versus replica)"
        return Assessment(
            resource.resource_id, resource.system_id, resource.category, resource.state, requirement.treatment, verdict,
            requirement.finding_op if violation and has_ops else None,
            requirement.remediation_op if violation and has_ops else None,
            requirement.verification_op or None, resource.evidence_id, resource.citable, reason, review)

    def audit(self, resources: Iterable[ResourceState], requirements: Iterable[Requirement],
              requested_at: datetime) -> AuditResult:
        by_id = {r.resource_id: r for r in requirements}
        # Sorted, so the result never depends on the order the resources arrive in.
        assessments = tuple(sorted((self.assess(res, by_id[res.resource_id], requested_at) for res in resources),
                                   key=lambda a: (a.system_id, a.resource_id)))
        violations = [a for a in assessments if a.verdict == Verdict.VIOLATION]
        compliant = [a for a in assessments if a.verdict == Verdict.COMPLIANT]
        unresolved = [a for a in assessments if a.verdict == Verdict.UNRESOLVED]
        stale = [a for a in assessments if a.verdict == Verdict.STALE]

        reasons = [f"{len(violations)} violation(s), {len(compliant)} compliant, {len(unresolved)} unresolved, "
                   f"{len(stale)} stale"]
        if violations:
            status = "INCOMPLETE"
        elif compliant:
            status = "COMPLETE"
        else:
            status = "UNDECIDED"
            reasons.append("no evidence could be assessed, so the case is not reported as complete")
        review = any(a.needs_review for a in assessments) or bool(unresolved) or status == "UNDECIDED"
        return AuditResult(status, assessments, review, tuple(reasons))
