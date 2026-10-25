import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable
os.environ.pop("OPENAI_API_KEY", None)                           # the evidence store must not make vision calls here
os.environ.pop("LLM_API_KEY", None)

from contracts import Claim, State
from evidence import EvidenceRecord
from state import PREFIXES, StateReconstructor, observe, prefix_of, reduce_resource

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
AUDIT = T0 + timedelta(days=1)


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


def record(rid, kind="log", observed=None, captured=T0, table=None, file_id="evidence_x", **fields):
    return EvidenceRecord(file_id, kind, captured, observed, rid, None, table, fields, 1)


def state_of(rec):
    found = observe(rec)
    return found[0].state if found else None


def log(rid, event, **fields):
    return record(rid, "log", event=event, **fields)


# 1) Log events.
check("a lookup with rows=0 is ABSENT", state_of(log("account_1", "profile_lookup_completed", rows="0", http_status="404")) == State.ABSENT)
check("a lookup with rows=1 is PRESENT", state_of(log("account_1", "profile_lookup_completed", rows="1", http_status="200")) == State.PRESENT)
check("a 404 alone is ABSENT", state_of(log("account_1", "profile_lookup_completed", http_status="404")) == State.ABSENT)
check("a search hit is PRESENT", state_of(log("search_1", "index_query_completed", result="hit")) == State.PRESENT)
check("a support ticket that still has the requester email is EXPOSED",
      state_of(log("support_1", "support_ticket_read_completed", requester_email="a@b.com", requester_name="null")) == State.EXPOSED)
check("a support ticket with every identifier null is DEIDENTIFIED",
      state_of(log("support_1", "support_ticket_read_completed", requester_email="null", requester_name="null",
                   requester_phone="null")) == State.DEIDENTIFIED)
check("a redacted message is DEIDENTIFIED", state_of(log("support_1", "message_read", message_text="[REDACTED]")) == State.DEIDENTIFIED)
check("a PII scan hit on a message is EXPOSED", state_of(log("support_1", "message_pii_scan", matched_value="a@b.com")) == State.EXPOSED)
check("an analytics read with email present is EXPOSED",
      state_of(log("analytics_1", "analytics_profile_read_completed", email="a@b.com", phone="null")) == State.EXPOSED)
check("an analytics read with no identifiers is DEIDENTIFIED",
      state_of(log("analytics_1", "analytics_profile_read", direct_identifiers="0", email="null", phone="null")) == State.DEIDENTIFIED)
check("an analytics identity scan with a direct identifier is EXPOSED",
      state_of(log("analytics_1", "analytics_identity_scan", direct_identifiers="1", matched_value="a@b.com")) == State.EXPOSED)
check("an enabled connector is ACTIVE", state_of(log("data_flow_1", "connector_config_read_completed", enabled="true")) == State.ACTIVE)
check("a disabled connector is DISABLED", state_of(log("data_flow_1", "connector_config_read_completed", enabled="false")) == State.DISABLED)
check("restore_allowed=true is RESTORABLE", state_of(log("backup_1", "restore_policy_read_completed", archive_present="true", restore_allowed="true")) == State.RESTORABLE)
check("restore_allowed=false is RESTORE_BLOCKED", state_of(log("backup_1", "restore_policy_read_completed", archive_present="true", restore_allowed="false")) == State.RESTORE_BLOCKED)
check("an eligible restore candidate with no block is RESTORABLE",
      state_of(log("backup_1", "restore_candidate_evaluated", eligible="true", deletion_block="false")) == State.RESTORABLE)
check("a denied restore authorization is RESTORE_BLOCKED", state_of(log("backup_1", "restore_authorization_completed", decision="deny")) == State.RESTORE_BLOCKED)
check("an export that is still downloadable is PRESENT", state_of(log("export_1", "export_download_check_completed", available="true")) == State.PRESENT)
check("a deletion marker that was never found means the record is still PRESENT",
      state_of(log("support_1", "deletion_marker_check", marker_found="false")) == State.PRESENT)
