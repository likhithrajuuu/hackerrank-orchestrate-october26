# PRD: Ghost Data Deletion-Audit Agent

Status: Draft v1 · Challenge: HackerRank Orchestrate, *Ghost Data Still Exists* (October 2026)
Source of truth for the contract: `AGENTS.md` §6 and `problem_statement.md`. This PRD does not override them.

---

## 1. Problem

A deletion workflow can report **Complete** while personal data survives in replicas, logs, support attachments, exports, search indexes, analytics, archives and backups. Active sync jobs and restore paths can also recreate data that was deleted. Reported statuses are claims, not proof.

We need an offline, terminal-runnable agent that, for each of the 518 requests in `dataset/deletion_requests.csv`, decides what is true at that case's `audit_at` and emits a structured plan.

## 2. Goals

| # | Goal | Measured by |
|---|------|-------------|
| G1 | Correctly classify each case as `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` | Case-level status accuracy |
| G2 | Resolve current *and* historical identifiers to one subject | Identity resolution, discovered records |
| G3 | Find every violating record, active data flow and restore risk, citing visible evidence | Step and field accuracy |
| G4 | Produce a correctly ordered finding → remediation → verification plan | Sequence accuracy |
| G5 | Never destroy data that policy requires us to keep | Retained-record checks |
| G6 | Be deterministic, cheap and reproducible | Re-run produces identical `output.csv`; usage report |

### Non-goals

- Executing any deletion, redaction or anonymization. The agent only **plans and verifies on paper**.
- Revoking sessions or credentials. The allowed operation set (§7) has no such operation, and we must not invent operations.
- Live-service concerns: Kafka, Postgres row locks, request queues. This is a batch job over static files (see §10).
- Using anything outside `dataset/` for predictions. Organizer-only files, golden labels and case-family shortcuts are forbidden.

## 3. Inputs

All under `dataset/`. Observed shape (sampled from the actual files):

| File | Key facts |
|------|-----------|
| `deletion_requests.csv` | 518 cases, `case_id` unique, 517 distinct emails (one email appears in two cases). |
| `user_identities.csv` | 850 users. One row per `user_id`: name, email, phone, `email_hash`, `updated_at`. |
| `user_audit_log.csv` | Tracks only `email`, `email_hash`, `phone` changes: `audit_id, user_id, event_time, field_name, old_value, new_value`. |
| `system_activity.csv` | Global stream. About 17 event types (DELETE_RECORD 890, UPDATE_RECORD 672, SYNC_RECORD 447, INDEX_RECORD 400, ANONYMIZE_RECORD 204, ...). `reported_result` is a claim. |
| `deletion_rules.csv` | 15 rules, category → action (see §6). |
| `evidence_files.csv` + `evidence/` | Manifest of about 94 files: 24 SQL snapshots, 29 JSON/record files, 26 logs, 11 PNG, 4 PDF. The manifest has no case or user link. |
| `sample_*` | 32 labeled cases (22 INCOMPLETE, 10 COMPLETE). Our only ground truth for tuning. |

Observed facts that shape the design:

- **Every request email resolves** to a `user_id` in this dataset. 100 of 518 evaluation cases resolve **only** through an old email in the audit log.
- `email_hash` = **SHA-256 of the lowercase email, unsalted** (verified on a current and a historical hash). Hashes are therefore computable for any known email.
- `LINK_RESOURCE_IDENTITY` events (12 rows, source `data_catalog`) explicitly tie a resource to an email and `user_id`.

## 4. Users and stakeholders

- **Grader**: hidden canonical answers; compares status, identity resolution, discovered records, cited evidence, policy decisions, step completeness and order.
- **Participant/interviewer**: reads `README.md`, the evaluation and usage reports, and `log.txt`; asks about architecture and tradeoffs.

## 5. Decision pipeline

