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
from llm.gateway import LLMUnavailable
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


def tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
                     usage_metadata={"input_tokens": 50, "output_tokens": 5, "total_tokens": 55})


def final(text):
    return AIMessage(content=text, usage_metadata={"input_tokens": 60, "output_tokens": 8, "total_tokens": 68})


class ScriptedModel:
    """Stands in for a chat model: bind_tools returns itself, invoke replays the script and records what it saw."""

    def __init__(self, *script):
        self.script, self.seen, self.calls = list(script), [], 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        self.seen.append(list(messages))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


SETTINGS = LLMSettings(None, {ModelRole.PRIMARY: ModelSettings("luna-test", "high", None),
                              ModelRole.ESCALATION: ModelSettings("sol-test", "low", None)})


def echo_tool(x: str) -> str:
    """Echo the input."""
    return f"echo:{x}"


def broken_tool(x: str) -> str:
    """Always fails."""
    raise RuntimeError("boom")


TOOLS = [StructuredTool.from_function(echo_tool), StructuredTool.from_function(broken_tool)]


def agent_for(luna, sol, root, max_steps=6):
    def invoker(name):
        return ResilientInvoker(name, RetryPolicy(max_attempts=1), CircuitBreaker(5, 30.0),
                                sleep=lambda s: None, rng=random.Random(0))
    ledger = UsageLedger(root / "usage.jsonl")
    agent = ChatAgent(SETTINGS, {ModelRole.PRIMARY: luna, ModelRole.ESCALATION: sol},
                      {ModelRole.PRIMARY: invoker("luna"), ModelRole.ESCALATION: invoker("sol")},
                      TOOLS, ledger, max_steps)
    return agent, ledger


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)

    # 1) The model asks for a tool, the tool runs, and the model answers using its result.
    luna = ScriptedModel(tool_call("echo_tool", {"x": "hi"}), final("done"))
    agent, ledger = agent_for(luna, ScriptedModel(final("sol")), root / "a")
    check("tool call then final answer", agent.ask([], "say hi") == "done")
    tool_messages = [m for m in luna.seen[1] if isinstance(m, ToolMessage)]
    check("the tool result is fed back to the model", tool_messages and tool_messages[0].content == "echo:hi")
    check("the system prompt comes first and states the read-only rule",
          isinstance(luna.seen[0][0], SystemMessage) and "only read" in luna.seen[0][0].content)
    check("usage is recorded for both model turns", ledger.summary({})["per_model"]["luna-test"]["calls"] == 2)

    # 2) A failing tool or an unknown tool does not end the conversation.
    luna = ScriptedModel(tool_call("broken_tool", {"x": "a"}), tool_call("nope", {}, "c2"), final("recovered"))
    agent, _ = agent_for(luna, ScriptedModel(final("sol")), root / "b")
    check("a failing tool is reported back, not raised", agent.ask([], "q") == "recovered")
    results = [m.content for m in luna.seen[-1] if isinstance(m, ToolMessage)]
    check("the failure and the unknown tool are visible to the model",
          any(r.startswith("tool error: RuntimeError") for r in results) and any("unknown tool" in r for r in results))

    # 3) A model that never stops calling tools is cut off.
    luna = ScriptedModel(tool_call("echo_tool", {"x": "again"}))
    agent, _ = agent_for(luna, ScriptedModel(final("sol")), root / "c", max_steps=3)
    check("endless tool calls stop at max_steps", "stopped after 3 tool steps" in agent.ask([], "loop") and luna.calls == 3)

    # 4) Earlier turns are passed to the model.
    luna = ScriptedModel(final("ok"))
    agent, _ = agent_for(luna, ScriptedModel(final("sol")), root / "d")
    agent.ask([HumanMessage("earlier question"), AIMessage("earlier answer")], "follow-up")
    check("history is included before the new question",
          [m.content for m in luna.seen[0][1:]] == ["earlier question", "earlier answer", "follow-up"])

    # 5) Failure paths: fall back to the escalation model; both down; permanent errors stop the run.
    sol = ScriptedModel(final("from sol"))
    agent, ledger = agent_for(ScriptedModel(HttpError(503)), sol, root / "e")
    check("primary down: the escalation model answers", agent.ask([], "q") == "from sol")
    check("usage is attributed to the escalation model", "sol-test" in ledger.summary({})["per_model"])

    agent, _ = agent_for(ScriptedModel(HttpError(503)), ScriptedModel(HttpError(429)), root / "f")
    try:
        agent.ask([], "q")
        check("both down raises LLMUnavailable", False)
    except LLMUnavailable as exc:
        check("both models down: LLMUnavailable names both", len(exc.errors) == 2)

    agent, _ = agent_for(ScriptedModel(HttpError(401)), ScriptedModel(final("never")), root / "g")
    try:
        agent.ask([], "q")
        check("401 propagates", False)
    except PermanentLLMError:
        check("a permanent error is not swallowed by the fallback", True)

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

print("chat checks passed")
