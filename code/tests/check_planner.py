import csv
import dataclasses
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable
os.environ.pop("OPENAI_API_KEY", None)                           # no model calls from a test
os.environ.pop("LLM_API_KEY", None)

from audit import Auditor
from contracts import CaseResult, Observation, ResourceState, State, Step
from loaders import load_rules
from output import check_file, write_output
from planner import Planner
from policy import PolicyEngine
from validator import Validator

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
AUDIT = T0 + timedelta(days=1)
REQUESTED = T0 - timedelta(days=3)
ENGINE, AUDITOR = PolicyEngine(load_rules()), Auditor()


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


def res(rid, category, state, system, evidence="evidence_1", citable=True, seen=T0):
    obs = Observation(rid, system, category, state, evidence, "log", seen, None, "src")
    return ResourceState(rid, system, category, state, evidence if citable else "", citable, None, False, (obs,), ())


def plan_for(resources, planner=None):
    audit = AUDITOR.audit(resources, [ENGINE.requirement(r, AUDIT) for r in resources], REQUESTED)
    return (planner or Planner()).plan(audit)


def shape(plan):
    return [(s.phase, s.operation, s.system_id, s.resource_id) for s in plan.steps]


# ---- Custom evaluation case A: everything is wrong at once. Order must be the dependency order. ----
A = [res("account_1", "USER_PROFILE", State.ABSENT, "account_service"),
     res("search_1", "SEARCH_INDEX_ENTRY", State.PRESENT, "search_service"),
     res("analytics_1", "ANALYTICS_PROFILE", State.EXPOSED, "analytics_platform"),
     res("support_1", "SUPPORT_MESSAGE", State.EXPOSED, "support_platform"),
     res("data_flow_1", "DATA_FLOW", State.ACTIVE, "integration_service"),
     res("backup_1", "BACKUP_ARCHIVE", State.RESTORABLE, "backup_service"),
     res("audit_record_1", "SECURITY_AUDIT_LOG", State.PRESENT, "security_service")]
plan = plan_for(A)
check("case A is INCOMPLETE", plan.status == "INCOMPLETE")
check("case A: findings, then remediation, then verification",
      [s.phase for s in plan.steps] == sorted((s.phase for s in plan.steps), key=("FINDING", "REMEDIATION", "VERIFICATION").index))
