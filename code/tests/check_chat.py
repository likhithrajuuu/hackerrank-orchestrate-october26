import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from datetime import timedelta

from agents.chat import ChatAgent
from agents.chat_tools import make_tools
from contracts import CaseResult, DiscoveryResult, Intent, IntentDecision, Source
from evidence import EvidenceRecord
from llm.config import LLMSettings, ModelRole, ModelSettings
from llm.gateway import LLMResult, LLMUnavailable
from llm.react import ReActStep
from llm.resilience import CircuitBreaker, PermanentLLMError, ResilientInvoker, RetryPolicy
from llm.usage import UsageLedger
from loaders import load_requests


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


class HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class Gateway:
    """A scripted ReAct model for the chat. Per role a list of steps; an Exception in the list is raised instead."""

    def __init__(self, primary, escalation=None):
        self.script = {ModelRole.PRIMARY: list(primary), ModelRole.ESCALATION: list(escalation or [])}
        self.seen, self.systems, self.roles = [], [], []

    def structured(self, schema, system, user, version, order=None, accept=None):
        role = order[0]
        self.seen.append(user)
        self.systems.append(system)
        self.roles.append(role)
        queue = self.script[role]
        if not queue:
            raise LLMUnavailable([f"{role.value}: nothing scripted"])
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return LLMResult(item, role, role.value, False)


step = lambda action, text="", thought="next": ReActStep(thought=thought, action=action, input=text)
say = lambda text: step("final", text, "I can answer now")


def echo_tool(x: str) -> str:
    """Echo the input."""
    return f"echo:{x}"


def broken_tool(x: str) -> str:
    """Always fails."""
    raise RuntimeError("boom")


TOOLS = [StructuredTool.from_function(echo_tool), StructuredTool.from_function(broken_tool)]
agent_for = lambda gw, max_steps=6: ChatAgent(gw, TOOLS, max_steps)

# 1) The model reasons, asks for a tool, the tool runs, and the model answers using its result.
gw = Gateway([step("echo_tool", '{"x": "hi"}', "I should echo hi"), say("done")])
agent = agent_for(gw)
check("thought, action, observation, then the final answer", agent.ask([], "say hi") == "done")
check("the tool result is fed back to the model as an observation", "Observation: echo:hi" in gw.seen[1])
check("the system prompt comes first and states the read-only rule", "only read" in gw.systems[0] and "ReAct" in gw.systems[0])
check("the tools are described to the model, with the fields they take", "- echo_tool:" in gw.systems[0] and "fields: x" in gw.systems[0])
check("the Thought / Action / Observation of the answer are kept", [t.action for t in agent.last_trace] == ["echo_tool", "final"]
      and agent.last_trace[0].thought == "I should echo hi" and agent.last_trace[0].observation == "echo:hi")

# 2) A failing tool, bad input or an unknown tool does not end the conversation.
gw = Gateway([step("broken_tool", '{"x": "a"}'), step("nope", "{}"), step("echo_tool", "not json"), say("recovered")])
agent = agent_for(gw)
check("failures are reported back, not raised", agent.ask([], "q") == "recovered")
obs = [t.observation for t in agent.last_trace]
check("the failure, the unknown tool and the bad input are visible to the model",
      obs[0].startswith("tool error: RuntimeError") and "unknown action" in obs[1] and obs[2].startswith("tool error"))

# 3) A model that never stops calling tools is cut off.
gw = Gateway([step("echo_tool", f'{{"x": "{i}"}}') for i in range(20)])
agent = agent_for(gw, max_steps=3)
check("endless tool calls stop at max_steps", "stopped after 3 tool steps" in agent.ask([], "loop") and len(gw.seen) == 3)

# 4) Earlier turns are passed to the model.
gw = Gateway([say("ok")])
agent_for(gw).ask([HumanMessage("earlier question"), AIMessage("earlier answer")], "follow-up")
check("history is included before the new question",
      "User: earlier question" in gw.seen[0] and "Assistant: earlier answer" in gw.seen[0] and "The user's message:\nfollow-up" in gw.seen[0] and gw.seen[0].index("earlier question") < gw.seen[0].index("follow-up"))

# 5) Failure paths: fall back to the escalation model; both down; permanent errors stop the run.
gw = Gateway([LLMUnavailable(["primary down"])], [say("from sol")])
check("primary down: the escalation model answers", agent_for(gw).ask([], "q") == "from sol" and gw.roles == [ModelRole.PRIMARY, ModelRole.ESCALATION])

try:
    agent_for(Gateway([LLMUnavailable(["a"])], [LLMUnavailable(["b"])])).ask([], "q")
    check("both down raises LLMUnavailable", False)
except LLMUnavailable as exc:
    check("both models down: LLMUnavailable names both", exc.errors == ["a", "b"])

try:
    agent_for(Gateway([PermanentLLMError("401")], [say("never")])).ask([], "q")
    check("401 propagates", False)
