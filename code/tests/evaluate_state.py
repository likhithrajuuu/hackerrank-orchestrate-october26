"""Scores stage 4 against the 32 labeled samples. For checking our work only: labels are never used to predict.

For each sample, the labeled steps imply a state for every resource they mention (a REPORT_ACTIVE_DATA_FLOW finding
means the flow is ACTIVE, a VERIFY_RECORD_ABSENT step means the record is ABSENT, and so on). We compare the state
our evidence reading produced, and the evidence id we would cite, with that.
"""
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("OPENAI_API_KEY", None)                           
os.environ.pop("LLM_API_KEY", None)

from config import DATASET
from contracts import State
from evidence import EvidenceStore
from identity import IdentityIndex
from loaders import load_activity, load_audit_log, load_identities, load_requests
from state import StateReconstructor, is_modeled

FINDINGS = [("REPORT_EXPOSED_PII", {State.EXPOSED}), ("REPORT_ACTIVE_DATA_FLOW", {State.ACTIVE}),
            ("REPORT_RESTORE_RISK", {State.RESTORABLE}), ("REPORT_REMAINING_RECORD", {State.PRESENT, State.EXPOSED})]
VERIFICATIONS = [("VERIFY_RECORD_ABSENT", {State.ABSENT}), ("VERIFY_RECORD_PRESENT", {State.PRESENT}),
                 ("VERIFY_RECORD_REDACTED", {State.DEIDENTIFIED}), ("VERIFY_RECORD_ANONYMIZED", {State.DEIDENTIFIED}),
                 ("VERIFY_DATA_FLOW_DISABLED", {State.DISABLED}), ("VERIFY_RESTORE_BLOCKED", {State.RESTORE_BLOCKED})]


def implied_state(operations: set[str]) -> set[State] | None:
    for operation, states in FINDINGS + VERIFICATIONS:
        if operation in operations:
            return states
    return None


def main() -> int:
    store = EvidenceStore.load()
    index = IdentityIndex(load_identities(), load_audit_log())
    claims: dict[str, list] = {}
    for row in load_activity():
        claims.setdefault(row.resource_id, []).append(row)
    reconstructor = StateReconstructor(store, claims)
    answers = {r["case_id"]: r for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8-sig"))}

    total = found = observed = match = 0
    evidence_total = evidence_match = 0
    mismatches, extras, unknown, evidence_mismatches = [], [], [], []
    by_category = Counter()
    by_category_ok = Counter()
    for request in load_requests("sample_deletion_requests.csv"):
        subject = index.resolve(request.email, request.audit_at)
        tokens = set(subject.emails) | set(subject.hashes) | set(subject.phones) | {subject.user_id}
        discovered = {r.resource_id for r in store.for_tokens(tokens, request.audit_at) if r.resource_id and is_modeled(r.resource_id)}
        states = {s.resource_id: s for s in reconstructor.reconstruct(discovered, request.audit_at)}
        steps = json.loads(answers[request.case_id]["steps_json"])
        operations: dict[str, set] = {}
        finding_evidence: dict[str, str] = {}
        for step in steps:
            operations.setdefault(step["resource_id"], set()).add(step["operation"])
            if step["evidence_id"]:
                finding_evidence[step["resource_id"]] = step["evidence_id"]

        for rid, ops in operations.items():
            expected = implied_state(ops)
            total += 1
            if rid not in states:
                continue
            found += 1
            got = states[rid]
            if got.state == State.UNKNOWN:
                unknown.append((request.case_id, rid))
                continue
            observed += 1
            by_category[got.category] += 1
            if got.state in expected:
                match += 1
                by_category_ok[got.category] += 1
            else:
                mismatches.append((request.case_id, rid, got.category, sorted(s.value for s in expected), got.state.value,
                                   [(o.file_id, o.source, o.state.value) for o in got.observations[-3:]]))
            if rid in finding_evidence:
                evidence_total += 1
                evidence_match += got.evidence_id == finding_evidence[rid]
                if got.evidence_id != finding_evidence[rid]:
                    evidence_mismatches.append((request.case_id, rid, "label", finding_evidence[rid], "ours", got.evidence_id or "(none)",
                                                [(o.file_id, o.source, o.state.value) for o in got.observations]))
        for rid in sorted(set(states) - set(operations)):
            extras.append((request.case_id, rid, states[rid].state.value))

    print(f"resources the labeled answers mention : {total}")
    print(f"  discovered by our evidence search   : {found}")
    print(f"  with a usable observation           : {observed}   (discovered but no readable state: {len(unknown)})")
    print(f"  observed state agrees with label    : {match} of {observed}  ({match / max(observed, 1):.0%})")
    print(f"  cited evidence id agrees with label : {evidence_match} of {evidence_total}")
    print(f"discovered but not in the labeled answer: {len(extras)} {extras}")
    print("agreement by category:", {c: f"{by_category_ok[c]}/{n}" for c, n in sorted(by_category.items())})
    if unknown:
        print("no readable state:", unknown)
    for m in mismatches:
        print("MISMATCH", m)
    for m in evidence_mismatches:
        print("EVIDENCE-ID MISMATCH", m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
