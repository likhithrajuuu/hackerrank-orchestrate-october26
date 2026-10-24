import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

CURRENT, HISTORICAL, UNRESOLVED, AMBIGUOUS = "CURRENT", "HISTORICAL", "UNRESOLVED", "AMBIGUOUS"

def sha256_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()

@dataclass(frozen=True)
class Subject:
    user_id: str | None
    status: str
    request_email: str
    emails: frozenset = frozenset()
    phones: frozenset = frozenset()
    hashes: frozenset = frozenset()
    via_audit_id: str | None = None
    candidates: tuple = ()

class IdentityIndex:
    def __init__(self, identities, audit_log):
        # Finding the snapshot of the each user
        self.current = {i.user_id: i for i in identities}
        # Each user's change log each, grouped by the field
        self.history = defaultdict(lambda: defaultdict(list))
        # Fetching every user by the email who has ever had it
        self.owners = defaultdict(set)
        for i in identities:
            self.owners[i.email.lower()].add(i.user_id)
        for a in sorted(audit_log, key=lambda a: (a.event_time, a.audit_id)):
            self.history[a.user_id][a.field_name].append(a)
            if a.field_name == "email":
                self.owners[a.old_value.lower()].add(a.user_id)
                self.owners[a.new_value.lower()].add(a.user_id)

    def _held(self, uid: str, field: str, audit_at: datetime) -> set:
        entries = self.history[uid][field]
        if not entries:
            return {getattr(self.current[uid], field).lower()}
        held = {entries[0].old_value.lower()}
        held |= {a.new_value.lower() for a in entries if a.event_time <= audit_at}
        return held

    def resolve(self, email: str, audit_at: datetime) -> Subject:
        e = email.strip().lower()
        owners= self.owners.get(e, set())
        if not owners:
            return Subject(None, UNRESOLVED, e, frozenset({e}), frozenset(), frozenset({sha256_email(e)}))

        holders = {u for u in owners if e in self._held(u, "email", audit_at)} or owners
        if len(holders) > 1:
            return Subject(None, AMBIGUOUS, e, frozenset({e}), frozenset(),frozenset({sha256_email(e)}), candidates=tuple(sorted(holders)))

        (uid,) = holders
        emails = self._held(uid, "email", audit_at) | {e}
        phones = self._held(uid, "phone", audit_at)
        hashes = self._held(uid, "email_hash", audit_at) | {sha256_email(x) for x in emails}
        is_current = e == self.current[uid].email.lower()
        via = None if is_current else next(
            (a.audit_id for a in self.history[uid]["email"]
            if e in (a.old_value.lower(), a.new_value.lower())), None)
        return Subject(uid, CURRENT if is_current else HISTORICAL, e,frozenset(emails), frozenset(phones), frozenset(hashes), via)