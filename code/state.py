"""Stage 4: read evidence as states, then reduce it to one state per resource at audit_at.

Two steps, kept apart:
  observe(record)          one evidence record -> zero or more Observations (what that record says)
  StateReconstructor       all observations of a resource -> one ResourceState (what is true at audit_at)

Activity-log rows are only attached as claims. They never decide a state: a row can say SUCCESS while the record
still exists, so only evidence (logs, snapshots, ledgers, documents) sets a state.
"""
from __future__ import annotations

import re
from typing import Callable, Iterable, Mapping

from contracts import Claim, Observation, ResourceState, State
from evidence import EvidenceRecord

# Resource-id prefix -> (system_id, category). The systems come from the ledger records inside the evidence, which are
# consistent. The activity log's target_system is NOT used: it contains decoy routing (for example object_store).
PREFIXES: dict[str, tuple[str, str]] = {
    "account": ("account_service", "USER_PROFILE"),
    "search": ("search_service", "SEARCH_INDEX_ENTRY"),
    "analytics": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support": ("support_platform", "SUPPORT_MESSAGE"),
    "export": ("export_service", "USER_EXPORT"),
    "audit_record": ("security_service", "SECURITY_AUDIT_LOG"),
    "legal_hold": ("legal_service", "LEGAL_HOLD_RECORD"),
    "backup": ("backup_service", "BACKUP_ARCHIVE"),
    "tombstone": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "data_flow": ("integration_service", "DATA_FLOW"),
    "marketing": ("marketing_platform", "MARKETING_PROFILE"),
    "cache": ("cache_service", "CACHE_ENTRY"),
    "email_delivery": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
    "object": ("object_storage", "SUPPORT_ATTACHMENT"),
}
CATEGORIES = {category for _, category in PREFIXES.values()}

# Fields that are direct identifiers. lookup_key, correlation_key and subject_reference are deliberately NOT here:
# they describe how the record was found, not what the record holds.
DIRECT_IDENTIFIERS = {"email", "phone", "email_hash", "full_name", "name", "display_name", "requester_email",
                      "requester_name", "requester_phone", "owner_email", "owner_phone", "customer_email",
                      "matched_value"}
LINKING_REFERENCES = {"user_id", "customer_reference"}

# Severity when observations at the same moment disagree: a copy that still exposes data outranks one that does not.
SEVERITY = [State.EXPOSED, State.ACTIVE, State.RESTORABLE, State.PRESENT, State.DEIDENTIFIED,
            State.RESTORE_BLOCKED, State.DISABLED, State.ABSENT]

EXISTS_FAMILY = {State.PRESENT, State.EXPOSED, State.DEIDENTIFIED}      # all prove that the record exists

LEDGER_RESULTS = {"FOUND": State.PRESENT, "NOT_FOUND": State.ABSENT, "ACTIVE": State.ACTIVE, "DISABLED": State.DISABLED,
                  "BLOCKED": State.RESTORE_BLOCKED, "IDENTIFIABLE": State.EXPOSED, "REDACTED": State.DEIDENTIFIED,
                  "ANONYMIZED": State.DEIDENTIFIED}


def prefix_of(resource_id: str) -> str:
    return re.sub(r"_?\d+$", "", resource_id)


def is_modeled(resource_id: str) -> bool:
    """True for the resource kinds we model. Screenshots also show support case and order numbers (CS-..., ORD-...),
    which are not resources of any system and must not be treated as discovered resources."""
    return prefix_of(resource_id) in PREFIXES


def _blank(value) -> bool:
    return value is None or str(value).strip().lower() in ("", "null", "none", "[redacted]")


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _falsy(value) -> bool:
    return str(value).strip().lower() in ("false", "0", "no")


def _has_identifiers(fields: Mapping, include_references: bool = False) -> bool:
    keys = DIRECT_IDENTIFIERS | (LINKING_REFERENCES if include_references else set())
    return any(key.split(".")[-1] in keys and not _blank(value) for key, value in fields.items())


def _rows_or_status(f: Mapping) -> State | None:
    if f.get("rows") is not None:
        return State.ABSENT if str(f["rows"]).strip() in ("0", "") else State.PRESENT
    if f.get("http_status") is not None:
        return State.ABSENT if str(f["http_status"]).strip() == "404" else State.PRESENT
    return None


# ---- log events: event name -> (state, category override or None) ----
def _ev_rows(f, category):
    return _rows_or_status(f), None


def _ev_contact_query(f, category):
    # a query against the legacy contact index finds a secondary identifier, whatever its resource-id prefix says
    return _rows_or_status(f), "SECONDARY_IDENTIFIER"


