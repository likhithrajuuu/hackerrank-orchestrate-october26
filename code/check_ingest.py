from collections import Counter
from loaders import *

reqs = load_requests()
ids, aud, act, = load_indentities(), load_audit_log(), load_activity()
print("requests", len(reqs)) #518
print("identities", len(ids)) #850
print("audit_log", len(aud), Counter(a.field_name for a in aud)) # email 107, email_hash 107, phone 92
print("activities", len(act)) # 4011
print("rules", len(load_rules())) #15
print("manifest", len(load_manifest())) # 94
print("sample_requests", len(load_requests("sample_deletion_requests.csv"))) #32