check("a lookup that only started says nothing", observe(log("account_1", "profile_lookup_started", lookup_key="a@b.com")) == [])
check("an event with no state fields says nothing", observe(log("support_1", "thumbnail_failed", reason="x")) == [])
check("an unknown resource prefix is ignored", observe(log("widget_1", "profile_lookup_completed", rows="1")) == [])

# 2) Ledger records (handover text and JSON ledgers).
def ledger(rid, record_type, result):
    return record(rid, "text", record_type=record_type, result=result, system="s")

check("ledger NOT_FOUND is ABSENT", state_of(ledger("account_1", "USER_PROFILE", "NOT_FOUND")) == State.ABSENT)
check("ledger IDENTIFIABLE is EXPOSED", state_of(ledger("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE")) == State.EXPOSED)
check("an ACTIVE backup can be restored", state_of(ledger("backup_1", "BACKUP_ARCHIVE", "ACTIVE")) == State.RESTORABLE)
check("an ACTIVE data flow stays ACTIVE", state_of(ledger("data_flow_1", "DATA_FLOW", "ACTIVE")) == State.ACTIVE)
check("a BLOCKED backup is RESTORE_BLOCKED", state_of(ledger("backup_1", "BACKUP_ARCHIVE", "BLOCKED")) == State.RESTORE_BLOCKED)
check("the record type decides the category", observe(ledger("support_1", "SUPPORT_ATTACHMENT", "FOUND"))[0].category == "SUPPORT_ATTACHMENT")

# 3) Structured rows (SQL, JSON, screenshots).
check("restore_allowed 0 in a SQL row is RESTORE_BLOCKED", state_of(record("backup_1", "sql", table="t", restore_allowed=0)) == State.RESTORE_BLOCKED)
check("enabled 1 in a SQL row is ACTIVE", state_of(record("data_flow_1", "sql", table="t", enabled=1)) == State.ACTIVE)
check("an analytics row that still links a user is EXPOSED", state_of(record("analytics_1", "json", user_id="user_5", email=None)) == State.EXPOSED)
check("an analytics row with everything null is DEIDENTIFIED",
      state_of(record("analytics_1", "json", user_id=None, email=None, phone=None, email_hash=None)) == State.DEIDENTIFIED)
check("a tombstone that keeps the name is EXPOSED", state_of(record("tombstone_1", "json", subject_reference="user_9", name="A B")) == State.EXPOSED)
check("a tombstone with only the user reference is DEIDENTIFIED", state_of(record("tombstone_1", "json", subject_reference="user_9", account_status="DELETED")) == State.DEIDENTIFIED)
check("a profile lookup with no profile is ABSENT", state_of(record("account_1", "json", profile=None, status_code=404)) == State.ABSENT)
check("a plain row is PRESENT", state_of(record("search_1", "sql", table="customer_search_documents", email="a@b.com")) == State.PRESENT)
check("a lookup key alone does not make a row EXPOSED", state_of(record("support_1", "sql", lookup_key="a@b.com", message_excerpt="hi")) == State.DEIDENTIFIED)

# 4) Reducing observations to one state.
def obs(state_rec, when=None):
    return observe(state_rec)[0]


a = obs(log("support_1", "support_ticket_read_completed", requester_email="a@b.com"))
b = obs(record("support_1", "log", observed=T0 + timedelta(hours=2), event="support_ticket_read_completed", requester_email="null"))
check("the latest observation wins", reduce_resource("support_1", [a, b], (), AUDIT).state == State.DEIDENTIFIED)

primary = obs(record("account_1", "sql", table="customer_profiles_primary", file_id="f1"))
replica_gone = obs(record("account_1", "log", captured=T0, event="profile_lookup_completed", rows="0"))
same_moment = reduce_resource("account_1", [primary, replica_gone], (), AUDIT)
check("at the same moment the worst case wins and the conflict is flagged",
      same_moment.state == State.PRESENT and same_moment.conflict)

late = obs(record("audit_record_1", "log", observed=T0, captured=AUDIT + timedelta(days=2), file_id="evidence_late",
                  event="audit_record_read_completed", rows="1"))