def _ev_contact_lookup(f, category):
    # a contact lookup that reports an email opt-in is a marketing profile
    return _rows_or_status(f), "MARKETING_PROFILE"


def _ev_index_query(f, category):
    return (State.PRESENT if str(f.get("result", "")).lower() == "hit" else State.ABSENT), None


def _ev_support_ticket(f, category):
    return (State.EXPOSED if _has_identifiers(f) else State.DEIDENTIFIED), "SUPPORT_MESSAGE"


def _ev_message_read(f, category):
    return (State.DEIDENTIFIED if _blank(f.get("message_text")) else State.EXPOSED), "SUPPORT_MESSAGE"


def _ev_message_scan(f, category):
    return State.EXPOSED, "SUPPORT_MESSAGE"


def _ev_attachment_scan(f, category):
    return State.PRESENT, "SUPPORT_ATTACHMENT"


def _ev_analytics(f, category):
    direct = f.get("direct_identifiers")
    exposed = _has_identifiers(f, include_references=True) or (direct is not None and str(direct) not in ("0", ""))
    return (State.EXPOSED if exposed else State.DEIDENTIFIED), None


def _ev_connector(f, category):
    if f.get("enabled") is None:
        return None, None
    return (State.ACTIVE if _truthy(f["enabled"]) else State.DISABLED), None


def _ev_restore_policy(f, category):
    if f.get("archive_present") is not None and _falsy(f["archive_present"]):
        return State.ABSENT, None
    if f.get("restore_allowed") is None:
        return None, None
    return (State.RESTORABLE if _truthy(f["restore_allowed"]) else State.RESTORE_BLOCKED), None


def _ev_restore_candidate(f, category):
    if _truthy(f.get("deletion_block", "false")):
        return State.RESTORE_BLOCKED, None
    if f.get("eligible") is None:
        return None, None
    return (State.RESTORABLE if _truthy(f["eligible"]) else State.RESTORE_BLOCKED), None


def _ev_restore_authorization(f, category):
    decision = str(f.get("decision", "")).lower()
    if decision not in ("deny", "allow"):
        return None, None
    return (State.RESTORE_BLOCKED if decision == "deny" else State.RESTORABLE), None


def _ev_availability(f, category):
    if f.get("available") is None:
        return State.PRESENT, None
    return (State.PRESENT if _truthy(f["available"]) else State.ABSENT), None


def _ev_deletion_marker(f, category):
    # marker_found=false means nobody ever marked it deleted, so the record is still there
    return (State.ABSENT if _truthy(f.get("marker_found", "false")) else State.PRESENT), "SECONDARY_IDENTIFIER"


def _ev_retained(f, category):
    return State.PRESENT, "LEGAL_HOLD_RECORD"


def _ev_record_lookup(f, category):
    return LEDGER_RESULTS.get(str(f.get("result", "")).upper()), None


EVENT_RULES: dict[str, Callable] = {
    "profile_lookup_completed": _ev_rows,
    "contact_query_completed": _ev_contact_query,
    "contact_lookup_completed": _ev_contact_lookup,
    "audit_record_read_completed": _ev_rows,
    "index_query_completed": _ev_index_query,
    "support_ticket_read_completed": _ev_support_ticket,
    "message_read": _ev_message_read,
    "message_pii_scan": _ev_message_scan,
    "attachment_pii_scan": _ev_attachment_scan,
    "analytics_profile_read_completed": _ev_analytics,
    "analytics_profile_read": _ev_analytics,
    "analytics_identity_scan": _ev_analytics,
    "connector_config_read_completed": _ev_connector,
    "connector_config_loaded": _ev_connector,
    "connector_heartbeat": _ev_connector,
    "restore_policy_read_completed": _ev_restore_policy,
    "restore_candidate_evaluated": _ev_restore_candidate,
    "restore_authorization_completed": _ev_restore_authorization,
    "export_download_check_completed": _ev_availability,
    "export_metadata_read_completed": _ev_availability,
    "deletion_marker_check": _ev_deletion_marker,
    "retained_order_loaded": _ev_retained,
    "record_lookup": _ev_record_lookup,
}


# ---- structured rows (SQL tables, JSON records, extracted screenshots) ----
def _row_state(category: str, f: Mapping) -> State:
    if category == "DATA_FLOW" and f.get("enabled") is not None:
        return State.ACTIVE if _truthy(f["enabled"]) else State.DISABLED
    if category == "BACKUP_ARCHIVE" and f.get("restore_allowed") is not None:
        return State.RESTORABLE if _truthy(f["restore_allowed"]) else State.RESTORE_BLOCKED
    if category == "SUPPORT_MESSAGE":
        return State.EXPOSED if _has_identifiers(f) else State.DEIDENTIFIED
    if category == "ANALYTICS_PROFILE":
        return State.EXPOSED if _has_identifiers(f, include_references=True) else State.DEIDENTIFIED
    if category == "ACCOUNT_TOMBSTONE":
        return State.EXPOSED if _has_identifiers(f) else State.DEIDENTIFIED
    if category == "USER_PROFILE":
        if "status_code" in f and str(f["status_code"]) == "404":
            return State.ABSENT
        if "profile" in f and f["profile"] is None:
            return State.ABSENT
    if category == "SEARCH_INDEX_ENTRY" and str(f.get("result", "")).lower() in ("miss", "no_hit"):
        return State.ABSENT
    return State.PRESENT


