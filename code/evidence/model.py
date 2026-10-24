import re
from dataclasses import dataclass, field
from datetime import datetime

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")


@dataclass(frozen=True)
class EvidenceRecord:
    file_id: str
    kind: str                       # sql | json | log | text | pdf | image
    captured_at: datetime           
    observed_at: datetime | None    
    resource_id: str | None
    system_id: str | None
    table: str | None               
    fields: dict = field(default_factory=dict)   
    ordinal: int = 0                


def tokens_of(fields: dict) -> set[str]:
    """Identifiers found in any value: emails, user_N, sha256 hashes, phone fields."""
    out = set()
    for key, value in fields.items():
        if not isinstance(value, str) or not value:
            continue
        low = value.lower()
        out |= {m.lower() for m in EMAIL_RE.findall(value)}
        out |= set(USER_RE.findall(low))
        out |= set(HASH_RE.findall(low))
        if "phone" in key.lower():
            out.add(low.strip())
    return out


def flatten(obj: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in obj.items():
        if isinstance(value, dict):
            out.update(flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out
