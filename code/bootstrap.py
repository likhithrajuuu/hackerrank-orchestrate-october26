"""Composition root: the only place that builds concrete objects (dependency inversion, DECISIONS.md D-004)."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agents.audit import AuditAgent
from agents.chat import ChatAgent
from agents.chat_tools import make_tools
from agents.discovery import DiscoveryAgent
from agents.gaps import GapAgent
from websearch import ExaSearch
from agents.interpret import InterpretAgent
from agents.identity import IdentityAgent
from agents.intent import IntentAgent, IntentRouter
from agents.plan import PlannerAgent
from agents.policy import PolicyAgent
from agents.state import StateAgent
from agents.validate import ValidatorAgent
from audit import Auditor
from classifier import classify
from config import REPO_ROOT
from evidence import EvidenceStore
from evidence.image_reader import VisionExtractor, cached_vision
from graph import build_graph
from identity import IdentityIndex
from llm.cache import ResponseCache
from llm.config import LLMSettings, ModelRole, load_settings
from llm.factory import build_chat_model
from llm.gateway import LLMGateway
from llm.resilience import ResilientInvoker
from llm.usage import UsageLedger
from loaders import (Request, load_activity, load_audit_log, load_identities, load_manifest, load_requests,
                     load_rules)
from planner import Planner
from policy import PolicyEngine
from state import StateReconstructor
from validator import Validator

CODE_DIR = Path(__file__).resolve().parent
CACHE_DIR = CODE_DIR / "cache"
USAGE_LOG = CACHE_DIR / "usage.jsonl"
ENV_FILE = REPO_ROOT / ".env"      # git-ignored; holds the API key and model names
MAX_CONCURRENT_CALLS = 4           # most simultaneous calls to one model (D-011); matches the CLI's default --workers


def load_env(path: Path = ENV_FILE) -> bool:
    """Load KEY=VALUE pairs from .env into the environment. A variable already set in the shell wins over the file."""
    from dotenv import load_dotenv
    return load_dotenv(path, override=False)


@dataclass(frozen=True)
class LLMStack:
    gateway: LLMGateway
    ledger: UsageLedger
    settings: LLMSettings
    invokers: Mapping[ModelRole, ResilientInvoker]


def build_llm(env: Mapping[str, str] = os.environ) -> LLMStack:
    """Raises ConfigError when a required environment variable is missing."""
    settings = load_settings(env)
    chats = {role: build_chat_model(settings, role) for role in ModelRole}
    invokers = {role: ResilientInvoker(role.value, max_concurrent=MAX_CONCURRENT_CALLS) for role in ModelRole}
    ledger = UsageLedger(USAGE_LOG)
    gateway = LLMGateway(settings, chats, invokers, ResponseCache(CACHE_DIR / "llm"), ledger)
    return LLMStack(gateway, ledger, settings, invokers)


class _NoNetwork:
    """Stands in for a chat model. Anything not already in the response cache fails like an unavailable model."""

    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, *args, **kwargs):
        from llm.resilience import TransientLLMError
        raise TransientLLMError("replay: not in the response cache, and replay never calls a model")


def build_replay_stack(ledger_path: Path, env: Mapping[str, str] = os.environ) -> LLMStack:
    """The real gateway and response cache with the network removed: cached answers are used, everything else counts as
    'model unavailable'. It spends nothing and writes its usage rows to ledger_path, never to the real ledger. Needs the
    same model names as the run that filled the cache, because they are part of every cache key."""
    settings = load_settings({**env, "LLM_API_KEY": env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY") or "replay-no-key"})
    chats = {role: _NoNetwork() for role in ModelRole}
    invokers = {role: ResilientInvoker(role.value, max_concurrent=MAX_CONCURRENT_CALLS) for role in ModelRole}
    ledger = UsageLedger(ledger_path)
    return LLMStack(LLMGateway(settings, chats, invokers, ResponseCache(CACHE_DIR / "llm"), ledger), ledger, settings, invokers)


def build_intent_agent(stack: LLMStack | None) -> IntentAgent:
    return IntentAgent(classify, IntentRouter(), stack.gateway if stack else None)


def load_store(stack: "LLMStack | None") -> EvidenceStore:
    """Screenshots are read through the gateway (Sol) when a model is configured, otherwise from the cache only."""
    return EvidenceStore.load(VisionExtractor(stack.gateway) if stack else cached_vision)


def build_case_graph(stack: LLMStack | None, store: EvidenceStore | None = None):
    """Builds the full flow. Loading the evidence store takes a few seconds, so only commands that need it call this."""
    index = IdentityIndex(load_identities(), load_audit_log())
    store = store or load_store(stack)
    claims: dict[str, list] = {}
    for row in load_activity():
        claims.setdefault(row.resource_id, []).append(row)
    state_agent = StateAgent(StateReconstructor(store, claims))
    validator = Validator({m.file_id: m.captured_at for m in load_manifest()},
                          {a.activity_id: a.event_time for rows in claims.values() for a in rows},
                          {a.audit_id: a.event_time for a in load_audit_log()})
    return build_graph(build_intent_agent(stack), IdentityAgent(index), DiscoveryAgent(store), state_agent,
                       PolicyAgent(PolicyEngine(load_rules())), AuditAgent(Auditor()),
                       PlannerAgent(Planner()), ValidatorAgent(validator), GapAgent(claims),
                       InterpretAgent(store, stack.gateway if stack else None))


def build_chat_agent(stack: LLMStack) -> ChatAgent:
    """The conversational front end. The case graph (which loads all evidence) is only built if a tool needs it."""
    requests = load_all_requests()
    holder: dict = {}

    def get_store():
        if "store" not in holder:
            holder["store"] = load_store(stack)
        return holder["store"]

    def get_graph():
        if "graph" not in holder:
            holder["graph"] = build_case_graph(stack, get_store())
        return holder["graph"]

    tools = make_tools(requests, get_graph, get_store, build_intent_agent(stack), stack.ledger,
                       stack.settings.prices(), ExaSearch.from_env())
    return ChatAgent(stack.gateway, tools)


def load_all_requests() -> dict[str, Request]:
    """Evaluation requests plus the labeled samples, keyed by case_id, so any case can be looked up."""
    merged: dict[str, Request] = {}
    for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        for request in load_requests(name):
            if request.case_id in merged:
                raise ValueError(f"case_id {request.case_id} appears in more than one request file")
            merged[request.case_id] = request
    return merged
