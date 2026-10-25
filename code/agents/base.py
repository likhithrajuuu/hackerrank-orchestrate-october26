from typing import TypedDict

from contracts import (AuditResult, CaseResult, DiscoveryResult, IntentDecision, Interpretation, Plan, Problem,
                       Requirement, StateResult)
from identity import Subject
from loaders import Request


class CaseState(TypedDict, total=False):
    """What flows through the graph for one case. Each agent adds its own key."""
    request: Request
    intent: IntentDecision
    subject: Subject
    discovery: DiscoveryResult
    states: StateResult
    interpretations: tuple[Interpretation, ...]
    requirements: tuple[Requirement, ...]
    audit: AuditResult
    plan: Plan
    validation: tuple[Problem, ...]
    flags: list[str]
    result: CaseResult
