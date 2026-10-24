import csv
from dataclasses import dataclass
from datetime import datetime

from config import DATASET, parse_ts


# Reading the csv files and returning as a list of the dictionary
def _read(name: str) -> list[dict]:
    with open(DATASET / name, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@dataclass(frozen=True)
class Request:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str
    reported_at: datetime
    audit_at: datetime
    request_text: str

@dataclass(frozen=True)
class Identity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime

@dataclass(frozen=True)
class AuditEntry:
    audit_id: str
    user_id: str
    event_time: datetime
    field_name: str
    old_value: str
    new_value: str

@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str

@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str

@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str
    origin: str
    captured_at: datetime

def load_requests(name: str = "deletion_requests.csv") -> list[Request]:
    rows = [
        Request(
            r["case_id"],
            r["email"].strip(),
            parse_ts(r["requested_at"]),
            r["reported_status"],
            parse_ts(r["reported_at"]),
            parse_ts(r["audit_at"]),
            r["request_text"]
        )
        for r in _read(name)
    ]

    ids = [r.case_id for r in rows]
    assert len(ids) == len(set(ids)), "duplicate case_id"
    return rows

def load_indentities() -> list[Identity]:
    return [
        Identity(
            r["user_id"],
            r["name"],
            r["email"].strip(),
            r["phone"],
            r["email_hash"],
            parse_ts(r["updated_at"]),
        )

        for r in _read("user_identities.csv")
    ]

def load_audit_log() -> list[AuditEntry]:
    return [
        AuditEntry(
            r["audit_id"],
            r["user_id"],
            parse_ts(r["event_time"]),
            r["field_name"],
            r["old_value"],
            r["new_value"]
        )

        for r in _read("user_audit_log.csv")
    ]

def load_activity() -> list[Activity]:
    return [
        Activity(
            r["activity_id"],
            parse_ts(r["event_time"]),
            r["event_type"],
            r["source_system"],
            r["target_system"],
            r["resource_id"],
            r["reported_result"],
            r["details"]
        )

        for r in _read("system_activity.csv")
    ]

def load_rules() -> list[Rule]:
    return [
        Rule(
            r["rule_id"],
            r["data_category"],
            r["condition"],
            r["required_action"]
        )

        for r in _read("deletion_rules.csv")
    ]

def load_manifest() -> list[EvidenceFile]:
    return [
        EvidenceFile(
            r["file_id"],
            r["file_path"],
            r["origin"],
            parse_ts(r["captured_at"])
        )

        for r in _read("evidence_files.csv")
    ]

