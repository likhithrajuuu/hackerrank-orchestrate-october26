from contracts import DiscoveryResult
from evidence import EvidenceStore
from state import is_modeled


class DiscoveryAgent:
    """Finds the evidence that mentions the subject, as of audit_at. Deterministic."""

    name = "discovery"

    def __init__(self, store: EvidenceStore):
        self._store = store

    def __call__(self, state) -> dict:
        request, subject = state["request"], state["subject"]
        tokens = set(subject.emails) | set(subject.hashes) | set(subject.phones)
        if subject.user_id:
            tokens.add(subject.user_id)
        records = tuple(self._store.for_tokens(tokens, request.audit_at))
        resource_ids = tuple(sorted({r.resource_id for r in records if r.resource_id and is_modeled(r.resource_id)}))
        return {"discovery": DiscoveryResult(records, resource_ids, tuple(sorted(self._store.unread)))}
