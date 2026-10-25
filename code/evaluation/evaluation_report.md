# Evaluation report

How well does the system do, how do we know, and what went wrong on the way. Every number below is produced by a script in
`code/tests/` that you can rerun (commands in section 9).

## 1. Read this first: what these numbers can and cannot show

- The only labeled answers are the **32 sample cases** (`dataset/sample_output.csv`). The other 486 requests that are
  scored by the hidden answers have **no labels we can see**, so the accuracy on them is **unknown**.
- **The rules were written and corrected while looking at these same 32 samples.** For example, two samples exposed that
  records must be categorized by what the evidence says they are and not by their id prefix (section 7, item 8). So the
  numbers below show how well the system **fits the samples**. They are not a held-out estimate, and the real score on the
  other cases is probably lower.
- The labels are used **only to score**. They never feed a prediction (`AGENTS.md` section 4).
- All 32 labeled cases are deletion requests, so the scorer skips the intent step (it has its own tests in section 6) and
  runs everything after it for real: identity, discovery, state, policy, audit, planner, validator.

## 2. Case-level status accuracy

| Measure | Result |
|---|---|
| Case status (`COMPLETE` / `INCOMPLETE`) matches the label | **31 of 32 (97%)** |

- The one miss is `case_07` (label `INCOMPLETE`, ours `COMPLETE`): its finding is a support attachment (`support_001`) that
  appears in no readable evidence, so no violation can be shown and the case takes the "no evidence" default (D-018).
- Three samples (`case_01`, `case_02`, `case_04`) are right **only because of that same default**. Without it they would
  be undecided. The default was chosen because three of the four such labeled cases were `COMPLETE`. That is a very small
  sample, so treat these three as luck as much as skill.
- Stage by stage, before the default: 28 of 32 (`code/tests/evaluate_audit.py`); the other 4 had no assessable evidence.
- On the 518 evaluation requests (unscored): 344 `INCOMPLETE`, 164 `COMPLETE`, 10 `NOT_APPLICABLE`. Four of the
  `COMPLETE` rows (`case_33`, `case_55`, `case_61`, `case_63`) have no assessable evidence and use the default; two more
  `COMPLETE` rows (`case_435`, `case_87`) are flagged for other review reasons. All six are marked in the run output.
  `case_39` was a fifth default until the evidence interpreter (D-021) read its screenshot: a data flow in continuous sync
  with events being delivered, so it is `INCOMPLETE`. It is the only row the interpreter changed.

## 3. Step-level, field-level and sequence accuracy

Whole output rows against the labeled rows (`code/tests/evaluate_output.py`, 32 cases, 193 labeled steps):

| Measure | Result |
|---|---|
| Case status accuracy | 31 of 32 (97%) |
| Step-level: exact steps found (recall) | 157 of 193 (81%) |
| Step-level: steps we wrote that are right (precision) | **157 of 157 (100%)** |
| Step-level F1 | 90% |
| Field-level accuracy over all labeled steps | 81.3% (942 of 1,158 fields) |
| Field-level accuracy on the steps we wrote | **100%** (all 7 fields right on every one of the 157) |
| Sequence: cases whose whole array is identical to the label | 10 of 32 (31%) |
| Sequence: ordered overlap with the label (longest common subsequence) | 81.3% |
| Sequence: cases where the steps we share are in the same order | **32 of 32** |
| Validator errors on our own rows | 0 |

How to read it:

- **Precision is 100%: we never wrote a step the labeled answer lacks.** All of our error is omission.
- **The 81% recall is not scattered mistakes.** The 36 missing steps belong to 34 resources that discovery never reaches
  (section 5). On every resource we do reach, the finding, the fix, the check, the state and the evidence id are right.
- The field-level figure over all labeled steps equals recall, because a missing step counts every field as wrong. The more
  informative figure is the 100% on the steps we wrote.
- The order is right wherever steps overlap: 32 of 32. The 10 whole-array matches are the cases with nothing missing.

Per stage (`evaluate_state.py`, `evaluate_audit.py`):

| Stage | Result |
|---|---|
| Evidence discovery | 77 of 111 labeled resources found; 1 extra (`account_6523`, a March lookup, correctly marked stale) |
| State reconstruction | observed state matches the label on 77 of 77 found resources |
| Evidence ids | the cited id matches the label on 58 of 58 steps that cite one |
| Findings and proposed fixes | 40 of 41 correct (the other resource was never found) |
| Checks (verification) | 77 of 77 correct on found resources; 34 resources never found |
| Stale-evidence rule | 1 resource marked stale, and it is not in the labeled answer |
| Intent, model path | the 8 weak-wording labeled samples are all classified `DELETION` (8 of 8); on the 518 real requests the hybrid gives 508 deletion and 10 not applicable |

