# Decisions

A running log of design decisions for the Ghost Data deletion-audit agent. Each entry records the context, the
decision, and what still has to be verified, so the reasoning survives into the evaluation report and the AI interview.

Status values: **Accepted** (decided), **Proposed** (needs confirmation), **Superseded** (replaced by a later entry).

---

## D-001: Model roles and token budget

- **Date:** 2026-10-25
- **Status:** Accepted. The figures below are the author's estimates and have not been independently verified (see
  "Verification needed").

### Context

The pipeline is deterministic by design (identity resolution, time gating, ordering, JSON validation are plain code).
A model is used only for the fuzzy parts: judging the intent of `request_text`, extracting facts from logs, JSON and
other text evidence, and reading PDFs and screenshots. There are 518 cases and an overall budget of about **5 USD**.

### Decision

Two model roles, a cheap primary and a stronger escalation:

| Role | Model | Reasoning effort | Used for |
|---|---|---|---|
| Primary | **Luna** | high | Classifying `request_text`; extracting facts from logs, JSON and other text evidence. |
| Escalation | **Sol** | low | PDFs and screenshots; tricky evidence; heavy reasoning tasks; fallback when Luna fails or cannot decide. |

Routing:

1. **Code first.** Rules and exact lookups answer everything they can; no model call.
2. **Luna** handles the text work the rules are unsure about.
3. **Sol** is called when the input is a PDF or image, when Luna errors or returns invalid or unclear output, or when
   the evidence needs multi-step reasoning.
4. If both fail, the case is flagged `needs_review` and takes the safe default (never silently guess `COMPLETE`).

### Budget arithmetic (the author's figures, plus what follows from them)

