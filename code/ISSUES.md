# Issues that I face during the programming 

### 1. Python Version Problem
My computer's venv had the 3.9 version which does not support `str | None` when researched, I figured out that it requires a Python version of the 3.10+ 
So ran the following commands as there were no requirements at this point when I am typing. Please check the following commands


Remove the present Python 3.9
```bash
deactivate 2>/dev/null
rm -rf .venv
```

Create a new virtual environment with Python 3.12
```bash
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
```

Checking the python version
```bash
python --version
```

### 2. OpenAI rejects `temperature` for the Luna and Sol models
I picked **Luna** as the primary model (reasoning effort `high`) and **Sol** as the escalation model (reasoning effort `low`), and I wanted a low temperature so the classifier would be repeatable. The very first live call failed:

```text
configuration error: primary: OpenAIInvalidRequestError: Error code: 400 - {'error': {'message': "Unsupported parameter: 'temperature' is not supported with this model.", 'type': 'invalid_request_error', 'param': 'temperature', 'code': None}}
```

So with reasoning-effort models OpenAI does not support temperature customization: the model only accepts its own default, and any `temperature` value (even 0) is a 400 error.

**What happened:** the app treated the 400 as a permanent error, stopped with exit code 2 and printed a hint, so nothing was silently flagged.

**Fix:** the code no longer sends a temperature by default (`DEFAULT_TEMPERATURE = "none"` in `code/llm/config.py`). If a model that does accept one is used later, set `LUNA_TEMPERATURE=0` or `SOL_TEMPERATURE=0` in `.env`. Repeatability comes from the schema-validated output and the response cache instead. See D-007 in `DECISIONS.md`.


### 3. The chat assistant treated a retained security audit record as a leftover
While testing `python code/cli.py chat` on `case_14`, the assistant listed `audit_record_987` (still `FOUND` in the evidence) as part of the incomplete deletion.

That is wrong. Policy rule 12 says a `SECURITY_AUDIT_LOG` inside its retention period is **RETAINED**, so the record being present is the correct final state, and the verification step for it is `VERIFY_RECORD_PRESENT`, never a delete.

**Cause:** the assistant had no access to the deletion policy, so it assumed "anything still present is a problem".

**Fix:** added a `deletion_policy` tool that returns the 15 rules, and a rule in the assistant's instructions to check the policy before saying whether a record that is still present is a problem. After the fix it answered: "presence is not itself a problem ... don't delete it".

### 4. The chat assistant suggested deleting a backup instead of blocking restores
In the same `case_14` test, asked what the fix for `backup_3870` should be, the assistant said to "remove the active archive".

That contradicts policy rule 14: a `BACKUP_ARCHIVE` is **RETAINED** when restore access can be blocked. The archive stays, and the fix is `BLOCK_RESTORE` followed by `VERIFY_RESTORE_BLOCKED`. The labeled answer for `case_14` says exactly that.

**Cause:** the same missing policy knowledge as issue 3. Without it the model fell back on a generic "delete everything" idea.

**Fix:** the `deletion_policy` tool now also lists the available remediation operations and states that a RETAIN category must not be deleted (a retained backup is handled by blocking restores). The rerun answered: "apply `BLOCK_RESTORE` and verify restore access is blocked; do not delete the archive."

**Lesson for both:** an AI assistant that is not given the policy will give plausible but wrong advice. The deterministic code owns the facts and the rules; the AI should only read them through tools and explain.


### 5. Stale data from Kafka-style consumers and Redis-style caches
A deletion can look finished at the source while a message consumer that is behind, or a cache that refreshes from an old
copy, brings the data back. What the dataset actually contains:

- **Consumer lag (Kafka-style):** `queue_consumer` warnings such as `topic=notification_events partition=2 lag=33` and
  `lag=63` (also on a replica), plus `checkpoint_saved offset=...` lines. A consumer that is 63 messages behind has not yet
  applied the newest events, deletions included. None of these lines carries a resource id, so they cannot be tied to any
  customer.
- **Cache (Redis-style):** 284 `REFRESH_CACHE` activity rows ("Profile cache refresh processed cache_9558"), but **zero**
  cache entries in any evidence file. Policy rule 4 says cache entries are always deleted, yet their state can never be
  verified, because only the activity log mentions them and that log is a set of claims.
- **Sync (recreation):** 447 `SYNC_RECORD` rows (381 SUCCESS, 46 PARTIAL, 20 FAILED), all claims. A sync that runs after a
  deletion can recreate the record in the target system.
- **Old snapshots:** a replica snapshot from before the deletion still shows the record. Treating it as current gives a
  false "still there"; ignoring every old file hides a copy that really is still there.

**Where it stands:** claims never set a state, the latest evidence wins, and evidence from before the request is marked
STALE (D-009, D-010, D-017). **Not solved:** cache entries and consumer lag cannot be observed at all in this data, and a
copy that exists only in an older snapshot is not reported.

### 6. Many workers at once: rate limits and paying twice for the same question
Running 518 cases in parallel against a rate-limited API invites 429 errors, and two workers asking the same question at the
same moment both miss the cache and both pay. The first live run made 26 model calls where 25 were expected; the likely
cause is two workers asking for the same text at once (not confirmed). Both problems are fixed: at most 4 calls per model are
in flight, and identical simultaneous requests are single-flighted into one call (D-011, D-016). Tested with 12 threads and
with 8 identical simultaneous requests.

### 7. The order of requests and of steps can change the score
The grader compares `steps_json` as an ordered array. Two plans that mean exactly the same thing but list their steps in a
different order would be scored differently. The same risk exists inside our own code: the order requests arrive in, the
number of workers, and the iteration order of sets and dictionaries must never change an answer.

