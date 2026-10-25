"""The order requests arrive in, and how many workers run them, must never change an answer.

The grader compares arrays in order, so two runs that are semantically the same but ordered differently would score
differently. This runs the same cases in different orders and with different worker counts and demands identical results.
"""
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable
os.environ.pop("OPENAI_API_KEY", None)                           # no live calls from a test
os.environ.pop("LLM_API_KEY", None)

from agents.audit import AuditAgent
from agents.discovery import DiscoveryAgent
from agents.plan import PlannerAgent
from agents.identity import IdentityAgent
from agents.policy import PolicyAgent
from planner import Planner
from agents.state import StateAgent
from audit import Auditor
from contracts import Intent, IntentDecision, Observation, ResourceState, Source, State
from evidence import EvidenceStore
from graph import build_graph
from identity import IdentityIndex
from loaders import load_activity, load_audit_log, load_identities, load_requests, load_rules
from policy import PolicyEngine
from state import StateReconstructor, reduce_resource


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


# 1) Reducing observations does not depend on their order.
T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
AUDIT = T0 + timedelta(days=1)
obs = [Observation("account_1", "account_service", "USER_PROFILE", st, f"evidence_{i}", "log", T0, None, f"src{i}")
       for i, st in enumerate([State.PRESENT, State.ABSENT, State.PRESENT, State.ABSENT, State.PRESENT])]
rng = random.Random(7)
baseline = reduce_resource("account_1", obs, (), AUDIT)
same = True
for _ in range(25):
    shuffled = obs[:]
    rng.shuffle(shuffled)
    same &= reduce_resource("account_1", shuffled, (), AUDIT) == baseline
check("25 random orderings of the same observations reduce to the identical state", same)

# 2) The audit does not depend on the order resources arrive in.
engine, auditor = PolicyEngine(load_rules()), Auditor()


def resource(rid, category, state):
    o = Observation(rid, "sys", category, state, "evidence_1", "log", T0, None, "src")
    return ResourceState(rid, f"sys_{category}", category, state, "evidence_1", True, None, False, (o,), ())


resources = [resource("account_1", "USER_PROFILE", State.ABSENT), resource("search_1", "SEARCH_INDEX_ENTRY", State.PRESENT),
             resource("data_flow_1", "DATA_FLOW", State.ACTIVE), resource("backup_1", "BACKUP_ARCHIVE", State.RESTORABLE),
             resource("support_1", "SUPPORT_MESSAGE", State.EXPOSED), resource("audit_record_1", "SECURITY_AUDIT_LOG", State.PRESENT)]
first = auditor.audit(resources, [engine.requirement(r, AUDIT) for r in resources], T0 - timedelta(days=2))
same = True
for _ in range(25):
    shuffled = resources[:]
    rng.shuffle(shuffled)
    same &= auditor.audit(shuffled, [engine.requirement(r, AUDIT) for r in shuffled], T0 - timedelta(days=2)) == first
check("25 random orderings of the same resources give the identical audit", same)
check("the assessments are in a fixed canonical order", [a.resource_id for a in first.assessments] ==
      sorted([a.resource_id for a in first.assessments], key=lambda r: next((x.system_id, x.resource_id) for x in first.assessments if x.resource_id == r)))

# 3) The whole flow: request order and worker count change nothing.
store = EvidenceStore.load()
claims = {}
for row in load_activity():
    claims.setdefault(row.resource_id, []).append(row)
index = IdentityIndex(load_identities(), load_audit_log())


def always_deletion(state):          # isolates the pipeline from the intent step, which has its own tests
    return {"intent": IntentDecision(Intent.DELETION, Source.RULES, False, ("test",))}


graph = build_graph(always_deletion, IdentityAgent(index), DiscoveryAgent(store),
                    StateAgent(StateReconstructor(store, claims)), PolicyAgent(engine), AuditAgent(auditor),
                    PlannerAgent(Planner()))
requests = load_requests("sample_deletion_requests.csv") + load_requests()[:60]


def signature(final):
    audit = final["audit"]
    return (final["request"].case_id, final["result"].status, final["result"].steps,
            tuple((a.system_id, a.resource_id, a.verdict.value, a.finding_op, a.remediation_op, a.verification_op, a.evidence_id)
                  for a in audit.assessments))


def run(order, workers):
    finals = graph.batch([{"request": r} for r in order], config={"max_concurrency": workers})
    return {sig[0]: sig for sig in map(signature, finals)}


reference = run(requests, 1)
check(f"{len(reference)} cases ran", len(reference) == len(requests))
reordered = run(list(reversed(requests)), 1)
check("reversing the request order changes no result", reordered == reference)
shuffled = requests[:]
random.Random(3).shuffle(shuffled)
check("shuffling the request order changes no result", run(shuffled, 1) == reference)
check("running with 8 workers instead of 1 changes no result", run(requests, 8) == reference)
shuffled_again = requests[:]
random.Random(11).shuffle(shuffled_again)
check("a different shuffle with 8 workers still changes no result", run(shuffled_again, 8) == reference)

print("determinism checks passed")
