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


def agent_for(gateway, verify=False):
    return IntentAgent(classify, IntentRouter(), gateway, verify)


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

# 8) The prompts are laid out so the provider can cache their shared prefix (DECISIONS.md D-016).
from agents.chat import SYSTEM as CHAT_SYSTEM
from agents.intent import SYSTEM as INTENT_SYSTEM

MIN_CACHEABLE_CHARS = 4096          # about 1,024 tokens at 4 characters per token: the provider's minimum prefix
check("the intent system prompt is long enough to be cached", len(INTENT_SYSTEM) >= MIN_CACHEABLE_CHARS)
check("the chat system prompt is long enough to be cached", len(CHAT_SYSTEM) >= MIN_CACHEABLE_CHARS)
check("the system prompts are fixed text with no per-request placeholders",
      all("{" not in p and "}" not in p for p in (INTENT_SYSTEM, CHAT_SYSTEM)))
sample_texts = [r.request_text.split(".")[0].strip() for r in load_requests("sample_deletion_requests.csv")]
check("no labeled sample request is copied into a prompt (the examples are synthetic)",
      not any(len(t) > 25 and t in INTENT_SYSTEM for t in sample_texts))

class Recorder:
    """Records the messages a gateway call sends, to prove the customer's text comes last."""
    def __init__(self):
        self.messages = None
    def with_structured_output(self, schema, include_raw=True, method=None):
        return self
    def invoke(self, messages):
        self.messages = messages
        return {"raw": None, "parsed": IntentVerdict(intent="DELETION", confidence="high", reason="t")}

import tempfile as _tmp
from llm.cache import ResponseCache
from llm.config import LLMSettings, ModelSettings
from llm.gateway import LLMGateway
from llm.resilience import ResilientInvoker
from llm.usage import UsageLedger

rec = Recorder()
settings = LLMSettings(None, {ModelRole.PRIMARY: ModelSettings("m", "high", None), ModelRole.ESCALATION: ModelSettings("m2", "low", None)})
tmp = Path(_tmp.mkdtemp())
gateway = LLMGateway(settings, {ModelRole.PRIMARY: rec, ModelRole.ESCALATION: rec},
                     {r: ResilientInvoker(r.value) for r in ModelRole}, ResponseCache(tmp / "c"), UsageLedger(tmp / "u.jsonl"))
from agents.intent import IntentAgent as _IA, IntentRouter as _IR
_IA(classify, _IR(), gateway)({"request": FakeRequest("Please confirm that the profile is gone")})
check("the call sends the fixed system prompt first and the customer's text last",
      rec.messages[0].content == INTENT_SYSTEM and rec.messages[-1].content == "Please confirm that the profile is gone"
      and len(rec.messages) == 2)

# ---- gaps: flags unattributed activity, never changes a status or a step ----
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from agents.gaps import GapAgent

at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
row = lambda rid, minutes: SimpleNamespace(event_type="DELETE_RECORD", resource_id=rid, event_time=at - timedelta(minutes=minutes))
gap_agent = GapAgent({"account_900": [row("account_900", 3)], "account_901": [row("account_901", 40)]})
case = lambda known: {"request": SimpleNamespace(reported_at=at, audit_at=at + timedelta(days=1)),
                      "discovery": SimpleNamespace(resource_ids=known), "flags": ["earlier"]}
check("an unattributed deletion shortly before reported_at is flagged and earlier flags are kept",
      gap_agent(case(()))["flags"] == ["earlier", "discovery:unattributed_activity=1"])
check("a resource kind the case already has is not flagged", "discovery:unattributed_activity=1" not in gap_agent(case(("account_5",)))["flags"])

# ---- second reading: the AI also reads strong rule answers, independently (D-020) ----
class Scripted:
    """Answers per role, so a test can make the primary and the escalation model agree or disagree with the rules."""

    def __init__(self, primary, escalation=None, down=()):
        self.answers, self.down, self.calls = {ModelRole.PRIMARY: primary, ModelRole.ESCALATION: escalation}, down, []

    def structured(self, schema, system, user, version, order=None, accept=None):
        role = order[0]
        self.calls.append((role, user))
        if role in self.down:
            raise LLMUnavailable([f"{role.value}: TransientLLMError"])
        return LLMResult(IntentVerdict(intent=self.answers[role], confidence="high", reason="scripted"), role, role.value, False)


strong_deletion = next(r for r in requests if router.is_strong(classify(r.request_text)) and classify(r.request_text).intent == "DELETION")
ask = lambda gw: agent_for(gw, verify=True)({"request": strong_deletion})["intent"]

