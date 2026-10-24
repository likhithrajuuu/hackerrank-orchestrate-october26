from policy import PolicyEngine


class PolicyAgent:
    """Attaches the required treatment, from deletion_rules.csv, to every resource whose state was reconstructed."""

    name = "policy"

    def __init__(self, engine: PolicyEngine):
        self._engine = engine

    def __call__(self, state) -> dict:
        audit_at = state["request"].audit_at
        return {"requirements": tuple(self._engine.requirement(rs, audit_at) for rs in state["states"].states)}