except PermanentLLMError:
    check("a permanent error is not swallowed by the fallback", True)

# 5b) Hallucination guard: invented ids are flagged, ids a tool returned are not.
agent = agent_for(Gateway([step("echo_tool", '{"x": "account_123"}'), say("Resource account_123 is fine.")]))
check("an id that a tool returned is not flagged", "[unverified]" not in agent.ask([], "check it"))

reply = agent_for(Gateway([say("The leftover is search_9999 and evidence_77.")])).ask([], "what is left?")
check("invented ids are flagged by name", "[unverified]" in reply and "search_9999" in reply and "evidence_77" in reply)

check("an id the user wrote is not flagged",
      "[unverified]" not in agent_for(Gateway([say("You asked about case_14, which I did not look up.")])).ask([], "tell me about case_14"))

agent = agent_for(Gateway([step("echo_tool", '{"x": "backup_55"}'), say("First answer about backup_55."), say("Still backup_55, as before.")]))
first = agent.ask([], "look at it")
second = agent.ask([HumanMessage("look at it"), AIMessage(first)], "and again?")
check("an id verified earlier in the session stays verified", "[unverified]" not in second)

check("system names such as account_service are not mistaken for ids",
      "[unverified]" not in agent_for(Gateway([say("The service names account_service and search_service are not ids.")])).ask([], "q"))

# 6) The real tools, with a stubbed case graph so no evidence is loaded and no model is called.
samples = {r.case_id: r for r in load_requests("sample_deletion_requests.csv")}


class StubGraph:
    def __init__(self, resource_ids):
        self.resource_ids = resource_ids

    def invoke(self, state):
        request = state["request"]
        return {"request": request,
                "intent": IntentDecision(Intent.DELETION, Source.RULES, False, ("strong:test",)),
                "discovery": DiscoveryResult((), tuple(self.resource_ids), ()),
                "result": CaseResult(request.case_id, "UNDECIDED")}


class StubStore:
    """Just the part of EvidenceStore that resource_evidence uses."""

    def __init__(self, records):
        self.records = records

    def for_resource(self, resource_id, audit_at):
        return [r for r in self.records if r.resource_id == resource_id]

    @staticmethod
    def citable(record, audit_at):
        return record.captured_at <= audit_at


audit_18 = samples["case_18"].audit_at
LOG_LINE = EvidenceRecord("evidence_94", "log", audit_18 - timedelta(minutes=30), audit_18 - timedelta(hours=1),
                          "search_4013", "search_service", "index_query_completed",
                          {"raw": "event=index_query_completed resource_id=search_4013 result=hit total_hits=1"}, 1)
LATE_LINE = EvidenceRecord("evidence_99", "log", audit_18 + timedelta(days=1), None, "search_4013", None, None,
                           {"raw": "exported after the audit"}, 2)
FOUND = {"case_14": ("account_3677", "backup_3870", "audit_record_987"),
         "case_18": ("analytics_037", "data_flow_4890", "search_4013", "support_5569")}
active = {"graph": None}


def make(case_id):
    active["graph"] = StubGraph(FOUND[case_id])
    tools = {t.name: t for t in make_tools(samples, lambda: active["graph"], lambda: StubStore([LOG_LINE, LATE_LINE]), None,
                                           UsageLedger(Path(tempfile.mkdtemp()) / "u.jsonl"), {})}
    return tools


tools = make("case_14")
text = tools["compare_with_sample"].invoke({"case_id": "case_14"})
rows = [line.strip() for line in text.splitlines()]
check("case_14: every expected resource is found",
      sum(r.startswith("FOUND") for r in rows) == 3 and not any(r.startswith("MISSING") for r in rows))
check("case_14: the labeled status is shown and marked evaluation-only",
      "EVALUATION ONLY" in text and "expected status INCOMPLETE" in text)

tools = make("case_18")
text = tools["compare_with_sample"].invoke({"case_id": "case_18"})
flat = " ".join(text.split())
check("case_18: the two undiscoverable resources are reported MISSING",
      "MISSING account_service account_5521" in flat and "MISSING security_service audit_record_6645" in flat)
check("case_18: the cited evidence is shown", "evidence_94" in text)
check("a non-sample case is refused politely", "not a labeled sample" in tools["compare_with_sample"].invoke({"case_id": "case_99999"}))
check("explain_case rejects an unknown id", "unknown case_id" in tools["explain_case"].invoke({"case_id": "nope"}))
text = tools["resource_evidence"].invoke({"case_id": "case_18", "resource_id": "search_4013"})
check("resource_evidence shows the activity claim from the real log (a FAILED delete)",
      "activity-log CLAIMS" in text and "DELETE_RECORD -> FAILED" in text)
check("resource_evidence shows the evidence line that proves the record is still there",
      "EVIDENCE (actual state)" in text and "result=hit" in text and "[citable]" in text)