gw = Scripted("DELETION")
d = ask(gw)
check("a strong answer the model agrees with is confirmed, with one call and no review flag",
      d.intent == Intent.DELETION and d.source == Source.RULES and not d.needs_review and len(gw.calls) == 1
      and any(x.startswith("confirmed by") for x in d.reasons))
check("the model is not told what the rules said (only the customer's text is sent)", gw.calls[0][1] == strong_deletion.request_text)

gw = Scripted("NON_DELETION", escalation="DELETION")
d = ask(gw)
check("one model disagreeing is settled by a second model, which sides with the rules: rules stand, flagged",
      d.intent == Intent.DELETION and d.needs_review and [r for r, _ in gw.calls] == [ModelRole.PRIMARY, ModelRole.ESCALATION])

d = ask(Scripted("NON_DELETION", escalation="NON_DELETION"))
check("both models against the rules overrides them, and the case is flagged",
      d.intent == Intent.NON_DELETION and d.source == Source.ESCALATION and d.needs_review)

d = ask(Scripted("NON_DELETION", down=(ModelRole.ESCALATION,)))
check("if the tie-breaker is unavailable the rules stand, flagged",
      d.intent == Intent.DELETION and d.source == Source.RULES and d.needs_review)

d = ask(Scripted("DELETION", down=(ModelRole.PRIMARY, ModelRole.ESCALATION)))
check("if no model answers, the rules stand unflagged: a run without a model gives the same answers",
      d.intent == Intent.DELETION and d.source == Source.RULES and not d.needs_review)

gw = Scripted("NON_DELETION")
d = agent_for(gw, verify=False)({"request": strong_deletion})["intent"]
check("with the second reading off, strong answers make no call", not gw.calls and d.source == Source.RULES)

# ---- interpret: the AI reads only records the rules cannot read, with ReAct, and its answer must be grounded (D-021, D-022) ----
import json as _json
from agents.interpret import InterpretAgent, grounded, mask
from contracts import Source as _S, State, StateResult, StateVerdict, ResourceState, Observation, Claim
from llm.react import ReActStep

when = datetime(2026, 9, 1, tzinfo=timezone.utc)
FLOW_TEXT = {"resource_id": "data_flow_9", "email": "ann@example.com", "notes": "Mode: Continuous; Owner: Data Platform; 124 events delivered"}
OTHER_TEXT = {"resource_id": "data_flow_9", "notes": "Paused by operator"}
LATE_TEXT = {"resource_id": "data_flow_9", "notes": "Resumed after audit"}
RECORDS = [SimpleNamespace(file_id="evidence_1", fields=FLOW_TEXT, captured_at=when - timedelta(days=2)),
           SimpleNamespace(file_id="evidence_2", fields=OTHER_TEXT, captured_at=when - timedelta(days=1)),
           SimpleNamespace(file_id="evidence_3", fields=LATE_TEXT, captured_at=when + timedelta(days=1))]
store_stub = SimpleNamespace(for_resource=lambda rid, at: RECORDS)
obs = lambda f, c: Observation("data_flow_9", "integration_service", "DATA_FLOW", State.PRESENT, f, "json", c, None, "row")


def flow(state=State.PRESENT, category="DATA_FLOW", citable=True, claims=()):
    return ResourceState("data_flow_9", "integration_service", category, state, "evidence_1" if citable else "", citable,
                         None, False, tuple(obs(r.file_id, r.captured_at) for r in RECORDS), tuple(claims))


class Verdicts:
    """A scripted ReAct model: per role a list of steps. A plain StateVerdict means 'final answer in one step'."""

    def __init__(self, primary, escalation=None, down=()):
        as_steps = lambda x: x if isinstance(x, list) else [x]
        self.script = {ModelRole.PRIMARY: as_steps(primary), ModelRole.ESCALATION: as_steps(escalation or primary)}
        self.down, self.calls, self.index = down, [], {ModelRole.PRIMARY: 0, ModelRole.ESCALATION: 0}

    def structured(self, schema, system, user, version, order=None, accept=None):
        assert schema is ReActStep
        role = order[0]
        self.calls.append(user)
        if role in self.down:
            raise LLMUnavailable(["down"])
        step = self.script[role][min(self.index[role], len(self.script[role]) - 1)]
        self.index[role] += 1
        if isinstance(step, StateVerdict):
            step = ReActStep(thought="the record is enough", action="final", input=_json.dumps(step.model_dump()))
        return LLMResult(step, role, role.value, False)


def verdict(state="ACTIVE", confidence="high", quote="Mode: Continuous; 124 events delivered"):
    return StateVerdict(state=state, confidence=confidence, quote=quote, reason="r")


