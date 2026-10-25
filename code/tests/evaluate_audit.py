"""Scores stages 5 and 6 against the 32 labeled samples. For checking our work only: labels never drive a prediction.

Compares, per case: the status, every finding, every proposed remediation, and every verification the labeled answer
contains, with what the audit produced from evidence alone. It also lists what we invent (a finding the labels lack)
and what we miss because discovery never found the resource.

    python code/tests/evaluate_audit.py
"""
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable
os.environ.pop("OPENAI_API_KEY", None)                           # never make vision calls from an evaluation script
os.environ.pop("LLM_API_KEY", None)

from audit import Auditor
from config import DATASET
from contracts import Verdict
from evidence import EvidenceStore
from identity import IdentityIndex
from loaders import load_activity, load_audit_log, load_identities, load_requests, load_rules
from policy import PolicyEngine
from state import StateReconstructor, is_modeled


def main() -> int:
    store = EvidenceStore.load()
    index = IdentityIndex(load_identities(), load_audit_log())
    claims: dict[str, list] = {}
    for row in load_activity():
        claims.setdefault(row.resource_id, []).append(row)
    reconstructor, engine, auditor = StateReconstructor(store, claims), PolicyEngine(load_rules()), Auditor()
    answers = {r["case_id"]: r for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8-sig"))}

    status_ok = 0
    totals = Counter()
    problems = []
    status_rows = []
    for request in load_requests("sample_deletion_requests.csv"):
        subject = index.resolve(request.email, request.audit_at)
        tokens = set(subject.emails) | set(subject.hashes) | set(subject.phones) | {subject.user_id}
        discovered = {r.resource_id for r in store.for_tokens(tokens, request.audit_at) if r.resource_id and is_modeled(r.resource_id)}
        states = reconstructor.reconstruct(discovered, request.audit_at)
        requirements = [engine.requirement(s, request.audit_at) for s in states]
        audit = auditor.audit(states, requirements, request.requested_at)
        ours = {a.resource_id: a for a in audit.assessments}

        label = answers[request.case_id]
        steps = json.loads(label["steps_json"])
        want = {phase: {} for phase in ("FINDING", "REMEDIATION", "VERIFICATION")}
        for step in steps:
            want[step["phase"]][step["resource_id"]] = step["operation"]

        status_ok += audit.status == label["deletion_status"]
        status_rows.append((request.case_id, label["deletion_status"], audit.status))

        # findings and fixes: only a violation produces one
        for phase, attr in (("FINDING", "finding_op"), ("REMEDIATION", "remediation_op")):
            for rid, op in want[phase].items():
                totals[f"{phase} labeled"] += 1
                if rid not in ours:
                    totals[f"{phase} missed (resource never discovered)"] += 1
                elif getattr(ours[rid], attr) == op:
                    totals[f"{phase} correct"] += 1
                else:
                    totals[f"{phase} wrong"] += 1
                    problems.append((request.case_id, phase, rid, "label", op, "ours", getattr(ours[rid], attr), ours[rid].reason[:70]))
            for rid, a in ours.items():
                if getattr(a, attr) and rid not in want[phase]:
                    totals[f"{phase} invented"] += 1
                    problems.append((request.case_id, phase, rid, "INVENTED", getattr(a, attr), a.reason[:70]))

        # verifications: every assessed (non-stale) resource gets one
        for rid, op in want["VERIFICATION"].items():
            totals["VERIFICATION labeled"] += 1
            if rid not in ours:
                totals["VERIFICATION missed (resource never discovered)"] += 1
            elif ours[rid].verdict == Verdict.STALE:
                totals["VERIFICATION wrongly stale"] += 1
                problems.append((request.case_id, "VERIFICATION", rid, "labeled resource marked STALE", ours[rid].reason[:80]))
            elif ours[rid].verification_op == op:
                totals["VERIFICATION correct"] += 1
            else:
                totals["VERIFICATION wrong"] += 1
                problems.append((request.case_id, "VERIFICATION", rid, "label", op, "ours", ours[rid].verification_op, ours[rid].state.value))
        for rid, a in ours.items():
            if a.verification_op and rid not in want["VERIFICATION"]:
                totals["VERIFICATION invented"] += 1
                problems.append((request.case_id, "VERIFICATION", rid, "INVENTED", a.verification_op, a.verdict.value))
        totals["stale resources"] += sum(a.verdict == Verdict.STALE for a in ours.values())
        totals["unresolved resources"] += sum(a.verdict == Verdict.UNRESOLVED for a in ours.values())

    print(f"case status agrees with the label : {status_ok} of {len(status_rows)}")
    wrong = [r for r in status_rows if r[1] != r[2]]
    if wrong:
        print("  status differences (case, label, ours):", wrong)
    for key in sorted(totals):
        print(f"  {key:52s} {totals[key]}")
    for p in problems:
        print("PROBLEM", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