**Fix:** every output has one canonical order (D-014): cases by `case_id`, assessments by system and resource id, and later
the steps by phase, then the operation order in the problem statement, then system id, then resource id.
`code/tests/check_determinism.py` runs 92 cases in original, reversed and shuffled order with 1 and 8 workers and demands
identical results. The planner was built later and is covered by the same test.

### 8. Fixes have to be applied in dependency order
Deleting a record while the sync that feeds it is still running, or while a backup can still restore it, only brings the
record back. The safe order is: disable data flows, then block restores, then change records (delete, then redact, then
anonymize), then verify. This is also the order the problem statement requires ("disable data flows and block unsafe
restores before changing dependent records"). Built as the dependency-aware planner (D-013, D-018).

### 9. The AI can hallucinate
Two real examples are issues 3 and 4: a retained audit record called a leftover, and a backup recommended for deletion. A
chat model can also invent a case, resource or evidence id that looks right. Measures in place (D-012, D-015): the AI never
produces statuses, findings or steps; its structured answers are schema-validated; low-confidence answers escalate to the
second model; and the chat flags any id in an answer that no tool returned (`[unverified]`). The step validator (built) checks
that every cited id exists and was captured at or before `audit_at`.

### 10. Provider-side prompt caching was not happening (fixed)
Asked to make sure prompt caching works, I measured it: two live chat turns, 800 and 911 input tokens each,
`provider_cached = 0`. The provider's automatic prompt cache needs a shared prefix of about 1,024 tokens or more, and these
prompts were below it (the intent prompt was about 380 tokens). My first answer was to leave it, which was wrong: you wanted
it working.

**Fix:** the prompts were made long enough with content that helps: a decision procedure and 17 synthetic labeled examples
for the intent model, and a glossary for the chat. A test now protects the layout (fixed system prompt first, customer's text
last, at least 4,096 characters). Measured afterwards: 88% of input tokens cached on repeated intent calls, about 90% over the
final run, and 90 to 99% on chat turns. The results did not change (`output.csv` byte-identical).

**What it cost:** total input tokens rose about 3.4 times (8,766 to about 29,000 in the final run). Whether that is cheaper
overall depends on the discount for cached tokens, which I have not verified. See D-016.

### 11. The first screenshot model refused one image, and the reader crashed on it
`gpt-4o` answered "I'm sorry, I can't assist with that." for the object-storage screenshot (`evidence_46`), which lists
customer file names. The reader then did `json.loads(None)` and failed, and because it was retried on every run it also made
runs slow. The same reader called the API directly, so its usage was missing from the ledger, and its cache was keyed by
file id, which would serve a stale extraction if a screenshot were ever replaced.

**Fix:** screenshots now go to the escalation model (as D-001 intended) through the gateway, with retries, the circuit breaker
and the ledger; the cache is keyed by the image's content hash. All 11 screenshots read. See D-018.

### 12. Screenshots add things that are not resources
Reading the screenshots with the new model added "resources" such as `CS-3245`, `FIN-7742` and `ORD-20263245` (support case,
finance and order numbers shown on screen). They are not resources of any system and showed up as unresolved noise.
**Fix:** only the resource kinds we model count as discovered.

### 13. 34 of the 111 labeled resources are never discovered
They are 14 accounts seen only in one activity row, 12 identifier-less records in an export that several cases share, 4 audit
records only in a snapshot captured after `audit_at`, and 4 in no readable evidence. I measured the one candidate rule that
could be sound (link an account when exactly one deletion row fits): right 8 of 8, but it assigns only 37 of 550 cases
because 464 of 550 cases share a `reported_at`. Not adopted. The cost is 36 of the 193 labeled steps. See D-018 and the
evaluation report.

### 14. A fresh checkout could not start: empty `.env.example` values crashed the settings, and the README overclaimed
Testing the clean-checkout path found two problems. Copying `.env.example` (which has empty price lines) made `float("")`
raise, so startup failed. And I had written that no key is needed to reproduce `output.csv`, which is only true with a
placeholder key: with no key at all the run stops with a configuration error.

**Fix:** empty values now mean "not configured"; `.env.example` names the two models; the README says exactly what was
verified (a placeholder key and an unreachable endpoint reproduce a byte-identical `output.csv`). Both are covered by tests.

### 15. Changing the cache key orphaned the cache
Adding image support to the gateway changed the response-cache key format, so every earlier cached answer stopped matching
and the "final run" re-asked the model (23 calls instead of 0). It cost about 8.8k tokens, but it would have made the
committed cache useless. **Fix:** the orphaned entries were deleted and the final run repopulated the cache, so the committed
cache reproduces the final run with 0 calls. Lesson: bump the cache version deliberately and regenerate the cache in the same
change.


### 16. Another process was writing to the same ledger and cache
While I was measuring usage, extra chat rows and an extra intent call kept appearing in the usage ledger "after the final
run". The cause was not a bug in the pipeline: a chat session in another terminal (probably yours, testing prompt caching)
was writing to the same `code/cache/usage.jsonl` and the same response cache. A marker line in the ledger cannot tell two
concurrent processes apart, and my own habit of snapshotting and restoring the ledger could have dropped some of that
session's rows.

**Fix:** every ledger row is now tagged with the run it belongs to (`run: "final-run"` for the process that writes
`output.csv`), and the usage report counts exactly those rows. Chat and other processes are reported separately as
development usage. I stopped restoring the ledger. Covered by a test that writes from two ledgers into one file.