```
request row
  │
  ▼
[1] Intent gate ──────────── not a deletion request ──► NOT_APPLICABLE, steps = []
  │ deletion
  ▼
[2] Identity resolution ──► subject set S = {user_id, all emails, hashes, phones, names}
  │                          (only audit entries with event_time <= audit_at)
  ▼
[3] Evidence sweep ───────► candidate records / flows / restores linked to S
  │                          (only evidence with captured_at <= audit_at,
  │                           activity with event_time <= audit_at)
  ▼
[4] State reconstruction ─► per-resource true state at audit_at
  │                          (claims in activity/reported_status do NOT count as proof)
  ▼
[5] Policy application ───► required treatment per record (deletion_rules.csv)
  │
  ▼
[6] Compare state vs policy ──► violations, retained-OK, active flows, restore risks
  │
  ├─ none ──► COMPLETE   (VERIFICATION steps only, each citing evidence)
  └─ any  ──► INCOMPLETE (FINDING + REMEDIATION + VERIFICATION)
  ▼
[7] Step builder + validator ──► deterministic ordering, JSON, row
```

### 5.1 Intent gate

Only `request_text` decides. It can be in any language. Classify as deletion or non-deletion (access, correction, export, marketing opt-out). Mixed or ambiguous text ("export my data and delete it", "stop emailing me") must be handled explicitly. A non-deletion intent returns `NOT_APPLICABLE` with `[]`, even if the other fields look like a deletion case. `system_activity.csv` also contains `CLASSIFY_REQUEST` events (10 rows). Treat them as claims, not authority.

### 5.2 Identity resolution

**Anchor on the stable `user_id`.** Names are never an anchor.

1. Look up the request email (case-insensitive, trimmed) in the current snapshot, then in audit `old_value` and `new_value` for `field_name=email`.
2. Having a `user_id`, build the identifier set S from the snapshot plus every audit entry with `event_time <= audit_at`: all emails, all `email_hash` values, all phones. Also add `sha256(email)` for each email, so hash-only records can be matched.
3. Widen with grounded links: `LINK_RESOURCE_IDENTITY` rows, resource IDs that embed the user (for example `account_010` ↔ `user_10`), and `user_id` fields found inside evidence.

**Time-travel caution.** Take the identifier set as of `audit_at`. Audit entries after `audit_at` must not add identifiers or change the answer. Deciding whether the *current* snapshot row itself post-dates `audit_at` is an open question (§13).

#### Edge case: records with no `user_id` (the "ghost")

Some records carry no `user_id` and no name, for example an analytics row with `user_id`, `email`, `phone` and `email_hash` all `null`. Rules:

- Attribute such a record to the subject **only** through a grounded link: an exact email or hash match, a catalog link event, or a resource ID tied to the subject. Never through name, fuzzy similarity or "it looks plausible".
- If the record is **already anonymized** (all direct identifiers null) and nothing grounds it to the subject, it is not reportable. Report nothing. It is the *desired end state*, and verifying it can be a `VERIFY_RECORD_ANONYMIZED` step when evidence ties it to the case.
- If no link exists at all, the agent does not guess. Log it as unattributed and leave it out.
- If the **request email itself resolves to no `user_id`**: this does not occur in the current data, but the agent must not crash. Fall back to a direct identifier search on that email and its hash. If nothing grounds the person, prefer a conservative result and flag the case in the run log (decision in §13).

#### Edge case: decoys

The global files are full of unrelated people. Examples already seen: `nadia.popescu@yahoo.com` and `customer4104@yahoo.com` next to the real subject `nadia.ionescu@yahoo.com`; `ingrid.dahl@outlook.com` appears in two cases.

- Match on **exact** identifiers (email, hash, phone, `user_id`, resource ID). Reject near-matches and same-name-different-person.
- Treat a repeated email across cases as one person with two independent audits, each with its own `audit_at`.

### 5.3 Evidence ingestion (offline)

There is no streaming ingestion. Everything is a static file, read once and indexed.

| Source | Format | Parsing |
|--------|--------|---------|
| `system_activity.csv` | CSV | Index by `resource_id`, `event_type`, `event_time`. Filter by `audit_at`. |
| Logs (`evidence/logs/*.log`) | Key=value lines | Regex-parse `ts, level, service, event, resource_id, lookup_key, marker_found, last_write ...`. |
| Records (`evidence/records/*.json`) | JSON | Walk the `records[]` array. Each has `resource_id` and identifier fields. Includes analytics and telemetry data. |
| Databases (`evidence/databases/*.sql`) | SQLite SQL | Execute into an in-memory SQLite database per file. Compare `*_primary` against `*_replica` tables for drift. |
| Documents (PDF) / Images (PNG) | Binary | Extract text (PDF) and use a vision/OCR model (PNG). Treat the result as extracted facts to be matched, not as a verdict. |

