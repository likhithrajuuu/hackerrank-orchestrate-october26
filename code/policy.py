"""Stage 5: what policy requires for each resource.

The treatment comes from deletion_rules.csv. The operations that go with each treatment come from the allowed
operations in the problem statement. Nothing here looks at the observed state: that is stage 6.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from contracts import Requirement, ResourceState, Treatment
from loaders import Rule

# treatment -> (finding op, remediation op, verification op)
OPERATIONS = {
    Treatment.DELETE: ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    Treatment.REDACT: ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    Treatment.ANONYMIZE: ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    Treatment.RETAIN: ("", "", "VERIFY_RECORD_PRESENT"),
    Treatment.DISABLE: ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
}
# A retained backup is not deleted: restore access is blocked instead.
BACKUP_OPERATIONS = ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED")

DATA_FLOW_NOTE = "no active sync, import or restore path may be able to recreate prohibited data (problem statement)"


def _parse_until(detail: str) -> datetime | None:
    if not detail.startswith("retention_until="):
        return None
    try:
        return datetime.fromisoformat(detail.split("=", 1)[1].replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


class PolicyEngine:
    def __init__(self, rules: Iterable[Rule]):
        self._rules = {rule.data_category: rule for rule in rules}

    def requirement(self, resource: ResourceState, audit_at: datetime) -> Requirement:
        category, rid = resource.category, resource.resource_id

        if category == "DATA_FLOW":
            return Requirement(rid, category, Treatment.DISABLE, "", DATA_FLOW_NOTE, *OPERATIONS[Treatment.DISABLE])

        rule = self._rules.get(category)
        if rule is None:
            return Requirement(rid, category, None, "", f"no rule in deletion_rules.csv covers {category}", "", "", "",
                               needs_review=True)

        treatment = Treatment(rule.required_action)
        finding, remediation, verification = BACKUP_OPERATIONS if category == "BACKUP_ARCHIVE" else OPERATIONS[treatment]
        reason, review = f"{rule.rule_id}: {category} is {rule.required_action} ({rule.condition})", False

        if category == "SECURITY_AUDIT_LOG":
            until = max((u for u in (_parse_until(o.detail) for o in resource.observations) if u), default=None)
            if until is not None and until < audit_at:
                reason, review = f"{reason}; the retention period ended on {until:%Y-%m-%d}, review", True
        return Requirement(rid, category, treatment, rule.rule_id, reason, finding, remediation, verification, review)
