from identity import IdentityIndex


class IdentityAgent:
    """Resolves the request email to one person and everything they held up to audit_at. Deterministic."""

    name = "identity"

    def __init__(self, index: IdentityIndex):
        self._index = index

    def __call__(self, state) -> dict:
        request = state["request"]
        subject = self._index.resolve(request.email, request.audit_at)
        flags = list(state.get("flags", []))
        if subject.status not in ("CURRENT", "HISTORICAL"):
            flags.append(f"identity:{subject.status}")
        return {"subject": subject, "flags": flags}