Every file is gated by `captured_at <= audit_at` from `evidence_files.csv`.

**Analytics and telemetry specifically.** There is no log-ingestion pipeline. Analytics profiles and product telemetry reach us as:

1. JSON records, for example `customer_analytics_report_*.json`, with per-record metrics and `recent_activity`;
2. rows in `system_activity.csv` (`SYNC_RECORD account_service → analytics_platform` 360, `ANONYMIZE_RECORD ... analytics_platform` 204);
3. screenshots such as `analytics_profile_view_*.png`.

Policy (rules 07, 08): a record **still attributable to a user** must be anonymized. A record whose direct identifiers are already null is compliant.

### 5.4 State reconstruction (claim vs. evidence)

A resource's state at `audit_at` is established by the strongest available **artifact** (a SQL row, a log line with `marker_found`, a JSON record, a screenshot). An activity row saying `SUCCESS` is only a claim. Examples:

- `DELETE_RECORD ... SUCCESS` followed by evidence that the row exists, or was written after the delete → still `PRESENT`.
- `SYNC_RECORD` from an active job after the deletion time → an active data flow that can recreate the data.
- A replica that still contains a row the primary does not → replication drift; the stale copy is a remaining record.
- A `RESTORE_BACKUP` or accessible `BACKUP_ARCHIVE` without `BLOCK_RESTORE` → restore risk.

### 5.5 Policy application

`deletion_rules.csv` is the only source of required treatment.

| Treatment | Categories |
|-----------|-----------|
| **DELETE** | `USER_PROFILE`, `SECONDARY_IDENTIFIER`, `SEARCH_INDEX_ENTRY`, `CACHE_ENTRY`, `USER_EXPORT`, `MARKETING_PROFILE`, `SUPPORT_ATTACHMENT` (only if it belongs solely to the subject) |
| **REDACT** | `SUPPORT_MESSAGE` (shared context), `EMAIL_DELIVERY_LOG` (contains email), `ACCOUNT_TOMBSTONE` (keep `user_id` only) |
| **ANONYMIZE** | `ANALYTICS_PROFILE`, `PRODUCT_TELEMETRY` (if still attributable) |
| **RETAIN** | `SECURITY_AUDIT_LOG` (within the retention period), `LEGAL_HOLD_RECORD` (hold active), `BACKUP_ARCHIVE` (restore can be blocked) |

Conditions are checked against evidence, for example the retention period in the security extract and an active hold in the legal register. A retained record must be **available**, not deleted. Over-deleting a retained record makes the case wrong.

### 5.6 Output construction

- Phases in order `FINDING` → `REMEDIATION` → `VERIFICATION`. Step numbers 1..n consecutive.
- Within a phase: the operation order in the allowed-operations table, then `system_id` ascending, then `resource_id` ascending.
- Remediation dependency rule: disable data flows and block restores **before** changing dependent records; downstream copies before their source.
- Evidence rule: findings and `COMPLETE` verification steps cite a `file_id`, `activity_id` or `audit_id` captured/occurring at or before `audit_at`. Remediation and future verification steps use `""`.
- `NOT_APPLICABLE` → `[]`. Rows sorted by `case_id`. Exactly 518 rows.

## 6. Functional requirements

| ID | Requirement |
|----|-------------|
| FR1 | Read only `dataset/`; write `output.csv` at the repo root; runnable from the terminal. |
| FR2 | Exactly one valid row per `case_id`, sorted. |
| FR3 | Enforce the `audit_at` cutoff on audit entries, activity and evidence captures. |
| FR4 | Resolve identities via current + historical emails, hashes, phones and `user_id`. |
| FR5 | Apply every rule in `deletion_rules.csv`; never remove RETAIN or tombstone references. |
| FR6 | Deterministic output: stable sort, canonical JSON, cached model results. |
| FR7 | Validate every row before writing (see §9). |
| FR8 | Secrets only from environment variables; never in output, logs or the zip. |
| FR9 | Append every session and turn to `log.txt` per `AGENTS.md` §5. |

