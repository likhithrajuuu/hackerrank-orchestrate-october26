from planner import Planner


class PlannerAgent:
    """Arranges the audit into ordered findings, remediation and verification steps. Deterministic."""

    name = "plan"

    def __init__(self, planner: Planner):
        self._planner = planner

    def __call__(self, state) -> dict:
        return {"plan": self._planner.plan(state["audit"])}
