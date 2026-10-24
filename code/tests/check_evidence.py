import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable

from config import parse_ts
from evidence import EvidenceStore
from identity import IdentityIndex
from loaders import load_audit_log, load_identities, load_manifest, load_requests

store = EvidenceStore.load()

# 1) Totals per kind.
print("records", len(store.records), dict(Counter(r.kind for r in store.records)))

# 2) Coverage: every manifest file is either read or listed as unread with a reason.
manifest = load_manifest()
read_ids = {r.file_id for r in store.records}
missing = [m.file_id for m in manifest if m.file_id not in read_ids and m.file_id not in store.unread]
print("manifest", len(manifest), "| read", len(read_ids), "| unread", len(store.unread), "| silent gaps", missing)
for fid, why in sorted(store.unread.items()):
    print("  unread", fid, "-", why[:90])

# 3) Grounded examples.
idx = IdentityIndex(load_identities(), load_audit_log())
samples = {r.case_id: r for r in load_requests("sample_deletion_requests.csv")}

c10 = samples["case_10"]
s = idx.resolve(c10.email, c10.audit_at)
hits = store.for_tokens(set(s.emails) | set(s.hashes) | {s.user_id}, c10.audit_at)
print("case_10 hits", len(hits), sorted({(h.file_id, str(h.resource_id)) for h in hits if h.file_id == "evidence_61"}))
print("case_10 finds support_002 via trace join:", any(h.resource_id == "support_002" for h in hits))  # True
early = store.for_tokens(set(s.emails), parse_ts("2026-09-17T21:00:00Z"))
print("case_10 before evidence_61:", [h.file_id for h in early if h.file_id == "evidence_61"])  # []

c09 = samples["case_09"]  # audit_record_4902 sits in a log exported AFTER audit_at, but the line predates it
rec09 = store.for_resource("audit_record_4902", c09.audit_at)
print("case_09 audit_record_4902: visible", len(rec09), "| citable", sum(store.citable(r, c09.audit_at) for r in rec09))  # 1 | 0

c04 = samples["case_04"]
print("case_04 analytics_901:", [(r.file_id, r.kind) for r in store.for_resource("analytics_901", c04.audit_at)])

ev54 = [r for r in store.records if r.file_id == "evidence_54"]
prim = {r.resource_id for r in ev54 if r.table == "customer_profiles_primary"}
repl = {r.resource_id for r in ev54 if r.table == "customer_profiles_failover_replica"}
print("evidence_54 replica-only (drift):", sorted(repl - prim))

# 4) Time gate holds for every case.
bad = 0
for r in load_requests():
    sub = idx.resolve(r.email, r.audit_at)
    for h in store.for_tokens(set(sub.emails) | set(sub.hashes), r.audit_at):
        bad += (h.observed_at or h.captured_at) > r.audit_at
print("time-gate violations:", bad)  # 0

# 5) Determinism.
again = EvidenceStore.load()
print("deterministic:", len(again.records) == len(store.records)
      and again.records[0] == store.records[0] and again.records[-1] == store.records[-1])