act = lambda action, text="": ReActStep(thought="look", action=action, input=text)


def interpret(gw, rs=None):
    case = {"request": SimpleNamespace(audit_at=when), "states": StateResult((rs or flow(),)), "flags": ["earlier"]}
    return InterpretAgent(store_stub, gw)(case)


check("mask hides emails, hashes and phone numbers but keeps the behavior",
      mask("a@b.co 33f407a3689b1b57bdb15b492e565867a507c27ce066c1d6a584f03f4dbb5565 +34 668 324 642 | 2026-04-26 1,284 events")
      == "<email> <hash> <phone> | 2026-04-26 1,284 events")
check("a quote stitched from fragments of the record is grounded; invented words are not",
      grounded("Mode: Continuous; 124 events delivered", "Mode: Continuous; Owner: Data Platform; 124 events delivered")
      and not grounded("Mode: Continuous; 900 events", "Mode: Continuous; 124 events delivered") and not grounded("", "x"))

gw = Verdicts(verdict())
out = interpret(gw)
check("two models agreeing with a grounded quote settles the state, and the case is flagged",
      out["states"].states[0].state == State.ACTIVE and out["interpretations"][0].after == "ACTIVE"
      and out["flags"] == ["earlier", "state:interpreted_by_ai=1"] and len(gw.calls) == 2)
check("the model is sent the masked record, never the email", all("ann@example.com" not in c and "<email>" in c for c in gw.calls))
check("the audit trace of each model's Thought / Action is kept on the result",
      "Thought: the record is enough" in out["interpretations"][0].trace and out["interpretations"][0].trace.count("[") == 2)
check("with no model nothing changes", interpret(None) == {})
check("a state the rules already read makes no call", not (g2 := Verdicts(verdict())).calls and interpret(g2, flow(State.ACTIVE)) == {} and not g2.calls)
check("a record that cannot be cited is not interpreted", interpret(Verdicts(verdict()), flow(citable=False)) == {})
check("a category with no question is not interpreted", interpret(Verdicts(verdict()), flow(category="USER_PROFILE")) == {})
check("a quote that is not in any record it saw is rejected", interpret(Verdicts(verdict(quote="Mode: Paused"))) == {})
check("the two models disagreeing leaves the state alone", interpret(Verdicts(verdict(), verdict("DISABLED", quote="Mode: Continuous"))) == {})
check("CANNOT_TELL leaves the state alone", interpret(Verdicts(verdict("CANNOT_TELL", quote=""))) == {})
check("low confidence leaves the state alone", interpret(Verdicts(verdict(confidence="low"))) == {})
check("a state that does not fit the category is rejected", interpret(Verdicts(verdict("RESTORABLE"))) == {})
check("if the second model is unavailable nothing changes", interpret(Verdicts(verdict(), down=(ModelRole.ESCALATION,))) == {})

# ReAct: the model looks at other evidence before answering
dig = [act("list_evidence"), act("read_record", "evidence_2"), verdict("DISABLED", quote="Paused by operator")]
gw = Verdicts(dig)
out = interpret(gw)
check("a model can read another evidence file first, and the state and the citation follow the file the quote came from",
      out["states"].states[0].state == State.DISABLED and out["states"].states[0].evidence_id == "evidence_2"
      and out["interpretations"][0].evidence_id == "evidence_2" and len(gw.calls) == 6)
check("list_evidence shows what can and cannot be cited at audit_at",
      "evidence_3" in gw.calls[1] and "cannot be cited" in gw.calls[1])
check("a quote from a file captured after audit_at is refused",
      interpret(Verdicts([act("read_record", "evidence_3"), verdict("ACTIVE", quote="Resumed after audit")])) == {})
claim = Claim("activity_9", "SYNC_RECORD", "SUCCESS", when, "Sync worker reported success")
check("activity-log claims are shown but can never be the quoted evidence",
      interpret(Verdicts([act("activity_claims"), verdict("ACTIVE", quote="Sync worker reported success")]), flow(claims=(claim,))) == {})
gw = Verdicts([act("activity_claims"), verdict()])
interpret(gw, flow(claims=(claim,)))
check("claims are labeled as claims, not proof", "CLAIMS (what a worker reported, not proof)" in gw.calls[1])
check("a model that only keeps calling tools gives up without changing the state",
      interpret(Verdicts([act("list_evidence"), act("what_to_decide"), act("activity_claims"), act("read_record", "evidence_2"),
                          act("read_record", "evidence_1")])) == {})

print("agent and graph checks passed")
