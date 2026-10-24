import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable

from classifier import classify
from loaders import load_activity, load_requests

# 1) Samples: all 32 are deletion requests.
s = Counter(classify(r.request_text).intent for r in load_requests("sample_deletion_requests.csv"))
print("samples", s)                                   # {'DELETION': 32}

# 2) Full set: expect 508 DELETION, 10 NON_DELETION, 0 UNCERTAIN.
reqs = load_requests()
res = {r.case_id: classify(r.request_text) for r in reqs}
print("eval", Counter(d.intent for d in res.values()))
print("mixed", [c for c, d in res.items() if d.mixed])  # expect []
for c, d in res.items():
    if d.intent != "DELETION":
        print(c, d.intent, d.reasons, next(r.request_text for r in reqs if r.case_id == c)[:70])

# 3) Cross-check against intake events (a claim, not a label): counts should agree.
n_events = sum(a.event_type == "CLASSIFY_REQUEST" for a in load_activity())
print("CLASSIFY_REQUEST events", n_events)            # 10

# 4) Adversarial cases (also feed evaluation_report.md "3 custom cases").
custom = {
    "Please export my data and then delete my account.": "DELETION",
    "Please do not delete anything, I only want a copy of my data.": "NON_DELETION",
    "Please remove me from marketing lists only.": "NON_DELETION",
    "Quiero eliminar mi cuenta y mis datos personales.": "DELETION",
    "Can you tell me whether my account was removed?": "DELETION",
    "Hello, how are you?": "UNCERTAIN",
}
for text, want in custom.items():
    got = classify(text).intent
    print("OK " if got == want else "BAD", got, "|", text)