| Item | Value |
|---|---|
| Cases | 518 |
| Assumed input per case | about 5,000 tokens |
| Total input | 518 x 5,000 = **2,590,000 tokens (about 2.6M)** |
| Luna cost for that input (author's estimate) | **about 0.28 to 0.30 USD** |
| Implied Luna input rate (derived) | about 0.11 USD per 1M input tokens |
| Sol cost per task (author's estimate) | about 0.03 USD |
| Overall budget | about 5 USD |
| Headroom for Sol after Luna (derived) | about 4.70 USD, or roughly 150 Sol tasks at 0.03 USD |

Sol's expected workload is small: 11 screenshots and 4 PDFs, plus escalations, so the budget leaves a wide margin.

Benchmark figures quoted by the author (Artificial Analysis): **intelligence index 33**, **cost per task about 0.03 USD**,
**AA-LCR about 79.3%**. These were given as the basis for the Sol(low) choice; confirm which model each number belongs to.

### Rationale

- A cheap model with high reasoning effort covers the high-volume text work for pennies.
- Sol at low effort is reserved for the inputs a cheap model is worst at (images, PDFs, multi-step reasoning) and
  doubles as the fallback, so a Luna failure has a defined recovery path.
- The deterministic core keeps results reproducible and keeps most cases from needing any model call.

### Caveats

- **The 5k tokens per case is an upper bound.** It assumes every case sends its evidence context. The hybrid
  classifier alone should send only the uncertain texts (about 23 distinct texts at last measurement), which costs far less.
- **The 2.6M figure counts input only.** Output tokens are billed too, and reasoning models bill hidden reasoning
  tokens as output. At **high** effort, Luna's real cost could be noticeably above the input-only estimate. Measure it.
- **Caching matters.** Many texts repeat (375 distinct request texts across 518 cases), so a disk cache keyed by model,
  prompt version and input removes repeat cost and keeps reruns identical.

### Measured result (2026-10-25, first live run of the hybrid intent classifier)

With this decision in place (Luna at high effort for the weak and ambiguous requests only), the real token use was
far below the estimate:

| Item | Assumed | Measured |
|---|---|---|
| Input tokens per model call | about 5,000 | **about 380** (9,865 input tokens over 26 calls) |
| Output tokens per model call | not estimated | **about 49** (1,266 output tokens over 26 calls) |
| Model calls for the run | one per case (518) | 26 (28 routed requests, 23 distinct texts, plus 2 manual classify checks) |
| Rerun | pays again | **0 new calls**, 28 cache hits |

- The usage ledger records the model, so these numbers come from `code/cache/usage.jsonl`, not from an estimate.
- The averages come from the first live run. The second run of the agent made no new model calls at all, because every
  answer was served from the cache.
- Cost in USD is not computed yet, because the `LUNA_PRICE_IN` and `LUNA_PRICE_OUT` values are not set.
- **Not included at the time:** the screenshot reads made by the first version of `code/evidence/image_reader.py` (about 9.5k
  input and 2k output tokens over 8 images, using `gpt-4o`), which called the API directly and so bypassed the ledger. That
  reader was replaced in D-018; screenshots are now read by Sol through the gateway and are recorded.
- Why it is so low: the regex rules decide 490 of 518 requests; since D-020 a model reads those too, but only to confirm, and repeated texts share one cached answer (D-002).

### Verification needed (before relying on these numbers)

- [x] Exact API model IDs, provider and key variable: `gpt-6-luna` and `gpt-6.1-sol` on the OpenAI endpoint, key in
  `OPENAI_API_KEY`.
- [ ] Current pricing from the provider's page, for **input and output** tokens (and reasoning tokens if billed separately).
- [ ] Source and date of the Artificial Analysis figures, and which model they describe.
- [x] A real Luna run over the uncertain texts, to compare measured tokens against the estimate (see above).
- [x] How the API sets reasoning effort: the `reasoning_effort` parameter is accepted. A custom `temperature` is
  **rejected** (see D-007 and `code/ISSUES.md`).

### Consequences for the code (not yet done)

- The single-model LLM module that was first planned (a plain llm module) is replaced by the two-role LLM layer in D-003 and D-004, configured
  through `LLM_BASE_URL`, `LLM_API_KEY`, `LUNA_MODEL` and `SOL_MODEL`. The screenshot reader first had its own client and
  model (`gpt-4o`); it now goes through the same gateway with Sol (D-018).
- The usage ledger must record the model name on every call, because `evaluation/usage_report.md` requires **per-model
  and overall** totals (calls, input and output tokens, estimated cost).
- Keys come from environment variables only and never appear in code, logs or the zip.

---

## D-002: Intent routing, strong versus weak and ambiguous

- **Date:** 2026-10-25
- **Status:** Accepted

### Context

`request_text` decides whether a case is `NOT_APPLICABLE`. The regex rules in `code/classifier.py` are fast, free and
explainable, but they are tuned to the wording in this dataset and can misread negation and soft phrasing.

### Decision

- **Strong intents are decided by regex.** That means a strong deletion verb in any of the six languages with no
  conflicting cue, a marketing opt-out, or a clear non-deletion pattern (copy, correction, invoice, login help).
- **Weak and ambiguous intents go to the AI.** That means a weak cue that only counts next to a scope word (for
  example "gone" or "remove"), a text with no decisive cue, a text that mixes deletion with another request, and any
  text containing negation ("do not close the account").
- If the AI is unavailable, the safe default is `NON_DELETION` with `needs_review`. Wrongly proposing to delete data
  nobody asked to delete is the harmful error. Wrongly skipping a request is safe and reviewable.

### Measured volume

Of 518 requests, 490 are strong and answered by rules. 28 requests (23 distinct texts) are routed to the AI.

### Consequences

- The router is its own small class, separate from the agent that calls the model.
- A rules-versus-AI audit run (the model also asked about strong cases, without overriding them) feeds the
  "failures found" section of the evaluation report.

---

## D-003: Resilience for third-party model calls

- **Date:** 2026-10-25
- **Status:** Accepted

### Decision

| Failure | Action |
|---|---|
| 408, 409, 425, 429, 500, 502, 503, 504, timeouts, connection errors | Retry with exponential backoff and full jitter (up to 4 attempts, delay capped at 30 s). Every failure counts toward the circuit breaker. |
| 400, 401, 403, 404, 422, unsupported parameter, bad key | No retry and no breaker. The run aborts loudly with a configuration error (exit code 2). |
| Output that does not parse into the schema | Not retried (same prompt, same result). Escalate to the next model. |
| Circuit open for a model | Fail fast and move to the next model. |
| Every model unavailable | Deterministic safe default, flagged `needs_review`. Never silently emit `COMPLETE`. |

- **Circuit breaker, per model:** closed, then open after 5 consecutive failures, then half-open after 30 s with a
  single trial call.
- **Fallback chain:** Luna, then Sol, then the deterministic safe default.
- **Concurrency** across cases is bounded. Output assembly stays single-threaded and sorted.

### Rationale

Full jitter avoids synchronized retry storms against a rate-limited API. A breaker stops a failing model from
slowing every case. Aborting on auth and parameter errors avoids flagging hundreds of cases for a problem that is
really a configuration mistake.

### Not done yet

`Retry-After` headers are not honored in the first version.

---

## D-004: Orchestration and agents

- **Date:** 2026-10-25
- **Status:** Accepted

### Decision

- A **LangGraph** state graph orchestrates the per-case flow: intent, then identity, then discovery, then (later)
  state, policy, planner and validator. A non-deletion intent short-circuits to `NOT_APPLICABLE`.
- Each agent has **one responsibility** and is a callable that takes the case state and returns a partial update.
  Only the intent agent (and later the extraction and vision agents) call a model. Identity, discovery, state,
  policy, planner and validator are deterministic.
- **LangChain** provides the chat-model abstraction, messages and structured output. **Retries, fallback, circuit
  breaker, cache and usage ledger are our own**, so they can be shared across both models. LangChain's built-in retry
  and fallback wrappers cannot share a per-model breaker or ledger.
- The code follows SOLID. Collaborators are injected, and one composition root (`bootstrap.py`) is the only place that
  builds concrete objects.

### Honest notes

- "Multi-agent" here means single-responsibility nodes, not models talking to each other. A graph where every node
  calls a model would cost more and drift, and the grader checks ordering and format exactly.
- LangGraph is more machinery than a linear pipeline strictly needs. It is chosen for explicit conditional routing,
  built-in batch concurrency and a clear explanation in the interview.

---

## D-005: Data contracts

- **Date:** 2026-10-25
- **Status:** Accepted

### Decision

- **Pydantic models at the LLM boundary**, so model output is validated before anything uses it (for example
  `IntentVerdict`: intent, confidence, reason).
- **Frozen dataclasses everywhere else** (`IntentDecision`, `DiscoveryResult`, `Step`, `CaseResult`, plus the existing
  `Request`, `Subject` and `EvidenceRecord`, which are reused and not redefined).
- Enums for closed sets (`Intent`, `Source`, `ModelRole`) so a typo cannot become a silent value.

---

## D-006: Terminal CLI as the product surface

- **Date:** 2026-10-25
- **Status:** Accepted

### Decision

A terminal CLI is the way the system is run and inspected:

| Command | Purpose |
|---|---|
| `classify "<text>"` | Show the rules decision, whether it is strong, and who answered (rules, Luna, Sol or fallback). |
| `case <case_id>` | Explain one case: intent, resolved identity, evidence hits by resource, flags. |
| `run [--all or --samples or --cases a,b] [--workers N] [--no-llm] [--output PATH]` | Run the graph over cases and list the cases that need review. With `--all --output output.csv` it validates every row and writes the submission file. |
| `chat [--once "question"]` | Read-only conversation with tools (D-008). |
| `usage-report` | Regenerates `code/evaluation/usage_report.md` from the usage ledger. |
| `status` | Pipeline stages, circuit breaker state per model, and usage and cost by model and overall. |

`--no-llm` keeps the whole tool usable offline. `output.csv` is written only by `run --all --output`, which refuses if any
row fails validation and re-reads the file it wrote. A case the audit cannot decide never reaches the file as `UNDECIDED`:
the planner applies the default in D-018.

---

## D-007: No custom temperature for Luna and Sol

- **Date:** 2026-10-25
- **Status:** Accepted

### Context

The first plan sent `temperature=0` to make classification repeatable. Luna (high effort) and Sol (low effort) are
reasoning models, and the API rejected the request:
`Unsupported parameter: 'temperature' is not supported with this model.` (HTTP 400, `invalid_request_error`).

### Decision

- **Do not send a temperature by default.** `DEFAULT_TEMPERATURE` is `none` in `code/llm/config.py`.
- It can still be set per model, for a model that accepts one: `LUNA_TEMPERATURE=0` or `SOL_TEMPERATURE=0`.
- Repeatability now comes from other things: a schema-validated output (`IntentVerdict`), the cache keyed by model,
  prompt version and text, and the fact that the 490 rule-decided requests are only confirmed by a model, never decided by one (D-020).
- A 400 from the API is a permanent error (D-003). The run stops with exit code 2 and prints a hint, instead of
  flagging hundreds of cases for what is really a configuration problem.

### Consequence

Because there is no temperature control, the same weak text could in principle get a different answer from the model on
a cold cache. The cache fixes that for reruns, and the audit mode in D-002 is how disagreements would be noticed.

---

## D-008: A chat interface where the AI takes on tasks

- **Date:** 2026-10-25
- **Status:** Accepted

### Decision

`python code/cli.py chat` opens a conversation (`chat --once "question"` asks one question). The model, Luna with Sol as
the fallback, reads the message, decides which tool to call, and explains the result in plain language.

| Tool | What it does |
|---|---|
| `classify_request_text` | Intent for any text: regex rules, then the AI for weak and ambiguous wording. |
| `explain_case` | Runs a case through the flow and describes it. |
| `compare_with_sample` | For a labeled sample, which expected resources we found and which we missed. Evaluation only. |
| `resource_evidence` | Activity-log claims and evidence for one resource as of the case's `audit_at`. |
| `deletion_policy` | The 15 policy rules and the available remediation operations. |
| `run_cases` | Run many cases and summarize. |
| `usage_summary` | Model calls, cache hits, tokens and cost. |

### Guardrails

- **Read-only.** The chat cannot edit files, run deletions or write `output.csv`. The deterministic code owns the facts;
  the AI chooses tools and explains. It never produces answer rows.
- **Claims versus state.** Activity-log rows are claims; evidence shows the actual state. The prompt says so.
- Text in tool results, request texts and evidence is treated as data, not instructions.
- The same resilience as everywhere else (D-003): retries, a circuit breaker per model, Luna then Sol.

### What testing found

The first live answers were partly wrong, which is why the last tools exist (see `code/ISSUES.md`, issues 3 and 4):
the model called a retained audit record a leftover and suggested deleting a backup, because it could not see the
policy. It also blamed "missing" resources for a case being incomplete, when those are gaps in our discovery.

---

## D-009: Stage 4, state reconstruction

- **Date:** 2026-10-25
- **Status:** Accepted. Scored against the 32 labeled samples; see the caveat below.

### Decision

Stage 4 is deterministic code (no model) in `code/state.py` and `code/agents/state.py`, in two steps kept apart:

1. `observe(record)` reads one evidence record as a state: `PRESENT`, `ABSENT`, `EXPOSED` (still carries direct
   identifiers), `DEIDENTIFIED` (redacted or anonymized), `ACTIVE` or `DISABLED` (data flows), `RESTORABLE` or
   `RESTORE_BLOCKED` (backups). Rules are small tables keyed by log event, ledger result, or structured field.
2. `reduce_resource(...)` turns all observations of a resource into one state at `audit_at`: the latest moment wins, and
   where observations at that moment disagree (a primary table and a replica in one snapshot) the worst case wins,
   because a leftover copy is still a leftover. The disagreement is flagged as a conflict.

### Rules that matter

- **Claims never set a state.** Activity-log rows are attached as claims only. A `CHECK_RESTORE_ACCESS` row that says
  SUCCESS while the archive "can still be restored" is a restorable archive.
- **Two time questions.** What can we know? An event is known from its own timestamp, a snapshot only from its capture
  time. What can we cite? Only a file captured at or before `audit_at` (inclusive). If the latest observation cannot be
  cited, the most recent citable observation that shows the same state is cited instead.
- **Systems come from the evidence.** The resource-id prefix to system map uses the ledger records inside the evidence.
  The activity log's `target_system` is not used, because it contains decoy routing.
- Identifiers that describe how a record was found (`lookup_key`, `correlation_key`, `subject_reference`) are never
  counted as identifiers the record holds.

### Result on the 32 labeled samples (checked by `code/tests/evaluate_state.py`)

| Measure | Result |
|---|---|
| Resources the labeled answers mention | 111 |
| Found by our evidence search | 77 |
| Observed state agrees with the label | 77 of 77 |
| Cited evidence id agrees with the label | 58 of 58 |
| Found but not in the labeled answer | 1 (`account_6523`, a stale March lookup) |

### Caveats, stated plainly

- **The rules were written while looking at these same 32 samples.** 77 of 77 shows the rules fit the samples. It does
  not show they will hold on the 508 unlabeled cases. Treat the real accuracy as unknown until it is tested on cases
  that were not used to write the rules.
- **34 of the 111 resources are never found, so stage 4 never sees them:** 20 accounts that appear only in one
  activity row, 7 analytics records with no identifiers, 6 audit records with no readable evidence, and 1 support
  record. Linking them needs a grounded rule that does not exist yet.
- Only the latest moment is used. A stale copy in an older snapshot is not reported.
- Which discovered resources are relevant to the current request is stage 6's job, not stage 4's. For example
  `account_6523` is correctly `ABSENT` in March, but it is not the account this request is about.

---

## D-010: Stages 5 and 6, policy and the COMPLETE or INCOMPLETE decision

- **Date:** 2026-10-25
- **Status:** Accepted. Scored against the 32 labeled samples; see the caveats.

### Decision

Both stages are deterministic code (no model). Delegation:

| Piece | Job |
|---|---|
| `PolicyEngine` (`code/policy.py`), `PolicyAgent` | Stage 5. Maps each resource's category to its required treatment from `deletion_rules.csv` (DELETE, REDACT, ANONYMIZE, RETAIN), plus DISABLE for data flows, and to the finding, fix and check operations that go with it. Never looks at the observed state. |
| `Auditor` (`code/audit.py`), `AuditAgent` | Stage 6. Compares the observed state with the requirement and gives each resource a verdict: COMPLIANT, VIOLATION, UNRESOLVED or STALE. Then decides the case status. |

### Rules that matter

- **A retained record is never deleted.** A backup that is still restorable is fixed with `BLOCK_RESTORE` and checked with
  `VERIFY_RESTORE_BLOCKED`. A security audit log that is present is compliant and checked with `VERIFY_RECORD_PRESENT`.
- **Data flows** must be DISABLED. This is not a line in `deletion_rules.csv`; it comes from the requirement that no
  sync, import or restore path can recreate prohibited data.
- **The record's real category comes from what the evidence says it is, not from its resource-id prefix.** A legacy
  contact index entry is a `SECONDARY_IDENTIFIER` and a contact lookup with an email opt-in is a `MARKETING_PROFILE`,
  both always deleted. Treating them as support messages and analytics profiles gave wrong answers.
- **Stale evidence is excluded.** If the last evidence about a resource predates the request, it cannot show the effect
  of the request, so it gets no finding and no check (verdict STALE).
- **Status.** Any violation makes the case INCOMPLETE. No violation and at least one compliant resource makes it
  COMPLETE. When nothing can be assessed the status is UNDECIDED: a case with no evidence is never reported complete.
  A resource the evidence cannot settle is UNRESOLVED and flags the case for review without deciding it.
- **A missing retained record** is a violation, but there is no allowed operation to repair it, so it is flagged for review.

### Result on the 32 labeled samples (`code/tests/evaluate_audit.py`)

| Measure | Result |
|---|---|
| Case status agrees with the label | 28 of 32 |
| Findings correct | 40 of 41 (the other resource was never discovered) |
| Proposed fixes correct | 40 of 41 (same resource) |
| Checks correct on resources we found | 77 of 77 |
| Findings or checks we invented | 0 |

The four status differences are cases where discovery found nothing to assess (`case_01`, `case_02`, `case_04`,
`case_07`), so they are UNDECIDED instead of wrong.

Over all 518 cases (offline, so the 23 weak requests take the safe default): 324 INCOMPLETE, 152 COMPLETE, 33 NOT_APPLICABLE
and 9 UNDECIDED. These are not scored: there are no labels for them.

### Caveats, stated plainly

- **Rules were refined while looking at these samples.** The category error above was found because two samples came out
  wrong. The numbers show fit to the 32 labeled cases, not accuracy on the other 508.
- **Never exercised by any labeled sample:** an expired audit-log retention period, an active legal hold, and a
  retained record that is missing. They are implemented and unit-tested, but nothing shows they match the real answers.
- **The STALE rule rests on one example** (a March lookup of an older account in `case_17`).
- **Was open, now decided in D-018:** what to output when a case is UNDECIDED. The default is COMPLETE, flagged for review,
  chosen from only four labeled cases (three COMPLETE, one INCOMPLETE).
- The 34 resources that discovery never finds (see D-009) are still invisible to both stages.

---

## D-011: Bounded concurrency

- **Date:** 2026-10-25
- **Status:** Accepted and built
- **Solves:** `code/ISSUES.md` issue 6

### Decision

Cases run in parallel, but the number of simultaneous calls is capped.

- **At most 4 calls in flight per model** (`MAX_CONCURRENT_CALLS` in `code/bootstrap.py`, a semaphore inside
  `ResilientInvoker`). The CLI's default `--workers` is 4 to match. Backoff sleeps happen outside the slot, so a waiting
  retry does not block other work.
- **Single-flight:** requests with the same cache key that arrive together make one model call; the rest wait and read the
  cached answer. Identical work is never paid for twice.
- Output assembly stays single-threaded and sorted (D-014).

### Why

An unbounded fan-out of 518 cases invites 429 rate-limit errors, which would open the circuit breaker for no good reason.
A bound keeps the system inside the provider's limits and keeps cost predictable.

### Verified

`check_resilience.py`: 12 simultaneous requests never exceed 3 calls in flight when the limit is 3, and more than one call
really does run at a time. `check_gateway.py`: 8 identical simultaneous requests make exactly one call and 7 cache hits.

---

## D-012: The deterministic policy engine is the source of truth

- **Date:** 2026-10-25
- **Status:** Accepted and built
- **Solves:** issue 9, with D-015

### Decision

Every outcome the grader scores is decided by deterministic code, never by a model: the identity, the observed state, the
required treatment (`code/policy.py`), the verdict and the case status (`code/audit.py`), and later the steps. The model is
used only where the text is fuzzy: whether a request asks for deletion (D-002), reading screenshots, and explaining results
in the chat. A model answer can change which branch of the rules runs, but it cannot change a policy result, a state, a
status or an evidence id.

### Why

The grader checks status, discovered records, cited evidence, policy decisions and exact step order. Those need to be
identical on every run and explainable from `deletion_rules.csv`. A model cannot promise either.

### Consequence

Anything the chat says about policy comes from the `deletion_policy` and `resource_evidence` tools, not from the model's
own knowledge (see issues 3 and 4).

---

## D-013: Dependency-aware remediation planner, then validator, then output generator

- **Date:** 2026-10-25
- **Status:** Accepted and **built** (see D-018).
- **Solves:** issue 8

### Decision

Stage 7 is three deterministic parts in this order:

1. **Planner.** Builds findings, the remediation plan and the verification checks from the audit's assessments. The
   remediation order follows the dependency, which is also the operation order in the problem statement:
   `DISABLE_DATA_FLOW` first, then `BLOCK_RESTORE`, then the record changes (`DELETE_RECORD`, `REDACT_RECORD`,
   `ANONYMIZE_RECORD`), then verification. A flow that is still running, or a backup that can still restore, would undo any
   record change made before it. Where a copy depends on a source record, the copy is processed first.
2. **Validator.** Checks every row before it is written (see D-015).
3. **Output generator.** Writes `output.csv` with exactly one row per `case_id`, sorted, with canonically serialized JSON.

### Ordering key (D-014)

Phase (FINDING, REMEDIATION, VERIFICATION), then the position of the operation in the problem statement's table, then
`system_id`, then `resource_id`, all ascending. Checked against `dataset/sample_output.csv`: all 32 labeled cases (193
steps) are already in exactly this order with consecutive step numbers (for example `case_18`: findings REMAINING,
EXPOSED_PII, ACTIVE_DATA_FLOW; fixes DISABLE, DELETE, REDACT, ANONYMIZE).

### Was open, now decided

What to output when a case is UNDECIDED (no assessable evidence): decided in D-018.

---

## D-014: One canonical order, so order can never change a score

- **Date:** 2026-10-25
- **Status:** Accepted and built for the current stages
- **Solves:** issue 7

### Decision

Because the grader compares arrays in order, every collection that reaches the output has exactly one canonical order that
does not depend on arrival order, worker count, or set and dictionary iteration:

- cases sorted by `case_id`;
- observations reduced with a fixed tie-break, and assessments sorted by `(system_id, resource_id)`;
- steps ordered by the key in D-013.

### Verified

`code/tests/check_determinism.py`: 25 random orderings of the same observations give an identical state; 25 orderings of
the same resources give an identical audit; and 92 cases give identical results when run in original, reversed and shuffled
order, with 1 worker and with 8. The planner and generator must pass the same test before they count as done.

---

## D-015: Guards against AI hallucination

- **Date:** 2026-10-25
- **Status:** Accepted and built. Guard 6, the validator, was built in D-018.
- **Solves:** issue 9

### Decision

1. **The model never writes answer rows** (D-012).
2. **Schema-validated output.** Model answers are parsed into pydantic models; anything that does not parse is discarded
   and the next model is tried.
3. **Escalate on doubt.** A low-confidence answer from the primary model goes to the second model; if the models are
   unavailable the safe default is NON_DELETION with `needs_review` (D-002, D-003).
4. **Chat is read-only and grounded.** It can only call tools. Any case, resource, evidence or activity id in an answer that
   no tool returned in the session, and that the user did not write, is flagged `[unverified]`.
5. **The model is told that claims are not state** and to treat text inside records and requests as data, not instructions.
6. **Validator (built, `code/validator.py`):** every cited id must exist and be captured at or before `audit_at`; step
   numbers, phases and operation and state pairs must be valid; a bad row blocks the file from being written.

### Limits

The guard catches invented ids. It does not catch a wrong explanation that uses real ids; that is why the chat's policy
knowledge comes from tools (issues 3 and 4) and why its answers should be read against `resource_evidence` output.

---

## D-016: Caching, and what the measurement showed

- **Date:** 2026-10-25 (revised the same day after the first measurement showed the provider cache was not working)
- **Status:** Accepted and built; provider prompt caching measured working
- **Solves:** issues 6 and 10

### Decision

| Cache | What it does | State |
|---|---|---|
| Response cache | Disk cache keyed by model, reasoning effort, prompt version, schema, system prompt, text and image hash. A rerun, or the same text in another case, costs nothing. Bumping `PROMPT_VERSION` invalidates it. | Built, working |
| Single-flight | Identical simultaneous requests share one call (D-011). | Built, tested |
| Screenshot extraction cache | One JSON file per screenshot in `code/cache/vision/`, keyed by the image's content hash and the prompt version. | Built |
| Provider prompt cache | The provider reuses a repeated prompt prefix of about 1,024 tokens or more. Prompts are laid out for it: a fixed, long system prompt first and the customer's text last. The ledger records `cached_input_tokens`. | **Working** (measured below) |

### What the first measurement showed

With the original short prompts the provider cached **nothing**: two live chat turns used 800 and 911 input tokens with
`provider_cached = 0`, and the intent prompt was about 380 tokens. Both are below the provider's roughly 1,024-token minimum
for a cacheable prefix.

### What was done about it

The prompts were made long enough, with content that earns its place and not filler:

- **Intent prompt** (`intent-v3`, about 1,250 tokens): definitions, a five-step decision procedure, confidence guidance, 17
  labeled examples (synthetic, none copied from the labeled samples; a test checks that) and a safety rule.
- **Chat prompt** (`chat-v2`, about 1,450 tokens with the tool list): a glossary of the states, verdicts, treatments,
  operations and id shapes the tools print.

A test (`check_agents.py`) protects the layout: both system prompts must stay at least 4,096 characters, contain no
per-request placeholders, and the call must send the system prompt first and the customer's text last.

### What was measured afterwards (live)

| Measurement | Result |
|---|---|
| Three different weak requests, one after another | call 1 cold (0 cached); calls 2 and 3 served **1,105 of about 1,255** input tokens (88%) from the provider cache |
| The final run's 23 intent calls | about **90%** of the input tokens came from the provider cache (the exact figures are in `code/evaluation/usage_report.md`) |
| Chat turns | 1,425 to 1,611 cached of 1,440 to 1,722 input tokens per call (about 90 to 99%) |
| Effect of the longer prompt on results | none: `output.csv` was byte-identical before and after |
| Accuracy of the model on weak-wording labeled samples | 8 of 8 classified `DELETION`, as labeled |

### The trade-off, stated plainly

The longer prompt raised the total input tokens of the final run from 8,766 to about 29,000 (about 3.4 times). The saving is
only real if cached tokens are billed at a discount. **The discount for these models has not been verified, so whether the run
became cheaper overall is not known.** What is known: the number of tokens sent is higher, most of them are served from the
provider cache, the answers did not change, and the response cache still means a rerun makes no calls at all.

### When to revisit

If the discount turns out to be small, shorten the intent prompt again. The cost of trying is one cold run of 23 calls.

---

## D-017: Treating stale data from queues, caches and old copies

- **Date:** 2026-10-25
- **Status:** Accepted for what is built; the last item is proposed only
- **Solves:** issue 5

### Decision

- Claims (activity rows such as `SYNC_RECORD`, `REFRESH_CACHE`, `DELETE_RECORD SUCCESS`) never set a state. Only evidence does.
- The latest observation wins; at the same moment the worst case wins, so a stale replica copy still counts as a leftover
  (D-009). Evidence that predates the request is STALE and gets no finding (D-010).
- A cache entry or a consumer's lag cannot be observed in this data. They are never assumed to be clean: such resources stay
  UNRESOLVED and flag the case for review.
- **Proposed, not built:** a data-flow or cache-refresh claim dated after a deletion claim on the same source would be flagged
  as a recreation risk for review. It needs a rule for linking a sync to its source, and the data does not give one yet.


---

## D-018: Stage 7 as built, the "no evidence" default, and how the discovery gaps were handled

- **Date:** 2026-10-25
- **Status:** Accepted and built. The default in item 1 is a judgement call on very little data: please review it.

### What was built

`code/planner.py` (D-013's planner), `code/validator.py` and `code/output.py`, wired into the graph after the audit. The
planner uses the one canonical order (D-014). The validator rejects a row for a bad status, a wrong phase or state for an
operation, a gap in step numbers, a duplicate, an unknown or late (after `audit_at`) cited id, evidence where none belongs
and a missing citation where one is required, and a wrong order. `python code/main.py run --all --output output.csv`
refuses to write if any row has an error, then re-reads the written file and checks it.

### Decisions

1. **A case with no assessable evidence is written as `COMPLETE`** (with no steps, flagged `needs_review`). Reasoning: a
   violation must be shown with evidence, and with none there is nothing to cite. Of the four labeled cases in this
   situation three were `COMPLETE`. **Caveat:** that is four cases, and the same four labeled answers were what I looked
   at to choose, so this is not an independent check. It is configurable (`Planner(undecided_status=...)`). Five cases in
   the final file use it: `case_33`, `case_39`, `case_55`, `case_61`, `case_63`.
2. **Screenshots are read by the escalation model through the gateway**, not by the first model I tried. `gpt-4o` refused
   one screenshot ("I'm sorry, I can't assist with that"); the escalation model reads all 11. Extractions are cached by the
   image's content hash (`code/cache/vision/`), so a replaced screenshot can never be served a stale extraction. A
   cache-only reader is the default, so offline runs and tests need no key.
3. **Only resource kinds we model count as discovered resources.** Screenshots also show support case and order numbers
   (`CS-3245`, `ORD-20263245`); those are not resources of any system.
4. **A later-captured snapshot is not used to discover resources.** Four labeled audit records exist only in a bundle
   captured two days after `audit_at`, and their rows have no write time. "Last written before `audit_at` and still there
   later" would prove existence, but without a write time it cannot be applied, and using them anyway would break "ignore
   later evidence". The cost is 4 verification steps.
5. **Linking an account that appears only in one activity row, by timing, was measured and not adopted.** The strictest
   rule (link only when exactly one deletion row could belong to the case) was right 8 of 8 on labeled accounts but assigns
   only 37 of 550 cases, because 464 of 550 cases share a `reported_at` with another case. A wrong link would put another
   customer's record on this case, and the problem statement warns against shortcuts that depend on how the data was
   generated.
6. **Records with no identifier inside an export that several cases share are not attributed to a case.** Nothing in the
   data says which case they belong to (12 resources).
7. **Citation:** a state cites the most recent citable observation that shows it. For "the record exists", any citable
   observation of an existing record qualifies (a record seen exposed earlier proves it exists). An "absent" state is never
   proven by an earlier sighting.
8. **Reproducibility is a requirement, and it was checked.** The final `output.csv` came from empty response caches; a rerun
   from the warm cache with 8 workers (not 4) is byte-for-byte identical and makes 0 model calls. A placeholder API key
   with an unreachable endpoint also reproduces it, so a reviewer does not need a real key.

### Result on the 32 labeled samples (code/tests/evaluate_output.py)

Case status 31 of 32; steps we wrote 157 of 157 correct; labeled steps found 157 of 193 (81%); whole array identical in 10
of 32; shared steps in the right order in 32 of 32. Full tables and the failures found are in
`code/evaluation/evaluation_report.md`.

### Caveats, stated plainly

- The rules were refined while looking at these samples, so the figures are a fit, not a held-out estimate. The real
  accuracy on the other 486 requests is unknown and probably lower.
- 34 of 111 labeled resources are never found. The same gaps almost certainly exist in the other cases.


## D-019: A single harness, and a flag (not a guess) for the discovery gaps

- **Date:** 2026-10-25
- **Status:** Accepted and built.

### Context

Checks were spread over 13 scripts, three scorers and by-hand comparisons, so "is everything still right after this
change?" had no single answer. Separately, 34 of 111 labeled resources are never discovered (D-018). Reading the data
again showed why: the unlinked records (a bare `DELETE_RECORD` activity row, a `profile: null` 404 lookup, an analytics
record with every identifier null) carry no email, hash, phone or user id. Nothing in them names a person, and the timing
link is unreliable (in the labeled samples the single deletion event before `reported_at` was the right one in 3 cases, a
wrong one in 2, and absent in 25). A model could only guess, and every wrong guess is a false step.

### Decisions

1. **`python code/main.py harness`** (`code/harness.py`) runs: every `check_*.py` in its own process; the 32 labeled
   samples (status, steps found / right / written, order, validation errors); all 518 cases at 1 and at 8 workers, which must
   give identical rows and a file that passes the format check and is byte-identical across runs; a comparison with the
   committed `output.csv` (the 28 AI-routed intents are skipped, as no model runs); and a scan for key-like strings. It exits
   0 only if all of it passes and needs no key or network. `--quick` skips the full runs.
2. **A saved floor.** `code/evaluation/harness_baseline.json` holds the accepted numbers. A higher-is-better metric
   falling, or a lower-is-better one (wrong steps, errors, mismatches) rising, fails the run. `--update-baseline` accepts the
   current numbers deliberately. The 31 of 32 status result is reported but not a failure by itself; it may not get worse.
3. **`code/agents/gaps.py`** adds a `discovery:unattributed_activity=N` flag when a delete, anonymize or redact action ran
   up to 10 minutes before `reported_at` on a resource kind the case has no record of. It is deterministic, changes no
   status and no step, and fires on 119 of 518 cases, so it means "worth a look", not "wrong".
4. **No model is used for the gaps.** There is nothing in the data for it to read. An AI guess would be unfalsifiable and
   would lower the score; the honest output is a flag for a person.

### Consequences

- Run the harness before every commit. A change that alters `output.csv` must be followed by regenerating it, or the
  submission check fails on purpose.
- The harness cannot say how well the system does on the unlabeled 486 cases: the samples are a fit, not a held-out set.


## D-020: The AI also reads the requests the rules decide (a second reading)

- **Date:** 2026-10-25
- **Status:** Accepted and built.

### Context

D-002 sent only the 28 weak and ambiguous requests (23 distinct texts) to a model, so for 490 of 518 requests the system
was rules alone. The regex is fast and explainable, but it can only know the wordings someone thought of. We wanted the AI
to carry real weight in the intent step without giving it the power to cause a wrong deletion plan.

### Decision

Every request the rules decide is now also read by the primary model, using the same fixed prompt (so it shares the
prompt cache) and **without being told the rules' answer**, so it cannot anchor on it.

| Rules vs primary model | What happens |
|---|---|
| Agree | The rules' answer stands, marked "confirmed by <model>". No flag. |
| Disagree | The escalation model breaks the tie and the case is flagged `intent:needs_review`. The answer changes **only if both models disagree with the rules**; otherwise the rules stand. |
| No model can answer | The rules stand, unflagged. A run with no model gives exactly the answers it always did, so the harness and `--no-llm` are unchanged. |

The weak and ambiguous path (D-002) is unchanged: the model decides, with Sol as escalation, and `NON_DELETION` +
`needs_review` if both are down.

### Measured

On all 368 distinct strong texts (514 requests across the evaluation and sample files) the model agreed with the rules on
every one: 363 deletion and 5 non-deletion, none flagged. A cold full run (empty response cache) made 375 model calls,
about 0.05 USD at the assumed price, 89% of input tokens served from the provider's prompt cache, and `output.csv` was
**byte-identical** to the run before. So today the second reading changes no answer; what it adds is a check that would
catch a wording the rules get wrong, and an independent confirmation recorded per request.

### Consequences

- Cost: one extra cheap call per distinct request text, absorbed by the response cache and prompt cache on every rerun.
- The model still only influences the intent step. Status, steps, order and evidence stay deterministic (D-012).
- `IntentAgent(..., verify=False)` turns the second reading off. The tests cover agree, disagree-and-rules-win,
  both-against-rules, tie-breaker down and no model.


## D-021: An evidence interpreter agent, where the evidence is free text

- **Date:** 2026-10-25
- **Status:** Accepted and built.

### Context

After D-018 to D-020 the pipeline was uncertain about very little: identity resolved for all 485 deletion cases, and 7 cases
were flagged for audit review, 4 resources `UNRESOLVED` and 2 `STALE`. Looking at the raw evidence showed one cause: the
record is free text, not a field the rules know. A screenshot of a data flow said `Mode: Continuous`, `124 events delivered`
(plainly running), but the rules only understand an explicit `enabled` field, so the flow was `UNRESOLVED` and its case
fell to the "no evidence" default `COMPLETE`. That is the one place a reading model is needed.

### Decision

A new `interpret` agent (`code/agents/interpret.py`) runs between `state` and `policy`:

1. **When.** Only for a resource whose state does not answer the question its category asks: a `DATA_FLOW` neither `ACTIVE`
   nor `DISABLED`, or a `BACKUP_ARCHIVE` neither `RESTORABLE` nor `RESTORE_BLOCKED`, and which has a citable record. Anything
   the rules can read never reaches a model.
2. **What it reads.** Only the record that would be cited, with emails, hashes and phone numbers masked
   (`<email>`, `<hash>`, `<phone>`; ISO dates are kept).
3. **What it may answer.** One of the two states that fit the category, or `CANNOT_TELL`, plus a quote.
4. **When the answer is accepted.** Only if both models (primary, then escalation) answer the same state, neither is
   low-confidence, the state fits the category, and every fragment of each quote appears in the record word for word. Any
   failure, an unavailable model or a `CANNOT_TELL` leaves the state exactly as the rules had it.
5. **What changes.** Only the resource's state. Policy, audit, planner and validator are untouched, and the case is flagged
   `state:interpreted_by_ai=N`.

### Measured

Of the 7 flagged cases the interpreter changed one: `case_39` (data flow, both models `ACTIVE`, quotes grounded), which
becomes `INCOMPLETE` with the usual three steps (finding, `DISABLE_DATA_FLOW`, verify). It is the only row of `output.csv`
that changed. The three backups it was shown (`backup_9287`, `backup_4689`, `backup_5237`) were answered `CANNOT_TELL` by both
models, for the right reason: one record only describes the archive, the other two carry a bare `ACTIVE`.

### Consequences

- The model still cannot create a step, an id or a status: it can only supply one of two states, and only on evidence it
  must quote. A hallucinated quote is rejected by a string check, not by trust.
- `bootstrap.build_replay_stack` replays cached model answers with no network. The harness uses it, so the harness now
  reproduces the whole submitted `output.csv`, AI-decided rows included, at no cost.
- Whether `case_39` is really non-compliant is judged from the screenshot alone; no label confirms it.


## D-022: ReAct for the agents that investigate

- **Date:** 2026-10-25
- **Status:** Accepted and built.

### Context

Two agents call a model to find something out, not just to label a text: the evidence interpreter (D-021) and the chat
assistant. The chat used native tool calling, which works but hides the reasoning, and the interpreter was a single shot: it
saw one record and had to answer from it, even when other evidence about the same resource existed.

### Decision

One reusable ReAct engine, `code/llm/react.py`, used by both. Each step the model writes a **Thought**, then an **Action**
(one tool, or `final`), and reads the **Observation**, until it answers. The engine:

- calls the model through the existing `LLMGateway`, so retries with backoff, the circuit breaker, the response cache,
  usage accounting and the Luna-to-Sol choice are unchanged;
- allows only the tools it was given (read-only), refuses a repeated action, turns a tool failure or unknown action into an
  observation instead of an exception, truncates long observations, stops at a step limit, and sends an unparseable final
  answer back for another try;
- keeps the full trace (`Turn`: thought, action, input, observation) and renders it for the CLI and for audit.

**Interpreter** tools: `list_evidence`, `read_record`, `activity_claims`, `what_to_decide`, all limited to the one resource
and to what is known at `audit_at`. The acceptance rules of D-021 are unchanged and now apply to what it read: two models must
agree, neither low-confidence, and each quote must appear word for word in an evidence record the model actually read and
that may be cited at `audit_at` (never in a claim, never in a file captured later). The state and the cited file follow the
record the quote came from, and both models' traces are stored on the result.

**Chat**: the same tools as before, now driven by the engine. The `--trace` flag, or typing /trace in the chat, shows how an answer was reached.
The id guard and the Luna-to-Sol fallback are kept.

### Not ReAct, on purpose

- **Intent classification** has nothing to look up: the whole input is one text, and an action would only repeat the regex's
  own signals back to the model. A single structured answer is cheaper and as good.
- **Screenshot reading** is one image in, one record out.
- **Status, steps and order** are deterministic; no agent loop is allowed near them (D-012).

### Measured

On the four unresolved cases, both models answered in one step: the record already given was enough, which is what the
prompt asks ("use a tool only when it can change your answer"). The investigation path (read another file, refuse a late
file, refuse a quote from a claim, give up after the step limit) is covered by tests with a scripted model, not by a live
case, because no live case needed it. A live chat question (`why is case_39 incomplete?`) took four actions: explain the case,
read the evidence, read the policy, answer, and kept the claim and the evidence apart.

### Consequences

- More calls than a single shot for a question that needs digging, bounded by the step limit (5 for the interpreter,
  6 for the chat) and by the response cache on a rerun.
- The reasoning is stored with every AI-decided state, so an interviewer can read why the model said what it said.


## D-023: Why the tombstone is REDACTED, not anonymized

- **Date:** 2026-10-25
- **Status:** Accepted (rule as supplied; not a choice we made).

`deletion_rules.csv` has `rule_15, ACCOUNT_TOMBSTONE, "Account is deleted and only a stable internal user_id is needed to
preserve references to retained records", REDACT`. `ANONYMIZE` is reserved for `ANALYTICS_PROFILE` and `PRODUCT_TELEMETRY`
(rules 07 and 08). The problem statement says the same: "the stable user reference may remain, but direct identifiers such as
name, email, phone, and email hash must be removed." The labeled sample `case_32` expects `REDACT_RECORD` and
`VERIFY_RECORD_REDACTED` for its tombstone, and we match it.

Anonymizing would defeat the tombstone's purpose. Anonymization means irreversibly unlinking a record from the person; the
tombstone exists so retained invoices and audit logs keep pointing at the same `user_id`. Redaction removes what identifies
the person to a reader (name, email, phone, hash) and keeps the one internal key that identifies nobody outside the company.
Whether that key is itself pseudonymous personal data is a legal question the supplied policy answers by permitting it.


## D-024: Exa web search for the chat only, never for the audit

- **Date:** 2026-10-25
- **Status:** Accepted and built. **Decision by the project owner**: after the proposal (Exa for the chat only, never for
  the audit) was put to them, they confirmed it and asked for it to be recorded as their decision.

### Context

An Exa API key was added to the environment. The audit rules limit where a web search may be used: `AGENTS.md` rule 3 says
predictions use only the participant-facing data under `dataset/`. Beyond the rule, web results would make `output.csv`
depend on a live service, so the same input could give a different file tomorrow and `code.zip` could no longer reproduce
the submission from its cache. And a search query is data sent to a third party, which an audit of personal data must not do
with customer details.

### Decision

1. **Not in the audit.** No intent, identity, evidence, state, policy, audit, plan or validate code uses web search. A test
   fails if any module other than the composition root and the chat tools mentions it.
2. **A chat tool, `web_search`**, for background only: what a regulation says, what a term means. It exists only when
   `EXA_API_KEY` is set, so without a key nothing changes. `code/websearch.py` uses the standard library (no new dependency).
3. **Queries are masked** (emails, hashes and phone numbers become `<email>`, `<hash>`, `<phone>`) before leaving the machine.
4. **Results are labeled** `EXTERNAL ... not evidence about any case`, the chat prompt tells the model never to present them as
   case evidence or let them change a status, and ids that appear only in web text are not counted as verified by the
   hallucination guard (D-015).
5. **Failures are quiet**: a timeout or HTTP error becomes a short "web search unavailable" observation, and the error text
   never contains the key. The key is read from the environment, sent in a header and never logged.

### Measured

A live question ("under GDPR can a company keep invoices after erasure, and does that match our rule set?") took three
actions: web search for the Article 17(3)(b) legal-obligation exception, the policy tool for our rules, then a final answer
that kept them apart and noted that invoices are not a category in the supplied rules.

### Consequences

- Better answers to general questions in the interview or while exploring; no effect on `output.csv`.
- One more outbound dependency, optional and off by default; its calls are not in the usage report, which covers the models.