## 7. Architecture

Mostly deterministic code, with a model only for the fuzzy parts. (Updated to the layout that was built; see
`DECISIONS.md` D-004, D-009, D-010, D-013, D-018.)

```
code/
  main.py, cli.py        entry point and commands (run, case, classify, chat, usage-report, status)
  loaders.py, config.py  typed CSV loaders, paths and time parsing
  classifier.py          regex intent rules; agents/intent.py routes weak and ambiguous texts to a model
  identity.py            resolve(email, audit_at) -> Subject (current and historical identifiers)
  evidence/              readers for SQL, JSON, logs, text, PDFs, screenshots behind one time-gated store
  state.py               stage 4: evidence -> observed state per resource at audit_at
  policy.py              stage 5: required treatment from deletion_rules.csv
  audit.py               stage 6: verdict per resource and the case status
  planner.py             stage 7a: ordered findings, remediation, verification (canonical order)
  validator.py           stage 7b: every row checked before it can be written
  output.py              stage 7c: output.csv writer and file check
  graph.py, agents/      the per-case flow (LangGraph) and its single-purpose agents
  llm/                   gateway, retries with backoff and jitter, circuit breaker, caches, usage ledger
  reporting.py, reports.py   text views and the generated usage report
  cache/                 cached model answers and screenshot extractions (no secrets)
  tests/                 14 check scripts and 3 scorers against the labeled samples
  evaluation/            evaluation_report.md, usage_report.md
README.md                setup and run instructions
```

Model use is limited to (a) intent classification of weak and ambiguous request wording, (b) reading screenshots, and (c) a
read-only chat. Everything else is code, so results are reproducible and cheap.

## 8. Anonymization semantics (what the plan means)

The agent never rewrites data. `ANONYMIZE_RECORD` / `VERIFY_RECORD_ANONYMIZED` describe the required end state:

- The record can no longer be linked to the person: no `user_id`, email, phone, `email_hash`, name or other direct identifier.
- Aggregate value is preserved (metrics and event types remain).
- **A hash is not anonymization.** An unsalted hash of an email is trivially reversible (we verified `email_hash` is a plain SHA-256). A keyed hash (HMAC) is pseudonymization: still personal data if anyone holds the key. A true anonymous token would be a fresh random value with no stored mapping.
- `REDACT` removes specific fields and keeps the record (support messages, delivery logs). The tombstone is the one place a stable reference (`user_id`) is deliberately kept.

## 9. Validation and failure handling

Pre-write checks per row: valid JSON array; consecutive `step_no`; valid phase, operation and state pairing; phase and sort order; exactly the seven fields; `NOT_APPLICABLE` ⇒ `[]`; `COMPLETE` ⇒ only `VERIFICATION`; evidence IDs exist and are not later than `audit_at`.

Failures:

- **Transient model/API errors**: bounded retry with exponential backoff; cap total attempts.
- **Resumability**: cache results per case and per file so a rerun does not repay for finished work.
- **Persistent failure on a case**: fall back to the deterministic path where possible; otherwise fail loudly and list the case. Never silently emit `COMPLETE`.
- **Never emit a malformed row.** A run that cannot produce 518 valid rows exits non-zero.
- **Concurrency**: parallelism only across cases, bounded for rate limits. Output assembly is single-threaded and sorted.

## 10. Why queues, locks and backpressure are out of scope

Processing is a single offline batch over fixed files, and no state changes while it runs. Idempotency is achieved by determinism (same input → same output), not an idempotency key. "Data changed a second after I queried" is handled by the `audit_at` filter: the answer is defined at a fixed point in time, so a later change cannot alter it.

## 11. Evaluation plan

- Run on the 32 labeled samples; report status accuracy, step-level, field-level and sequence accuracy.
- Hold out samples by family to avoid tuning to a few cases.
- Add at least three custom cases (suggestions): (1) old-email-only request with a hash-only record in a log, (2) evidence captured after `audit_at` that must be ignored, (3) a non-deletion request mixed with deletion language.
- Report failures found and improvements made in `evaluation/evaluation_report.md`; report model, calls, tokens and cost in `evaluation/usage_report.md`.

