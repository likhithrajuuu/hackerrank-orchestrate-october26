"""Scores the whole output row against the 32 labeled samples: status, steps, fields and order.

For checking our work only: the labels never drive a prediction. Every labeled sample is a deletion request, so the intent
step is bypassed here (it has its own tests); everything after it is the real pipeline.

    python code/tests/evaluate_output.py            # metrics and every difference
    python code/tests/evaluate_output.py --markdown # the same metrics as Markdown tables, for the report
"""
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable
os.environ.pop("OPENAI_API_KEY", None)                           # never make model calls from an evaluation script
os.environ.pop("LLM_API_KEY", None)

from audit import Auditor
from config import DATASET
from evidence import EvidenceStore
from identity import IdentityIndex
from loaders import load_activity, load_audit_log, load_identities, load_manifest, load_requests, load_rules
from planner import Planner
from policy import PolicyEngine
from state import StateReconstructor, is_modeled
from validator import Validator

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def lcs(a: list, b: list) -> int:
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, x in enumerate(a, 1):
        for j, y in enumerate(b, 1):
            table[i][j] = table[i - 1][j - 1] + 1 if x == y else max(table[i - 1][j], table[i][j - 1])
    return table[-1][-1]


def run_samples():
    """(case_id, label status, our status, label steps, our steps, problems) for each of the 32 samples."""
    store = EvidenceStore.load()
    index = IdentityIndex(load_identities(), load_audit_log())
    claims: dict[str, list] = {}
    for row in load_activity():
        claims.setdefault(row.resource_id, []).append(row)
    reconstructor, engine, auditor, planner = StateReconstructor(store, claims), PolicyEngine(load_rules()), Auditor(), Planner()
    validator = Validator({m.file_id: m.captured_at for m in load_manifest()},
                          {a.activity_id: a.event_time for rows in claims.values() for a in rows},
                          {a.audit_id: a.event_time for a in load_audit_log()})
    answers = {r["case_id"]: r for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8-sig"))}
    out = []
    for request in load_requests("sample_deletion_requests.csv"):
        subject = index.resolve(request.email, request.audit_at)
        tokens = set(subject.emails) | set(subject.hashes) | set(subject.phones) | {subject.user_id}
        ids = {r.resource_id for r in store.for_tokens(tokens, request.audit_at) if r.resource_id and is_modeled(r.resource_id)}
        states = reconstructor.reconstruct(ids, request.audit_at)
        audit = auditor.audit(states, [engine.requirement(s, request.audit_at) for s in states], request.requested_at)
        plan = planner.plan(audit)
        problems = validator.validate(request.case_id, plan.status, plan.steps, request.audit_at)
        label = answers[request.case_id]
        out.append((request.case_id, label["deletion_status"], plan.status, json.loads(label["steps_json"]),
                    [{f: getattr(s, f) for f in ("step_no",) + FIELDS} for s in plan.steps], problems))
    return out


def main(markdown: bool = False) -> int:
    results = run_samples()
    n = len(results)
    status_ok = sum(want == got for _, want, got, *_ in results)
    tp = fp = fn = 0
    field_hits = Counter()
    labeled_steps = 0
    exact_cases = 0
    lcs_total = len_total = 0
    order_ok = 0
    problems = []
    diffs = []
    for cid, want_status, got_status, want, got, validation in results:
        key = lambda s: (s["system_id"], s["resource_id"], s["operation"])
        want_by, got_by = {key(s): s for s in want}, {key(s): s for s in got}
        full = lambda s: tuple(s[f] for f in FIELDS)
        want_set, got_set = {full(s) for s in want}, {full(s) for s in got}
        tp += len(want_set & got_set)
        fp += len(got_set - want_set)
        fn += len(want_set - got_set)
        for k, s in want_by.items():
            labeled_steps += 1
            mine = got_by.get(k)
            for f in FIELDS:
                field_hits[f] += bool(mine) and mine[f] == s[f]
        w_seq, g_seq = [full(s) for s in want], [full(s) for s in got]
        exact_cases += w_seq == g_seq
        lcs_total += lcs(w_seq, g_seq)
        len_total += max(len(w_seq), len(g_seq))
        shared = [k for k in (key(s) for s in want) if k in got_by]
        order_ok += shared == [k for k in (key(s) for s in got) if k in want_by]
        problems += [p for p in validation if p.severity == "error"]
        if want_status != got_status or w_seq != g_seq:
            diffs.append((cid, want_status, got_status, sorted(want_set - got_set), sorted(got_set - want_set)))

    precision, recall = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    field_accuracy = sum(field_hits.values()) / (len(FIELDS) * labeled_steps)
    rows = [
        ("Case status accuracy", f"{status_ok} of {n} ({status_ok / n:.0%})"),
        ("Step-level: exact steps found (recall)", f"{tp} of {tp + fn} ({recall:.0%})"),
        ("Step-level: steps we wrote that are right (precision)", f"{tp} of {tp + fp} ({precision:.0%})"),
        ("Step-level F1", f"{f1:.0%}"),
        ("Field-level accuracy over labeled steps", f"{field_accuracy:.1%} ({sum(field_hits.values())} of {len(FIELDS) * labeled_steps})"),
        ("Sequence: cases whose whole array is identical", f"{exact_cases} of {n} ({exact_cases / n:.0%})"),
        ("Sequence: ordered overlap (LCS of steps)", f"{lcs_total / len_total:.1%}"),
        ("Sequence: cases where shared steps are in the same order", f"{order_ok} of {n}"),
        ("Validator errors on our own rows", str(len(problems))),
    ]
    if markdown:
        print("| Measure | Result |\n|---|---|")
        for k, v in rows:
            print(f"| {k} | {v} |")
        print("\n| Field | Correct on labeled steps |\n|---|---|")
        for f in FIELDS:
            print(f"| `{f}` | {field_hits[f]} of {labeled_steps} ({field_hits[f] / labeled_steps:.0%}) |")
        return 0
    for k, v in rows:
        print(f"{k:58s} {v}")
    print("field accuracy by field:", {f: f"{field_hits[f]}/{labeled_steps}" for f in FIELDS})
    for d in diffs:
        print("DIFF", d[0], "label", d[1], "ours", d[2], "| missing:", len(d[3]), "| extra:", len(d[4]))
    for p in problems:
        print("VALIDATOR ERROR", p.message)
    return 0


if __name__ == "__main__":
    sys.exit(main("--markdown" in sys.argv))
