from typing import TypedDict

from contracts import AuditResult, CaseResult, DiscoveryResult, IntentDecision, Requirement, StateResult
from identity import Subject
from loaders import Request


class CaseState(TypedDict, total=False):
    """What flows through the graph for one case. Each agent adds its own key."""
    request: Request
    intent: IntentDecision
    subject: Subject
    discovery: DiscoveryResult
    states: StateResult
    requirements: tuple[Requirement, ...]
    audit: AuditResult
    flags: list[str]
    result: CaseResult
