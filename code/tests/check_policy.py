import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable
os.environ.pop("OPENAI_API_KEY", None)                           # the evidence store must not make vision calls here
os.environ.pop("LLM_API_KEY", None)

from audit import Auditor, judge
from contracts import Observation, ResourceState, State, Treatment, Verdict
from loaders import load_rules
from policy import PolicyEngine

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
REQUESTED = T0 - timedelta(days=3)
AUDIT = T0 + timedelta(days=1)
ENGINE, AUDITOR = PolicyEngine(load_rules()), Auditor()


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


def resource(rid, category, state, seen=T0, conflict=False, detail="", file_id="evidence_x"):
    obs = Observation(rid, "sys", category, state, file_id, "log", seen, None, "src", detail)
    return ResourceState(rid, "sys", category, state, file_id, True, None, conflict, (obs,), ())


def assess(rid, category, state, **kw):
    res = resource(rid, category, state, **kw)
    return AUDITOR.assess(res, ENGINE.requirement(res, AUDIT), REQUESTED)


# 1) Stage 5: the treatment per category comes from deletion_rules.csv.
treatment = {c: ENGINE.requirement(resource("x_1", c, State.PRESENT), AUDIT).treatment for c in
             ("USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "USER_EXPORT", "MARKETING_PROFILE",
              "SUPPORT_ATTACHMENT", "SUPPORT_MESSAGE", "ANALYTICS_PROFILE", "ACCOUNT_TOMBSTONE", "SECURITY_AUDIT_LOG",
              "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "DATA_FLOW")}
check("profile, search, export, attachment, secondary identifier and marketing are DELETE",
      all(treatment[c] == Treatment.DELETE for c in ("USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY",
                                                      "USER_EXPORT", "MARKETING_PROFILE", "SUPPORT_ATTACHMENT")))
check("support messages and tombstones are REDACT", treatment["SUPPORT_MESSAGE"] == treatment["ACCOUNT_TOMBSTONE"] == Treatment.REDACT)
check("analytics is ANONYMIZE", treatment["ANALYTICS_PROFILE"] == Treatment.ANONYMIZE)
check("audit logs, legal holds and backups are RETAIN",
      all(treatment[c] == Treatment.RETAIN for c in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE")))
check("a data flow must be DISABLED although no rule file line covers it", treatment["DATA_FLOW"] == Treatment.DISABLE)

backup = ENGINE.requirement(resource("backup_1", "BACKUP_ARCHIVE", State.RESTORABLE), AUDIT)
check("a retained backup is fixed by blocking restore, never by deleting",
      (backup.finding_op, backup.remediation_op, backup.verification_op) ==
      ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"))
check("a retained record has nothing to fix, only a presence check",
      ENGINE.requirement(resource("audit_record_1", "SECURITY_AUDIT_LOG", State.PRESENT), AUDIT).remediation_op == "")
unknown = ENGINE.requirement(resource("zzz_1", "NOT_A_CATEGORY", State.PRESENT), AUDIT)
check("a category no rule covers needs review", unknown.treatment is None and unknown.needs_review)

future = resource("audit_record_1", "SECURITY_AUDIT_LOG", State.PRESENT, detail="retention_until=2033-12-31T23:59:59Z")
expired = resource("audit_record_2", "SECURITY_AUDIT_LOG", State.PRESENT, detail="retention_until=2026-01-01T00:00:00Z")
check("a retention period that is still running needs no review", not ENGINE.requirement(future, AUDIT).needs_review)
check("a retention period that has ended is flagged for review", ENGINE.requirement(expired, AUDIT).needs_review)

# 2) Stage 6: the verdict for each treatment and state.
cases = [
    ("USER_PROFILE", State.PRESENT, Verdict.VIOLATION), ("USER_PROFILE", State.EXPOSED, Verdict.VIOLATION),
    ("USER_PROFILE", State.ABSENT, Verdict.COMPLIANT), ("USER_PROFILE", State.UNKNOWN, Verdict.UNRESOLVED),
    ("SUPPORT_MESSAGE", State.EXPOSED, Verdict.VIOLATION), ("SUPPORT_MESSAGE", State.DEIDENTIFIED, Verdict.COMPLIANT),
    ("SUPPORT_MESSAGE", State.ABSENT, Verdict.UNRESOLVED), ("SUPPORT_MESSAGE", State.PRESENT, Verdict.UNRESOLVED),
    ("ANALYTICS_PROFILE", State.EXPOSED, Verdict.VIOLATION), ("ANALYTICS_PROFILE", State.DEIDENTIFIED, Verdict.COMPLIANT),
    ("SECURITY_AUDIT_LOG", State.PRESENT, Verdict.COMPLIANT), ("SECURITY_AUDIT_LOG", State.ABSENT, Verdict.VIOLATION),
    ("BACKUP_ARCHIVE", State.RESTORABLE, Verdict.VIOLATION), ("BACKUP_ARCHIVE", State.RESTORE_BLOCKED, Verdict.COMPLIANT),
    ("DATA_FLOW", State.ACTIVE, Verdict.VIOLATION), ("DATA_FLOW", State.DISABLED, Verdict.COMPLIANT),
    ("ACCOUNT_TOMBSTONE", State.EXPOSED, Verdict.VIOLATION), ("ACCOUNT_TOMBSTONE", State.DEIDENTIFIED, Verdict.COMPLIANT),
]
for category, state, expected in cases:
    res = resource("x_1", category, state)
    verdict, _ = judge(ENGINE.requirement(res, AUDIT), res)
    check(f"{category} that is {state.value} is {expected.value}", verdict == expected)

# 3) What an assessment carries.
a = assess("search_1", "SEARCH_INDEX_ENTRY", State.PRESENT)
check("a violation carries its finding, fix and check",
      (a.finding_op, a.remediation_op, a.verification_op) == ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"))
a = assess("account_1", "USER_PROFILE", State.ABSENT)
check("a compliant resource carries only its check", (a.finding_op, a.remediation_op, a.verification_op) == (None, None, "VERIFY_RECORD_ABSENT"))
a = assess("audit_record_1", "SECURITY_AUDIT_LOG", State.ABSENT)
check("a retained record that is gone is a violation with no operation to fix it, and needs review",
      a.verdict == Verdict.VIOLATION and a.finding_op is None and a.needs_review)
a = assess("search_1", "SEARCH_INDEX_ENTRY", State.PRESENT, conflict=True)
check("a primary-versus-replica conflict is flagged for review", a.needs_review and "disagree" in a.reason)

old = assess("account_9", "USER_PROFILE", State.ABSENT, seen=REQUESTED - timedelta(days=100))
check("evidence older than the request is STALE and produces no operations",
      old.verdict == Verdict.STALE and old.verification_op is None and old.finding_op is None)
edge = assess("account_9", "USER_PROFILE", State.ABSENT, seen=REQUESTED)
check("evidence from the very moment of the request is not stale", edge.verdict != Verdict.STALE)

# 4) The case status.
def case(*specs, **kw):
    ress = [resource(f"{c.lower()}_{i}", c, s, **kw) for i, (c, s) in enumerate(specs)]
    return AUDITOR.audit(ress, [ENGINE.requirement(r, AUDIT) for r in ress], REQUESTED)

check("one violation makes the case INCOMPLETE",
      case(("USER_PROFILE", State.ABSENT), ("SEARCH_INDEX_ENTRY", State.PRESENT)).status == "INCOMPLETE")
check("everything compliant makes it COMPLETE",
      case(("USER_PROFILE", State.ABSENT), ("SECURITY_AUDIT_LOG", State.PRESENT)).status == "COMPLETE")
check("a restorable backup alone makes it INCOMPLETE",
      case(("USER_PROFILE", State.ABSENT), ("BACKUP_ARCHIVE", State.RESTORABLE)).status == "INCOMPLETE")
check("no evidence at all is UNDECIDED, never COMPLETE", case().status == "UNDECIDED")
r = case(("USER_PROFILE", State.UNKNOWN))
check("only unresolved resources is UNDECIDED and flagged for review", r.status == "UNDECIDED" and r.needs_review)
r = case(("USER_PROFILE", State.ABSENT), ("SUPPORT_MESSAGE", State.PRESENT))
check("a compliant case with an unresolved resource is COMPLETE but flagged", r.status == "COMPLETE" and r.needs_review)
check("only stale evidence is UNDECIDED", case(("USER_PROFILE", State.ABSENT), seen=REQUESTED - timedelta(days=9)).status == "UNDECIDED")

# 5) Real data: stages 4, 5 and 6 together on labeled samples.
from evidence import EvidenceStore
from identity import IdentityIndex
from loaders import load_activity, load_audit_log, load_identities, load_requests
from state import StateReconstructor

store = EvidenceStore.load()
claims = {}
for row in load_activity():
    claims.setdefault(row.resource_id, []).append(row)
index = IdentityIndex(load_identities(), load_audit_log())
samples = {r.case_id: r for r in load_requests("sample_deletion_requests.csv")}


def run_case(case_id):
    request = samples[case_id]
    subject = index.resolve(request.email, request.audit_at)
    ids = {r.resource_id for r in store.for_tokens(set(subject.emails) | set(subject.hashes) | {subject.user_id}, request.audit_at) if r.resource_id}
    states = StateReconstructor(store, claims).reconstruct(ids, request.audit_at)
    return AUDITOR.audit(states, [ENGINE.requirement(s, request.audit_at) for s in states], request.requested_at)


r14 = run_case("case_14")
check("case_14 is INCOMPLETE and the only violation is the restorable backup",
      r14.status == "INCOMPLETE" and [(a.resource_id, a.finding_op) for a in r14.of(Verdict.VIOLATION)] == [("backup_3870", "REPORT_RESTORE_RISK")])
check("case_14 keeps the audit record as a retained, compliant resource",
      any(a.resource_id == "audit_record_987" and a.verdict == Verdict.COMPLIANT and a.verification_op == "VERIFY_RECORD_PRESENT" for a in r14.assessments))

r18 = run_case("case_18")
check("case_18 reports four violations: delete search, redact support, anonymize analytics, disable the flow",
      sorted(a.remediation_op for a in r18.of(Verdict.VIOLATION)) == ["ANONYMIZE_RECORD", "DELETE_RECORD", "DISABLE_DATA_FLOW", "REDACT_RECORD"])

r10 = run_case("case_10")
check("case_10: the legacy contact index entry is a secondary identifier that must be deleted",
      any(a.resource_id == "support_002" and a.category == "SECONDARY_IDENTIFIER" and a.remediation_op == "DELETE_RECORD" for a in r10.assessments)
      and r10.status == "INCOMPLETE")
check("case_10: the March lookup of an older account is STALE, not a finding",
      all(a.verdict != Verdict.VIOLATION for a in r10.assessments if a.resource_id.startswith("account")))
check("case_17: the stale March account lookup is excluded", any(a.verdict == Verdict.STALE for a in run_case("case_17").assessments))
check("case_26 (labeled COMPLETE) has no violation", run_case("case_26").status == "COMPLETE")

print("policy and audit checks passed")
