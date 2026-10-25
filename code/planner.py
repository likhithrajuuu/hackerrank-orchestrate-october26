"""Stage 7a: the dependency-aware remediation planner (DECISIONS.md D-013, D-014).

Turns the audit's assessments into the ordered steps. No model is involved and nothing here decides a policy result: it
only arranges what the audit already decided.

The order is one canonical key, which is also the order the problem statement requires and the dependency order:
phase (findings, then remediation, then verification), then the operation's position in the statement's table
(DISABLE_DATA_FLOW, BLOCK_RESTORE, DELETE_RECORD, REDACT_RECORD, ANONYMIZE_RECORD: a running flow or a restorable backup
would undo any record change made before it), then system_id, then resource_id.
"""
from __future__ import annotations

from contracts import AuditResult, Plan, Step, Verdict

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
OPERATIONS = (
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
)
PHASE_OF = {**{op: "FINDING" for op in OPERATIONS[:4]}, **{op: "REMEDIATION" for op in OPERATIONS[4:9]},
            **{op: "VERIFICATION" for op in OPERATIONS[9:]}}
STATE_OF = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}


def canonical_key(phase: str, operation: str, system_id: str, resource_id: str) -> tuple:
    return PHASES.index(phase), OPERATIONS.index(operation), system_id, resource_id


class Planner:
    def __init__(self, undecided_status: str = "COMPLETE"):
        # D-018: a case with no assessable evidence has no violation we could cite, so it defaults to this status.
        self._undecided_status = undecided_status

    def plan(self, audit: AuditResult) -> Plan:
        notes: list[str] = []
        status = audit.status
        if status == "UNDECIDED":
            status = self._undecided_status
            notes.append(f"no assessable evidence: defaulted to {status} (D-018), flagged for review")

        rows: list[tuple] = []        # (operation, system_id, resource_id, evidence_id)
        if status == "COMPLETE":
            # A COMPLETE verification must cite the evidence that proves the compliant state at audit_at.
            for a in audit.assessments:
                if a.verdict != Verdict.COMPLIANT or not a.verification_op:
                    continue
                if a.citable and a.evidence_id:
                    rows.append((a.verification_op, a.system_id, a.resource_id, a.evidence_id))
                else:
                    notes.append(f"{a.resource_id}: compliant but no citable evidence, so its check is left out")
        else:
            for a in audit.assessments:
                if a.verdict == Verdict.VIOLATION and a.finding_op:
                    evidence = a.evidence_id if a.citable else ""
                    if not evidence:
                        notes.append(f"{a.resource_id}: finding has no citable evidence")
                    rows.append((a.finding_op, a.system_id, a.resource_id, evidence))
                    rows.append((a.remediation_op, a.system_id, a.resource_id, ""))
                elif a.verdict == Verdict.VIOLATION:
                    notes.append(f"{a.resource_id}: {a.reason} (no allowed operation can repair this)")
            for a in audit.assessments:                 # the checks that must pass after the fixes: future work, no evidence
                if a.verdict in (Verdict.COMPLIANT, Verdict.VIOLATION) and a.verification_op:
                    rows.append((a.verification_op, a.system_id, a.resource_id, ""))
        for a in audit.assessments:
            if a.verdict in (Verdict.UNRESOLVED, Verdict.STALE):
                notes.append(f"{a.resource_id}: {a.verdict.value.lower()}, no step produced ({a.reason})")

        rows = sorted(set(rows), key=lambda r: canonical_key(PHASE_OF[r[0]], r[0], r[1], r[2]))
        steps = tuple(Step(i, PHASE_OF[op], system, resource, op, STATE_OF[op], evidence)
                      for i, (op, system, resource, evidence) in enumerate(rows, start=1))
        return Plan(status, steps, tuple(notes))
