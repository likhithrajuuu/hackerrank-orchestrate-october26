import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable

from agents.audit import AuditAgent
from agents.discovery import DiscoveryAgent
from agents.identity import IdentityAgent
from agents.intent import IntentAgent, IntentRouter
from agents.policy import PolicyAgent
from agents.state import StateAgent
from audit import Auditor
from classifier import classify
from contracts import Intent, IntentVerdict, Source, State, Verdict
from evidence import EvidenceStore
from graph import build_graph
from identity import IdentityIndex
from llm.config import ModelRole
from llm.gateway import LLMResult, LLMUnavailable
from loaders import load_activity, load_audit_log, load_identities, load_requests, load_rules
from policy import PolicyEngine
from state import StateReconstructor


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


NEGATION_WORDS = ("not asking", "not a request", "do not", "don't", "without")


class StubGateway:
    """Answers like a sensible model would for the routed texts, and records every call."""

    def __init__(self, role=ModelRole.PRIMARY, confidence="high", unavailable=False):
        self.calls, self.role, self.confidence, self.unavailable = [], role, confidence, unavailable

    def structured(self, schema, system, user, version, order=None, accept=None):
        self.calls.append(user)
        if self.unavailable:
            raise LLMUnavailable(["primary: TransientLLMError", "escalation: TransientLLMError"])
        intent = "NON_DELETION" if any(w in user.lower() for w in NEGATION_WORDS) else "DELETION"
        return LLMResult(IntentVerdict(intent=intent, confidence=self.confidence, reason="stub"),
                         self.role, "stub-model", False)


class FakeRequest:
    def __init__(self, text):
        self.request_text = text


def agent_for(gateway):
    return IntentAgent(classify, IntentRouter(), gateway)


requests = load_requests()
router = IntentRouter()

# 1) Routing volume on the real data.
routed = [r for r in requests if not router.is_strong(classify(r.request_text))]
strong = [r for r in requests if router.is_strong(classify(r.request_text))]
check("490 requests are strong, 28 are routed to the AI",
      len(strong) == 490 and len(routed) == 28)
check("the routed requests are 23 distinct texts", len({r.request_text for r in routed}) == 23)

# 2) Strong intents never reach the model.
gateway = StubGateway()
agent = agent_for(gateway)
decisions = [agent({"request": r})["intent"] for r in strong]
check("strong intents never call the model", not gateway.calls)
check("strong intents are answered by rules", all(d.source == Source.RULES for d in decisions))

# 3) Weak and ambiguous intents are answered by the model.
decisions = [agent({"request": r})["intent"] for r in routed]
check("weak and ambiguous intents call the model once each", len(gateway.calls) == 28)
check("their source is the primary model", all(d.source == Source.PRIMARY for d in decisions))

# 4) End to end over all 518: the hybrid reproduces the expected 508 / 10 split.
gateway = StubGateway()
agent = agent_for(gateway)
labels = Counter(agent({"request": r})["intent"].intent for r in requests)
check("hybrid gives 508 deletion and 10 non-deletion", labels == {Intent.DELETION: 508, Intent.NON_DELETION: 10})

# 5) Escalation, low confidence, and failure paths.
d = agent_for(StubGateway(role=ModelRole.ESCALATION))({"request": routed[0]})["intent"]
check("an answer from the escalation model is labelled as such", d.source == Source.ESCALATION)
d = agent_for(StubGateway(confidence="low"))({"request": routed[0]})["intent"]
check("a low-confidence answer is flagged for review", d.needs_review)
d = agent_for(StubGateway(unavailable=True))({"request": routed[0]})["intent"]
check("models unavailable: safe default, flagged",
      d.source == Source.FALLBACK and d.intent == Intent.NON_DELETION and d.needs_review
      and d.reasons[0] == "models unavailable")
d = agent_for(None)({"request": routed[0]})["intent"]
check("no model configured: safe default, flagged",
      d.source == Source.FALLBACK and d.intent == Intent.NON_DELETION and d.needs_review)
d = agent_for(None)({"request": strong[0]})["intent"]
check("no model configured: strong intents are unaffected", d.source == Source.RULES and not d.needs_review)

# 6) The whole graph.
index = IdentityIndex(load_identities(), load_audit_log())
store = EvidenceStore.load()
graph = build_graph(agent_for(StubGateway()), IdentityAgent(index), DiscoveryAgent(store))

non_deletion = next(r for r in requests if "out of date" in r.request_text)
final = graph.invoke({"request": non_deletion})
check("a non-deletion request short-circuits to NOT_APPLICABLE",
      final["result"].status == "NOT_APPLICABLE" and "subject" not in final and "discovery" not in final)

sample10 = next(r for r in load_requests("sample_deletion_requests.csv") if r.case_id == "case_10")
final = graph.invoke({"request": sample10})
check("a deletion request flows through identity and discovery", final["subject"].user_id == "user_10")
check("case_10 discovery reaches support_002 through the trace join", "support_002" in final["discovery"].resource_ids)
check("until the planner exists, a deletion case is UNDECIDED", final["result"].status == "UNDECIDED")

weak = next(r for r in routed if classify(r.request_text).intent == "DELETION")
final = graph.invoke({"request": weak})
check("a weak-intent deletion request is routed to the model and continues", final["intent"].source == Source.PRIMARY
      and final["result"].status == "UNDECIDED")

# 6b) Stage 4 inside the graph.
claims = {}
for row in load_activity():
    claims.setdefault(row.resource_id, []).append(row)
graph_with_state = build_graph(agent_for(StubGateway()), IdentityAgent(index), DiscoveryAgent(store),
                               StateAgent(StateReconstructor(store, claims)))
final = graph_with_state.invoke({"request": sample10})
support = final["states"].get("support_002")
check("stage 4 runs in the graph: case_10's support record is PRESENT, cited, with its DELETE claim kept",
      support.state == State.PRESENT and support.citable and any(c.event_type == "DELETE_RECORD" for c in support.claims))
final = graph_with_state.invoke({"request": non_deletion})
check("a non-deletion request never reaches the state agent", "states" not in final)

# 6c) The whole flow, stages 4 to 6, decides a status.
full = build_graph(agent_for(StubGateway()), IdentityAgent(index), DiscoveryAgent(store),
                   StateAgent(StateReconstructor(store, claims)), PolicyAgent(PolicyEngine(load_rules())), AuditAgent(Auditor()))
samples_by_id = {r.case_id: r for r in load_requests("sample_deletion_requests.csv")}
final = full.invoke({"request": samples_by_id["case_14"]})
check("case_14 flows through to INCOMPLETE because of the restorable backup",
      final["result"].status == "INCOMPLETE" and [a.resource_id for a in final["audit"].of(Verdict.VIOLATION)] == ["backup_3870"])
final = full.invoke({"request": samples_by_id["case_26"]})
check("case_26 flows through to COMPLETE", final["result"].status == "COMPLETE")
final = full.invoke({"request": non_deletion})
check("a non-deletion request still stops at NOT_APPLICABLE", final["result"].status == "NOT_APPLICABLE" and "audit" not in final)

# 7) Batch execution keeps order and results line up with their requests.
batch = requests[:3] + [non_deletion] + routed[:2]
finals = graph.batch([{"request": r} for r in batch], config={"max_concurrency": 3})
check("batch returns one result per request, in order",
      [f["result"].case_id for f in finals] == [r.case_id for r in batch])

print("agent and graph checks passed")