## 12. Deliverables

`code.zip` (code, prompts, config, README, `evaluation/`; no `dataset/`, venvs, caches, secrets), `output.csv`, `log.txt` as the chat transcript, and the AI interview.

---

## Appendix A: Worked example: identity resolution (sample `case_10`)

**Request.** `nadia.ionescu@yahoo.com`, "I used this Yahoo address before changing the email on my account. Please remove records linked to both the old and current addresses." `audit_at = 2026-09-18T12:25:00Z`, `reported_status = COMPLETE`.

**Step 1: intent.** Explicit deletion across old and current addresses → proceed.

**Step 2: resolve.** `nadia.ionescu@yahoo.com` is not in `user_identities.csv`. It is the `old_value` of `audit_121`, which belongs to `user_10`.

| Time | Audit ID | Field | Old → New |
|------|----------|-------|-----------|
| 2025-12-13 | audit_121 / audit_122 | email / email_hash | `nadia.ionescu@yahoo.com` → `nadia.petrescu@yahoo.com`; hash `708ef5…` → `e407f5…` |
| 2026-04-12 | audit_238 / audit_239 | email / email_hash | `nadia.petrescu@yahoo.com` → `n.petrescu@yahoo.com`; hash `e407f5…` → `6385b5…` |

Current snapshot: `user_10`, Nadia Petrescu, `n.petrescu@yahoo.com`, `+40 723 481 638`.
Subject set S = emails {`nadia.ionescu@…`, `nadia.petrescu@…`, `n.petrescu@…`} + their three SHA-256 hashes + the phone + `user_10`. (The surname changed, so a name match on "Ionescu" would also have missed her.)

**Step 3: sweep and decoy rejection.** `evidence_61` (`evidence/logs/support_replica.log`, captured 2026-09-17T22:00:19Z, before `audit_at`, so it is usable) contains:

```
... trace_id=tr-rp-002 event=contact_query_started lookup_key=nadia.ionescu@yahoo.com
... trace_id=tr-rp-002 event=contact_query_completed resource_id=support_002 rows=1
... trace_id=tr-rp-002 event=deletion_marker_check resource_id=support_002 marker_found=false last_write=2026-09-07T10:01:00Z
... resource_id=support_5914 lookup_key=nadia.popescu@yahoo.com ...      <- decoy, different person
... resource_id=support_4104 lookup_key=customer4104@yahoo.com ...       <- decoy
```

Exact lookup-key match ⇒ `support_002` belongs to the subject. `nadia.popescu@…` is similar but not in S ⇒ ignore.

**Step 4: claim vs. evidence.** `system_activity.csv` has `activity_3596 DELETE_RECORD support_platform support_002 SUCCESS` (2026-09-02T10:09:01Z). That is only a claim. The log shows no deletion marker and a `last_write` of 2026-09-07, after the "successful" delete. State at `audit_at`: `support_002` is **PRESENT**. The primary account `account_010` was deleted successfully.

**Step 5: expected output (from `sample_output.csv`).**

| # | Phase | System | Resource | Operation | State | Evidence |
|---|-------|--------|----------|-----------|-------|----------|
| 1 | FINDING | support_platform | support_002 | REPORT_REMAINING_RECORD | PRESENT | evidence_61 |
| 2 | REMEDIATION | support_platform | support_002 | DELETE_RECORD | ABSENT | `""` |
| 3 | VERIFICATION | account_service | account_010 | VERIFY_RECORD_ABSENT | ABSENT | `""` |
| 4 | VERIFICATION | support_platform | support_002 | VERIFY_RECORD_ABSENT | ABSENT | `""` |

Note the sample verifies `account_010` even though it was not remediated: verification appears to cover every record that must end up deleted, not only the ones fixed. (Confirm against more samples; see §13.)

## Appendix B: Worked example: anonymization (sample `case_04`)

**Request.** `thandiwe.n@gmail.com` (current email of `user_04`, Thandiwe Ndlovu): "You can keep anonymous statistics, but nothing in analytics should still point back to me." Reported `COMPLETE`; expected `COMPLETE`.

