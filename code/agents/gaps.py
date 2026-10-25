from datetime import timedelta
from typing import Mapping, Sequence

from state import is_modeled

ACTIONS = ("DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD")
WINDOW = timedelta(minutes=10)


def _kind(resource_id: str) -> str:
    return resource_id.rsplit("_", 1)[0]


class GapAgent:
    """Flags activity that may belong to this person but cannot be attributed to them. Deterministic, and it never
    changes a status or a step: an activity row names no email, hash or user, so which person it concerns is not in
    the data. The flag tells a reviewer where to look."""

    name = "gaps"

    def __init__(self, claims: Mapping[str, Sequence]):
        self._rows = [row for rows in claims.values() for row in rows
                      if row.event_type in ACTIONS and is_modeled(row.resource_id)]

    def __call__(self, state) -> dict:
        request, discovery = state["request"], state["discovery"]
        known = {_kind(rid) for rid in discovery.resource_ids}
        nearby = sorted({row.resource_id for row in self._rows
                         if _kind(row.resource_id) not in known and row.event_time <= request.audit_at
                         and timedelta(0) <= request.reported_at - row.event_time <= WINDOW})
        flags = list(state.get("flags", []))
        if nearby:
            flags.append(f"discovery:unattributed_activity={len(nearby)}")
        return {"flags": flags}
