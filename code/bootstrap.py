"""Composition root: the only place that builds concrete objects (dependency inversion, DECISIONS.md D-004)."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agents.discovery import DiscoveryAgent
from agents.identity import IdentityAgent
from agents.intent import IntentAgent, IntentRouter
from classifier import classify
from evidence import EvidenceStore
from graph import build_graph
from identity import IdentityIndex
from llm.cache import ResponseCache
from llm.config import LLMSettings, ModelRole, load_settings
from llm.factory import build_chat_model
from llm.gateway import LLMGateway
from llm.resilience import ResilientInvoker
from llm.usage import UsageLedger
from loaders import Request, load_audit_log, load_identities, load_requests

CODE_DIR = Path(__file__).resolve().parent
CACHE_DIR = CODE_DIR / "cache"
USAGE_LOG = CACHE_DIR / "usage.jsonl"


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
    invokers = {role: ResilientInvoker(role.value) for role in ModelRole}
    ledger = UsageLedger(USAGE_LOG)
    gateway = LLMGateway(settings, chats, invokers, ResponseCache(CACHE_DIR / "llm"), ledger)
    return LLMStack(gateway, ledger, settings, invokers)


def build_intent_agent(stack: LLMStack | None) -> IntentAgent:
    return IntentAgent(classify, IntentRouter(), stack.gateway if stack else None)


def build_case_graph(stack: LLMStack | None):
    """Builds the full flow. Loading the evidence store takes a few seconds, so only commands that need it call this."""
    index = IdentityIndex(load_identities(), load_audit_log())
    store = EvidenceStore.load()
    return build_graph(build_intent_agent(stack), IdentityAgent(index), DiscoveryAgent(store))


def load_all_requests() -> dict[str, Request]:
    """Evaluation requests plus the labeled samples, keyed by case_id, so any case can be looked up."""
    merged: dict[str, Request] = {}
    for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        for request in load_requests(name):
            if request.case_id in merged:
                raise ValueError(f"case_id {request.case_id} appears in more than one request file")
            merged[request.case_id] = request
    return merged