early = obs(record("audit_record_1", "sql", captured=T0 - timedelta(days=5), file_id="evidence_early", table="t"))
rs = reduce_resource("audit_record_1", [early, late], (), AUDIT)
check("a late-exported file sets the state but is never cited; the earlier citable file is cited",
      rs.state == State.PRESENT and rs.evidence_id == "evidence_early" and rs.citable)
only_late = reduce_resource("audit_record_1", [late], (), AUDIT)
check("with only a late-exported file nothing may be cited", only_late.evidence_id == "" and not only_late.citable)

existing = obs(record("support_1", "log", observed=T0, captured=T0 - timedelta(days=1), file_id="evidence_old",
                      event="support_ticket_read_completed", requester_email="a@b.com"))
now_present = obs(record("support_1", "log", observed=T0 + timedelta(hours=1), captured=AUDIT + timedelta(days=2),
                         file_id="evidence_late", event="attachment_pii_scan", matched_value="a@b.com"))
rs = reduce_resource("support_1", [existing, now_present], (), AUDIT)
check("a PRESENT record whose latest file is not citable cites an earlier citable observation of an existing record",
      rs.state == State.PRESENT and rs.evidence_id == "evidence_old" and rs.citable)
rs = reduce_resource("support_1", [now_present], (), AUDIT)
check("with nothing citable at all, nothing is cited", rs.evidence_id == "" and not rs.citable)
deleted_then = obs(record("support_1", "log", observed=T0, captured=T0 - timedelta(days=1), file_id="evidence_present", event="profile_lookup_completed", rows="1"))
gone = obs(record("support_1", "log", observed=T0 + timedelta(hours=1), captured=AUDIT + timedelta(days=2), file_id="evidence_late2", event="profile_lookup_completed", rows="0"))
check("an ABSENT state is never proven by an earlier observation that the record existed",
      reduce_resource("support_1", [deleted_then, gone], (), AUDIT).evidence_id == "")

boundary = obs(record("account_1", "text", captured=AUDIT, file_id="evidence_boundary", record_type="USER_PROFILE", result="NOT_FOUND", system="s"))
check("a file captured exactly at audit_at is citable", reduce_resource("account_1", [boundary], (), AUDIT).evidence_id == "evidence_boundary")

claim = Claim("activity_1", "DELETE_RECORD", "SUCCESS", T0)
still_there = obs(record("search_1", "sql", table="t"))
rs = reduce_resource("search_1", [still_there], (claim,), AUDIT)
check("a SUCCESS claim never overrides evidence that the record exists", rs.state == State.PRESENT and rs.claims == (claim,))
check("no observations means UNKNOWN, with the claims kept", reduce_resource("account_9", [], (claim,), AUDIT).state == State.UNKNOWN)

# 5) Tables stay consistent, and the real data reads end to end.
check("prefix_of strips the numeric suffix", prefix_of("audit_record_6645") == "audit_record" and prefix_of("data_flow_4890") == "data_flow")
check("every prefix maps to a system and a category", all(len(v) == 2 and all(v) for v in PREFIXES.values()))

from evidence import EvidenceStore
from identity import IdentityIndex
from loaders import load_activity, load_audit_log, load_identities, load_requests

store = EvidenceStore.load()
claims = {}
for row in load_activity():
    claims.setdefault(row.resource_id, []).append(row)
index = IdentityIndex(load_identities(), load_audit_log())
case14 = next(r for r in load_requests("sample_deletion_requests.csv") if r.case_id == "case_14")
subject = index.resolve(case14.email, case14.audit_at)
ids = {r.resource_id for r in store.for_tokens(set(subject.emails) | set(subject.hashes) | {subject.user_id}, case14.audit_at) if r.resource_id}
result = {s.resource_id: s for s in StateReconstructor(store, claims).reconstruct(ids, case14.audit_at)}
check("case_14: the account is ABSENT and cited", result["account_3677"].state == State.ABSENT and result["account_3677"].evidence_id == "evidence_03")
check("case_14: the retained audit record is PRESENT", result["audit_record_987"].state == State.PRESENT)
check("case_14: the backup is RESTORABLE even though its restore check says SUCCESS",
      result["backup_3870"].state == State.RESTORABLE and result["backup_3870"].claims[0].result == "SUCCESS")

print("state checks passed")
