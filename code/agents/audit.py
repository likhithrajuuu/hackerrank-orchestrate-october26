from audit import Auditor


class AuditAgent:
    """Compares each resource's state with its requirement and decides COMPLETE, INCOMPLETE or UNDECIDED."""

    name = "audit"

    def __init__(self, auditor: Auditor):
        self._auditor = auditor

    def __call__(self, state) -> dict:
        request = state["request"]
        audit = self._auditor.audit(state["states"].states, state["requirements"], request.requested_at)
        return {"audit": audit}