def _classify(record: EvidenceRecord) -> tuple[State, str, str] | None:
    """(state, category, source) for one record, or None when the record says nothing about state."""
    f, rid = record.fields, record.resource_id
    system_id, category = PREFIXES[prefix_of(rid)]

    if record.kind == "log":
        event = f.get("event")
        rule = EVENT_RULES.get(event) if event else None
        if rule is not None:
            state, override = rule(f, category)
        elif event and event.endswith("_started"):
            return None
        else:
            state, override = _rows_or_status(f), None        # unknown event that still reports rows
        return (state, override or category, event or "log") if state else None

    if "record_type" in f and "result" in f:                    # ledger records 
        category = f["record_type"] if f["record_type"] in CATEGORIES else category
        state = LEDGER_RESULTS.get(str(f["result"]).upper())
        if state == State.ACTIVE and category == "BACKUP_ARCHIVE":
            state = State.RESTORABLE                           
        return (state, category, f["record_type"]) if state else None

    if record.kind in ("sql", "json", "image"):
        return _row_state(category, f), category, record.table or record.kind
    return None


def observe(record: EvidenceRecord) -> list[Observation]:
    """What one evidence record says about the state of its resource."""
    if not record.resource_id or prefix_of(record.resource_id) not in PREFIXES:
        return []
    found = _classify(record)
    if found is None:
        return []
    state, category, source = found
    system_id = PREFIXES[prefix_of(record.resource_id)][0]
    until = next((str(record.fields[k]) for k in ("retention_until", "retained_until") if not _blank(record.fields.get(k))), "")
    return [Observation(record.resource_id, system_id, category, state, record.file_id, record.kind,
                        record.captured_at, record.observed_at, source, f"retention_until={until}" if until else "")]


def reduce_resource(resource_id: str, observations: list[Observation], claims: tuple[Claim, ...],
                    audit_at) -> ResourceState:
    """One state per resource: the latest moment wins. Where observations at that moment disagree (a primary table
    and a replica in the same snapshot), the worst case wins, because a leftover copy is still a leftover."""
    system_id, category = PREFIXES.get(prefix_of(resource_id), ("unknown", "UNKNOWN"))
    if not observations:
        return ResourceState(resource_id, system_id, category, State.UNKNOWN, "", False, None, False, (), claims)
    latest = max(o.when for o in observations)
    tied = [o for o in observations if o.when == latest]
    states = {o.state for o in tied}
    state = min(states, key=SEVERITY.index)
    chosen = sorted((o for o in tied if o.state == state), key=lambda o: (o.file_id, o.source))[0]
    # The state comes from the latest observation, but a file captured after audit_at may not be cited. In that case
    # cite the most recent citable observation that shows the same state.
    citable = [o for o in observations if o.captured_at <= audit_at]
    support = [o for o in citable if o.state == state]
    if not support and state == State.PRESENT:
        # "The record exists" is proven by any citable observation of an existing record, whatever it held at the time.
        support = [o for o in citable if o.state in EXISTS_FAMILY]
    cited = max(support, key=lambda o: (o.when, o.file_id, o.source)) if support else None
    return ResourceState(resource_id, system_id, chosen.category or category, state,
                         cited.file_id if cited else "", cited is not None, chosen.observed_at, len(states) > 1,
                         tuple(sorted(observations, key=lambda o: (o.when, o.file_id, o.source))), claims)


class StateReconstructor:
    def __init__(self, store, claims_by_resource: Mapping[str, list]):
        self._store, self._claims = store, claims_by_resource

    def reconstruct(self, resource_ids: Iterable[str], audit_at) -> tuple[ResourceState, ...]:
        out = []
        for rid in sorted(set(resource_ids)):
            observations = [o for record in self._store.for_resource(rid, audit_at) for o in observe(record)]
            claims = tuple(Claim(a.activity_id, a.event_type, a.reported_result, a.event_time, a.details)
                           for a in self._claims.get(rid, []) if a.event_time <= audit_at)
            out.append(reduce_resource(rid, observations, claims, audit_at))
        return tuple(out)
