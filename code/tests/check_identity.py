import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable

from identity import IdentityIndex, sha256_email
from loaders import load_audit_log, load_identities, load_requests
from config import parse_ts

ids, aud = load_identities(), load_audit_log()
idx = IdentityIndex(ids, aud)
reqs = load_requests()
subs = {r.case_id: idx.resolve(r.email, r.audit_at) for r in reqs}

# 1) Status counts. Expect CURRENT ~418, HISTORICAL ~100, nothing else.
print(Counter(s.status for s in subs.values()))

# 2) The hash formula holds for every user.
print("hash mismatches:", sum(sha256_email(i.email) != i.email_hash for i in ids))      # 0

# 3) Does any email belong to more than one user?
print("shared emails:", sum(len(v) > 1 for v in idx.owners.values()))                    # hope 0

# 4) Worked example from the PRD (case_10).
c10 = next(r for r in load_requests("sample_deletion_requests.csv") if r.case_id == "case_10")
s = idx.resolve(c10.email, c10.audit_at)
print(s.user_id, s.status, sorted(s.emails), len(s.hashes), s.via_audit_id)
# user_10 HISTORICAL [3 emails: ionescu, petrescu, n.petrescu] 3 audit_121

# 5) The cutoff works: before the April 2026 change the newest email is not yet held.
early = idx.resolve("nadia.ionescu@yahoo.com", parse_ts("2026-01-01T00:00:00Z"))
print(sorted(early.emails))        # only ionescu and petrescu

# 6) The email used by two cases resolves to one person each time.
dahl = [(r.case_id, subs[r.case_id].user_id) for r in reqs if r.email == "ingrid.dahl@outlook.com"]
print(dahl)                        # same user_id twice