remediation = [s.operation for s in plan.steps if s.phase == "REMEDIATION"]
check("case A: a running flow is stopped first, then restores blocked, then records changed",
      remediation == ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"])
check("case A: findings follow the statement's order",
      [s.operation for s in plan.steps if s.phase == "FINDING"] ==
      ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"])
check("case A: two exposed records are ordered by system_id (analytics before support)",
      [s.resource_id for s in plan.steps if s.operation == "REPORT_EXPOSED_PII"] == ["analytics_1", "support_1"])
check("case A: the retained audit record is verified present and never touched",
      ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_1") in shape(plan)
      and not any(s.resource_id == "audit_record_1" and s.phase != "VERIFICATION" for s in plan.steps))
check("case A: the retained backup gets BLOCK_RESTORE, never a delete",
      ("REMEDIATION", "BLOCK_RESTORE", "backup_service", "backup_1") in shape(plan)
      and not any(s.operation == "DELETE_RECORD" and s.resource_id == "backup_1" for s in plan.steps))
check("case A: findings cite evidence, remediation and future checks do not",
      all(bool(s.evidence_id) == (s.phase == "FINDING") for s in plan.steps))
check("case A: step numbers run 1..n", [s.step_no for s in plan.steps] == list(range(1, len(plan.steps) + 1)))
check("case A: every state matches its operation (for example DISABLE is DISABLED)",
      {s.operation: s.state for s in plan.steps}["DISABLE_DATA_FLOW"] == "DISABLED"
      and {s.operation: s.state for s in plan.steps}["REPORT_RESTORE_RISK"] == "ACTIVE")

rng = random.Random(5)
same = True
for _ in range(25):
    shuffled = A[:]
    rng.shuffle(shuffled)
    same &= plan_for(shuffled) == plan
check("case A: the plan is identical under 25 random orderings of the same resources", same)

# ---- Custom evaluation case B: a clean case. Verification only, and only what can be proven. ----
B = [res("account_2", "USER_PROFILE", State.ABSENT, "account_service", evidence="evidence_7"),
     res("audit_record_2", "SECURITY_AUDIT_LOG", State.PRESENT, "security_service", evidence="evidence_8"),
     res("backup_2", "BACKUP_ARCHIVE", State.RESTORE_BLOCKED, "backup_service", evidence="", citable=False)]
plan = plan_for(B)
check("case B is COMPLETE with verification steps only", plan.status == "COMPLETE" and {s.phase for s in plan.steps} == {"VERIFICATION"})
check("case B: every COMPLETE check cites the evidence that proves it",
      [(s.resource_id, s.evidence_id) for s in plan.steps] == [("account_2", "evidence_7"), ("audit_record_2", "evidence_8")])
check("case B: a compliant resource with nothing citable is left out and explained",
      all(s.resource_id != "backup_2" for s in plan.steps) and any("backup_2" in n for n in plan.notes))

# ---- Custom evaluation case C: nothing can be assessed (D-018). ----
plan = plan_for([])
check("case C: no assessable evidence defaults to COMPLETE with no steps, and says so",
      plan.status == "COMPLETE" and plan.steps == () and any("D-018" in n for n in plan.notes))
check("case C: the default is configurable", plan_for([], Planner(undecided_status="INCOMPLETE")).status == "INCOMPLETE")

# ---- Custom evaluation case D: a retained record is missing; no allowed operation can fix it. ----
plan = plan_for([res("audit_record_4", "SECURITY_AUDIT_LOG", State.ABSENT, "security_service"),
                 res("account_4", "USER_PROFILE", State.ABSENT, "account_service")])
check("case D: INCOMPLETE, no invented remediation, and the problem is explained",
      plan.status == "INCOMPLETE" and not any(s.phase == "REMEDIATION" for s in plan.steps)
      and any("audit_record_4" in n and "retained" in n for n in plan.notes))

# ---- Validator: every rule rejects what it should. ----
V = Validator({"evidence_1": T0, "evidence_late": AUDIT + timedelta(days=1)}, {"activity_1": T0}, {"audit_1": T0})


def step(n, phase, op, state, evidence="", system="s", resource="r_1"):
    return Step(n, phase, system, resource, op, state, evidence)


def errors(status, steps):
    return [p.message for p in V.validate("c", status, steps, AUDIT) if p.severity == "error"]


good = [step(1, "FINDING", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1"), step(2, "REMEDIATION", "DELETE_RECORD", "ABSENT"),
        step(3, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT")]
check("a correct INCOMPLETE row passes", errors("INCOMPLETE", good) == [])
check("a correct COMPLETE row passes",
      errors("COMPLETE", [step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1")]) == [])
check("an unknown status is rejected", bool(errors("DONE", [])))
check("NOT_APPLICABLE with steps is rejected", bool(errors("NOT_APPLICABLE", good)))
check("NOT_APPLICABLE with no steps passes", errors("NOT_APPLICABLE", []) == [])
check("COMPLETE with a finding is rejected", bool(errors("COMPLETE", good[:1])))
check("a gap in step_no is rejected", bool(errors("INCOMPLETE", [good[0], dataclasses.replace(good[1], step_no=3)])))
check("a wrong state for the operation is rejected",
      bool(errors("INCOMPLETE", [good[0], step(2, "REMEDIATION", "DELETE_RECORD", "PRESENT"), good[2]])))
check("an operation in the wrong phase is rejected",
      bool(errors("INCOMPLETE", [good[0], step(2, "VERIFICATION", "DELETE_RECORD", "ABSENT"), good[2]])))
check("an unknown operation is rejected", bool(errors("INCOMPLETE", [step(1, "FINDING", "DESTROY", "PRESENT")])))
check("a duplicate step is rejected", bool(errors("INCOMPLETE", [good[0], dataclasses.replace(good[0], step_no=2)])))
check("a COMPLETE check with no evidence is rejected", bool(errors("COMPLETE", [step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT")])))
check("evidence on a remediation step is rejected",
      bool(errors("INCOMPLETE", [good[0], step(2, "REMEDIATION", "DELETE_RECORD", "ABSENT", "evidence_1"), good[2]])))
check("a cited id that does not exist is rejected",
      bool(errors("COMPLETE", [step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_404")])))
check("evidence captured after audit_at is rejected",
      bool(errors("COMPLETE", [step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_late")])))
check("an activity id and an audit id are valid citations",
      errors("COMPLETE", [step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "activity_1", resource="a"),
                          step(2, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "audit_1", resource="b")]) == [])
check("steps out of canonical order are rejected",
      bool(errors("INCOMPLETE", [Step(1, "VERIFICATION", "s", "r_1", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
                                 Step(2, "FINDING", "s", "r_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1")])))
warn = [p for p in V.validate("c", "INCOMPLETE", [step(1, "FINDING", "REPORT_REMAINING_RECORD", "PRESENT", "")], AUDIT)]
check("a finding with nothing to cite is a warning, not a blocker", any(p.severity == "warning" for p in warn)
      and not any(p.severity == "error" for p in warn))

# ---- Writer and the file check ----
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "output.csv"
    results = [CaseResult("case_3", "COMPLETE", (Step(1, "VERIFICATION", "s", "r,with,commas", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1"),)),
               CaseResult("case_1", "NOT_APPLICABLE"), CaseResult("case_2", "INCOMPLETE", tuple(good))]
    check("the writer writes every row", write_output(results, path) == 3)
    check("rows come out sorted and the file passes its own check", check_file(path, ["case_1", "case_2", "case_3"]) == [])
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    check("commas and quotes inside steps_json survive the CSV round trip", "r,with,commas" in rows[3][2] and rows[1][2] == "[]")
    check("a missing row is detected", any("rows, expected" in p for p in check_file(path, ["case_1", "case_2", "case_3", "case_4"])))
    path.write_text("case_id,deletion_status,steps_json\ncase_2,COMPLETE,[]\ncase_1,COMPLETE,[]\n", encoding="utf-8")
    check("unsorted rows are detected", any("sorted" in p for p in check_file(path, ["case_1", "case_2"])))
    path.write_text("case_id,deletion_status,steps_json\ncase_1,NOT_APPLICABLE,not json\n", encoding="utf-8")
    check("invalid JSON is detected", any("not valid JSON" in p for p in check_file(path, ["case_1"])))
    path.write_text("case_id,deletion_status,steps_json\ncase_1,NOT_APPLICABLE,\"[{}]\"\n", encoding="utf-8")
    check("NOT_APPLICABLE with steps is detected in the file", any("must be []" in p for p in check_file(path, ["case_1"])))
    path.write_text("wrong,header,here\n", encoding="utf-8")
    check("a wrong header is detected", any("header" in p for p in check_file(path, [])))

# ---- Real data: against the labeled samples ----
from evaluate_output import run_samples

results = {r[0]: r for r in run_samples()}
FULLY_DISCOVERABLE = ("case_05", "case_09", "case_11", "case_12", "case_13", "case_14", "case_15", "case_16", "case_17", "case_19")
fields = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
for cid in FULLY_DISCOVERABLE:
    _, want_status, got_status, want, got, _ = results[cid]
    check(f"{cid}: status and every step match the labeled answer exactly, in order",
          got_status == want_status and [tuple(s[f] for f in fields) for s in want] == [tuple(s[f] for f in fields) for s in got])
check("no sample row we write contains a step the labeled answer does not have",
      all({tuple(s[f] for f in ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")) for s in got}
          <= {tuple(s[f] for f in ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")) for s in want}
          for _, _, _, want, got, _ in results.values()))
check("no sample row fails the validator", all(not [p for p in v if p.severity == "error"] for *_, v in results.values()))

print("planner, validator and writer checks passed")
