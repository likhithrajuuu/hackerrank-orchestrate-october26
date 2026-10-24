from contracts import StateResult
from state import StateReconstructor


class StateAgent:
    """Works out the real state of every discovered resource at audit_at. Deterministic: it reads evidence and attaches
    activity-log rows as claims, but only evidence decides a state."""

    name = "state"

    def __init__(self, reconstructor: StateReconstructor):
        self._reconstructor = reconstructor

    def __call__(self, state) -> dict:
        request, discovery = state["request"], state["discovery"]
        return {"states": StateResult(self._reconstructor.reconstruct(discovery.resource_ids, request.audit_at))}