## 4. Repeatability and the order of requests

The grader compares arrays in order, so a result that changes with the order requests arrive in, or with the number of
workers, would score differently for the same meaning (`code/ISSUES.md` issue 7).

- `code/tests/check_determinism.py`: 92 cases run in the original, reversed and shuffled order, with 1 and 8 workers: all
  results identical, including every step. Also 25 random orderings of the same observations and of the same resources give
  identical results.
- The final `output.csv` was produced from empty response caches and then regenerated from the warm cache with 8 workers: the
  two files are **byte-for-byte identical**, and the second run made 0 model calls.

## 5. What is missing, and why

34 of the 111 labeled resources are never discovered. They fall into four kinds:

| Kind | Resources | Why it is not fixed |
|---|---:|---|
| Accounts that appear only in one activity-log row | 14 | The only link is timing. A strict "exactly one candidate" rule was right 8 of 8 on labeled accounts but assigns only 37 of 550 cases, because 464 of 550 cases share a `reported_at` with another case. Not adopted (D-018). |
| Records with no identifier inside an export that several cases share (`evidence_92`) | 12 | Nothing in the data tells which case such a record belongs to. |
| Audit records only in a snapshot captured two days after `audit_at` (`evidence_02`) | 4 | The rows have no write time, so they cannot be shown to exist at `audit_at`. Using them would break the "ignore later evidence" rule. |
| In no readable evidence at all | 4 | Nothing to find. |

## 6. Additional evaluation cases created for this project

These are cases of our own, not taken from the samples. Each is a test that passes today.

| # | Case | What it checks | Result |
|---|---|---|---|
| A | **Everything wrong at once**: a running data flow, a restorable backup, a record still present, two exposed records, plus a retained audit record and a deleted account | Findings follow the statement's order; fixes are `DISABLE_DATA_FLOW`, then `BLOCK_RESTORE`, then delete, redact, anonymize; the backup is never deleted; the audit record is only verified present; findings cite evidence and fixes do not; identical under 25 shuffles | Pass (`check_planner.py`) |
| B | **A clean case with one compliant resource that cannot be cited** | `COMPLETE` contains verification steps only, each with citable evidence; the uncitable resource is left out and explained, not invented | Pass |
| C | **No assessable evidence** | Takes the documented default (`COMPLETE`, no steps, flagged) and the default is configurable | Pass |
| D | **A retained record is missing** | `INCOMPLETE`, no invented remediation, the reason is stated | Pass |
| E | **Stale data and citation edges**: the newest file was exported after `audit_at`; a record is present; another record is shown absent only by a later file | A present record cites the earlier citable observation; an absent record is never "proven" by an earlier sighting; nothing citable means nothing cited | Pass (`check_state.py`) |
| F | **Same requests, different order and worker count** | Results identical for reversed and shuffled input with 1 and 8 workers | Pass (`check_determinism.py`) |
| G | **Tricky request texts** (also with a model in the loop): "export my data and then delete my account" (mixed), "do not delete anything, I only want a copy" (negated), "remove me from marketing lists only", a Spanish request, "confirm the profile is gone" (weak cue) | Each lands on the right side; negated and weak texts are routed to the model; the model path falls back safely when unavailable | Pass (`check_classifier.py`, `check_agents.py`); on the 518 real requests the hybrid gives 508 deletion and 10 not applicable |
| H | **A model that invents ids** | A chat answer naming an id no tool returned is flagged `[unverified]`; ids a tool returned or the user wrote are not | Pass (`check_chat.py`) |
| J | **Prompt layout for provider caching** | Both system prompts stay at least 4,096 characters, contain no per-request placeholder, no labeled sample text is copied into a prompt, and a call sends the system prompt first and the customer's text last | Pass (`check_agents.py`) |
| K | **Documents versus reality** | Every path, decision and issue reference, command and test list in the docs is real, and the key numbers equal what the code and data produce | Pass (`check_docs.py`) |
| I | **Provider failure** | Retries with backoff and jitter on 429 and 5xx, a circuit breaker per model, fallback from the first to the second model, abort on 401; 12 simultaneous requests never exceed the concurrency bound; 8 identical simultaneous requests make one call | Pass (`check_resilience.py`, `check_gateway.py`) |

In total there are 14 check scripts with 291 explicit checks (three older scripts print plain counts and are not included
in that figure). All pass.

## 7. Failures found and improvements made

In the order they were found:

1. **Negated non-deletion requests were read as deletion requests.** The first classifier caught 7 of the 10 non-deletion
   texts; three said what the person is *not* asking for ("I am not asking for deletion", "Do not close the account").
   Improvement: wider, tightly limited negation patterns, still tested against a real request such as "I don't use this
   account anymore, please delete it".