**Evidence.** `evidence_56` (`customer_analytics_report_20260907.json`, captured 2026-09-07T09:45:00Z, before `audit_at` 2026-09-07T10:20:00Z) holds two records:

| resource_id | user_id | email | phone | email_hash | Verdict |
|-------------|---------|-------|-------|-----------|---------|
| `analytics_901` | null | null | null | null | **Already anonymized**: metrics and activity remain, every direct identifier is null |
| `analytics_824` | `user_824` | `dahl.freja@…` | `+46 70 …` | `d61fb6…` | Attributable, but to **someone else** (decoy) |

**Result.** `COMPLETE` with verification steps only, each citing evidence:

| # | Operation | System | Resource | State | Evidence |
|---|-----------|--------|----------|-------|----------|
| 1 | VERIFY_RECORD_ABSENT | account_service | account_004 | ABSENT | evidence_54 (a failover-replica snapshot that no longer contains it) |
| 2 | VERIFY_RECORD_ANONYMIZED | analytics_platform | analytics_901 | ANONYMIZED | evidence_56 |

**What "anonymized" looks like in this data.** Compare a subject record before anonymization (shape of `analytics_824`) with the compliant `analytics_901`:

```json
// attributable (violates rule 07)             // anonymized (compliant)
{ "resource_id": "analytics_824",              { "resource_id": "analytics_901",
  "user_id": "user_824",                         "user_id": null,
  "email": "dahl.freja@icloud.com",              "email": null,
  "phone": "+46 70 226 32 84",                   "phone": null,
  "email_hash": "d61fb6…",                       "email_hash": null,
  "metrics": { "sessions_30d": 6, ... } }        "metrics": { "sessions_30d": 11, ... } }
```

No random token or hash replaces the identifiers: the fields are nulled and the aggregate metrics survive. That is what the grader expects `VERIFY_RECORD_ANONYMIZED` to confirm.

**Open question this example exposes (§13):** `analytics_901` carries no identifier in the evidence, and I did not find a text-searchable link from it to `user_04`. The link may live in an image/PDF (not yet inspected) or rest on other signals such as activity timing. The agent must not attribute it on a guess; the linking rule for fully anonymous records has to be settled by studying more samples.

---

## 13. Open questions and risks

| # | Item | Plan |
|---|------|------|
| Q1 | How exactly is a fully anonymous record (like `analytics_901`) tied to its case? Not found in text evidence. | Inspect images/PDFs and other samples; define the rule empirically on the 32 labeled cases. |
| Q2 | Does verification cover every must-be-absent/retained record, or only those touched? | Derive from sample outputs across all 32 cases. |
| Q3 | If the current snapshot row's `updated_at` is later than `audit_at`, do we roll identifiers back via the audit log? | Test on samples; keep rule time-consistent. |
| Q4 | Request email resolving to no `user_id` (none today). | Conservative fallback plus a flagged log line. |
| Q5 | Multilingual intent edge cases and mixed requests. | Model classification with a labeled mini-set, including the custom cases. |
| Q6 | OCR/vision accuracy on screenshots and PDFs. | Cache extractions; cross-check against SQL/JSON when both exist. |
| R1 | Overfitting to 32 samples. | Hold-out by scenario family; write rules from the policy, not from case shortcuts. |
| R2 | Cost and non-determinism from model calls. | Deterministic core; temperature 0; cache everything. |


### Status of the open questions (updated after the build)

| # | Question | Status |
|---|---|---|
| Q1 | How a fully anonymous record is tied to its case | **Not solved.** Nothing in the data links it; 12 resources stay undiscovered (D-018). |
| Q2 | Whether verification covers every record that must end up deleted | **Answered** by the labeled samples: every assessed resource gets a verification step. |
| Q3 | Whether the current snapshot counts as held at `audit_at` | Handled by using audit entries up to `audit_at` only; not contradicted by any sample. |
| Q4 | A request email that resolves to no user | Never occurs in the data; handled (unresolved identity is flagged). |
| Q5 | Multilingual and mixed intent | Done: regex for strong cues, a model for weak and ambiguous ones (D-002). |
| Q6 | Screenshot and PDF accuracy | All 11 screenshots and 4 PDFs read; screenshots by the escalation model, cached by content hash (D-018). |