check("evidence from a file captured after audit_at is marked not citable", "NOT citable" in text)
check("resource_evidence rejects an unknown case", "unknown case_id" in tools["resource_evidence"].invoke(
    {"case_id": "nope", "resource_id": "search_4013"}))
policy = tools["deletion_policy"].invoke({})
check("the policy tool lists every rule and says retained records are not deleted",
      policy.count("rule_") >= 15 and "BACKUP_ARCHIVE: RETAIN" in policy and "must NOT be deleted" in policy)
check("every tool has a description for the model", all(t.description for t in tools.values()))

# 7) Web search (Exa) is chat-only background: masked queries, labeled results, no key leakage, no part in the audit (D-024).
import io
import json as _json
import urllib.error
from websearch import ExaSearch, WebSearchError, render as render_web

SECRET = "exa-secret-key-123"
sent = {}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_open(request, timeout=None):
    sent["body"] = _json.loads(request.data.decode())
    sent["key"] = request.headers.get("X-api-key")
    return FakeResponse(_json.dumps({"results": [{"title": "GDPR Art. 17", "url": "https://eur-lex.example/17",
                                                   "text": "Right to  erasure\n applies unless a legal obligation"}]}).encode())


ex = ExaSearch(SECRET, opener=fake_open)
found = ex.search("what does erasure mean for jain.aditi@gmail.com +34 668 324 642 33f407a3689b1b57bdb15b492e565867a507c27ce066c1d6a584f03f4dbb5565")
check("the query is masked before it is sent: no email, phone or hash leaves the machine",
      "@" not in sent["body"]["query"] and "668" not in sent["body"]["query"] and "33f407" not in sent["body"]["query"]
      and "<email>" in sent["body"]["query"])
check("the key goes in a header, never in the body", sent["key"] == SECRET and SECRET not in _json.dumps(sent["body"]))
check("results are parsed and whitespace collapsed", found[0]["title"] == "GDPR Art. 17" and "\n" not in found[0]["text"])
check("rendered results are labeled external and not evidence", "EXTERNAL" in render_web(found, "q") and "not evidence" in render_web(found, "q"))


def failing_open(request, timeout=None):
    raise urllib.error.HTTPError("https://api.example", 401, f"bad key {SECRET}", {}, io.BytesIO(SECRET.encode()))


try:
    ExaSearch(SECRET, opener=failing_open).search("q")
    check("an HTTP error becomes WebSearchError", False)
except WebSearchError as exc:
    check("an HTTP error becomes WebSearchError, and the message never contains the key", "401" in str(exc) and SECRET not in str(exc))
try:
    ExaSearch(SECRET, opener=lambda r, timeout=None: (_ for _ in ()).throw(TimeoutError())).search("q")
    check("a timeout becomes WebSearchError", False)
except WebSearchError:
    check("a timeout becomes WebSearchError", True)
check("no key configured means no search client", ExaSearch.from_env({}) is None and ExaSearch.from_env({"EXA_API_KEY": " "}) is None)
check("a quoted key and URL from a .env file are cleaned",
      ExaSearch.from_env({"EXA_API_KEY": '"abc"', "EXA_API_URL": '"https://x.test/search"'})._url == "https://x.test/search")

base = lambda web: {t.name for t in make_tools(samples, lambda: None, lambda: None, None, UsageLedger(Path(tempfile.mkdtemp()) / "u.jsonl"), {}, web)}
check("without a key the chat has no web_search tool, with one it does", "web_search" not in base(None) and "web_search" in base(ex))

web_tool = {t.name: t for t in make_tools(samples, lambda: None, lambda: None, None, UsageLedger(Path(tempfile.mkdtemp()) / "u.jsonl"), {},
                                          ExaSearch(SECRET, opener=failing_open))}["web_search"]
check("a failing search is reported to the model, not raised", web_tool.invoke({"query": "x"}).startswith("web search unavailable"))

agent = ChatAgent(Gateway([step("web_search", '{"query": "erasure"}'), say("Per the web, case_77 shows this.")]),
                  [StructuredTool.from_function(lambda query: "EXTERNAL web text mentioning case_77", name="web_search",
                                                description="search the web")])
check("an id that appears only in web text is not treated as verified", "[unverified]" in agent.ask([], "what does erasure mean?") and "case_77" in agent.last_trace[0].observation)

# The audit must never depend on the web: only the composition root and the chat tools may mention the search module.
import re as _re
allowed = {"bootstrap.py", "chat_tools.py", "websearch.py"}
offenders = [str(p.relative_to(Path(__file__).resolve().parent.parent)) for p in Path(__file__).resolve().parent.parent.rglob("*.py")
             if "tests" not in p.parts and ".venv" not in p.parts and p.name not in allowed
             and _re.search(r"websearch|ExaSearch|api\.exa\.ai|EXA_API", p.read_text(encoding="utf-8"))]
check("no audit module (intent, identity, evidence, state, policy, audit, plan, validate) refers to web search", not offenders)

print("chat checks passed")
