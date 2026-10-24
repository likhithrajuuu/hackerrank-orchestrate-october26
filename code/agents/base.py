from typing import TypedDict

from contracts import CaseResult, DiscoveryResult, IntentDecision
from identity import Subject
from loaders import Request


class CaseState(TypedDict, total=False):
    """What flows through the graph for one case. Each agent adds its own key."""
    request: Request
    intent: IntentDecision
    subject: Subject
    discovery: DiscoveryResult
    flags: list[str]
    result: CaseResult