2. **Python 3.9 could not run the code** (`str | None`). Improvement: a Python 3.12 environment, documented in the README.
3. **The time gate used the wrong time.** An audit record in `case_09` sits in a log exported after `audit_at`, but the
   line itself is stamped before it. Improvement: separate "what can we know" (the event's own time) from "what may we
   cite" (a file captured at or before `audit_at`).
4. **A lookup is split across two log lines** (the email on one, the resource id on the next). Improvement: join lines by
   trace id.
5. **The model rejected a custom `temperature`** (issue 2). Improvement: not sent by default; repeatability comes from the
   schema, the response cache and the fact that the 490 rule-decided requests are only confirmed by a model, never decided by one (D-020).
6. **The chat gave policy-wrong advice** (issues 3 and 4): it called a retained audit record a leftover and suggested
   deleting a backup. Improvement: a policy tool, an evidence tool and prompt rules; a grounding guard for invented ids.
7. **The chat blamed "missing" resources for a case being incomplete** when they were gaps in our discovery. Improvement:
   the tool output now says so and the chat can show the real cause (failed deletions in the log).
8. **Records were categorized by id prefix.** `case_10` and `case_12` came out wrong because a legacy contact index entry
   is a secondary identifier and a contact lookup with an email opt-in is a marketing profile. Improvement: category from
   the event type. (This is the failure that most shows the numbers are fit to the samples.)
9. **A finding cited no evidence** (`case_09`, `backup_4978`): the latest observation was not citable and the rule only
   accepted an identical state. Improvement: cite the most recent citable observation that shows the same state. A second,
   later case (two final cases) led to a wider rule for "the record exists" only, covered by tests.
10. **The first screenshot model refused one image** ("I'm sorry, I can't assist with that"). Improvement: screenshots go to
    the escalation model through the gateway, cached by content hash, fully recorded in the usage ledger.
11. **Screenshots added noise**: support case and order numbers (`CS-3245`, `ORD-...`) were counted as resources.
    Improvement: only the resource kinds we model are reported.
12. **Identical concurrent requests were paid for twice** (26 model calls where 25 were expected). Improvement: a
    concurrency bound and single-flight requests.
13. **A cache-format change orphaned the cache.** Improvement: the orphaned entries were deleted and the final run
    repopulated them, so the committed cache reproduces the final run with 0 calls.
14. **Provider prompt caching did not happen** (issue 10): the prompts were below the provider's roughly 1,024-token minimum
    (measured: 0 cached tokens on two live turns). Improvement: the intent prompt gained a decision procedure and 17 synthetic
    labeled examples and the chat prompt a glossary, with the customer's text last. Now about 90% of the final run's input
    tokens are served from the provider cache and the results did not change. The cost: input tokens rose about 3.4 times, and
    whether that is cheaper overall is unknown because the cached-token discount is unverified (D-016).
15. **Discovery gaps analysed and deliberately not "fixed" with a heuristic** (section 5): a wrong link would put another
    customer's record on a case.
16. **Another process was writing to the same usage ledger** (issue 16): a chat session in another terminal made a position
    marker useless for isolating the final run. Improvement: every ledger row is tagged with its run, and the usage report
    counts exactly those rows.
17. **The documents drifted from the code** (statements that the validator was still to be built, or that prompt caching was not working, outlived the changes that
    made them false). Improvement: `code/tests/check_docs.py` now checks every path,
    decision and issue reference, command, test list and key number in the docs against the code and the data.

## 8. Known limits and risks

- Accuracy on the unlabeled requests is unknown and likely lower than on the samples (section 1).
- 34 of 111 labeled resources are never discovered, so about 19% of the labeled steps are missing; the same gaps almost
  certainly exist in the other cases.
- The "no evidence" default (`COMPLETE`) rests on four labeled cases.
- Never exercised by a labeled sample: an expired audit-log retention period, an active legal hold, a retained record that
  is missing. They are implemented and unit tested.
- Cache entries and message-consumer lag cannot be observed in this data (issue 5), so they are never assumed clean.
- Model answers (intent for weak texts, screenshot extraction) are not independently checked; they are validated against a
  schema, cached, and flagged when the models disagree or are unavailable.

## 9. How to reproduce

```bash
python code/tests/evaluate_output.py            # section 3 (add --markdown for the tables)
python code/tests/evaluate_state.py             # discovery and state
python code/tests/evaluate_audit.py             # findings and checks
for t in ingest classifier identity evidence resilience gateway agents chat state policy determinism planner docs; do
  python code/tests/check_$t.py; done           # section 6
python code/main.py run --all --output output.csv   # regenerates the submission file
```
