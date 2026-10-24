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
- **Not included:** the screenshot reads made by `code/evidence/image_reader.py` (about 9.5k input and 2k output tokens
  over 8 images, using `gpt-4o`). That reader calls the API directly, so it bypasses the ledger.
- Why it is so low: 490 of 518 requests are answered by the regex rules and never reach a model (D-002).

### Verification needed (before relying on these numbers)

- [x] Exact API model IDs, provider and key variable: `gpt-6-luna` and `gpt-6.1-sol` on the OpenAI endpoint, key in
  `OPENAI_API_KEY`.
- [ ] Current pricing from the provider's page, for **input and output** tokens (and reasoning tokens if billed separately).
- [ ] Source and date of the Artificial Analysis figures, and which model they describe.
- [x] A real Luna run over the uncertain texts, to compare measured tokens against the estimate (see above).
- [x] How the API sets reasoning effort: the `reasoning_effort` parameter is accepted. A custom `temperature` is
  **rejected** (see D-007 and `code/ISSUES.md`).

### Consequences for the code (not yet done)

- The single-model `llm.py` that was first planned is replaced by the two-role LLM layer in D-003 and D-004, configured
  through `LLM_BASE_URL`, `LLM_API_KEY`, `LUNA_MODEL` and `SOL_MODEL`. `code/evidence/image_reader.py` still hardcodes
  its own client and model (`gpt-4o`), and later moves onto the same gateway with Sol.
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
| `run [--all or --cases a,b] [--workers N] [--no-llm]` | Run the graph over cases and list the cases that need review. |
| `status` | Pipeline stages, circuit breaker state per model, and usage and cost by model and overall. |

`--no-llm` keeps the whole tool usable offline. `output.csv` is only written once the state, policy, planner and
validator stages exist, and a case still `UNDECIDED` is never written as a row.

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
  prompt version and text, and the fact that 490 of 518 requests never reach a model.
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
- **Still open for stage 7:** what to output when a case is UNDECIDED. The final file needs COMPLETE or INCOMPLETE, and
  the labeled UNDECIDED cases split three COMPLETE to one INCOMPLETE, which is too few to choose a default from.
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
- **Status:** Accepted design. **Not built yet.**
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

### Still open

What to output when a case is UNDECIDED (no assessable evidence). The labeled UNDECIDED cases split three COMPLETE to one
INCOMPLETE, which is too few to choose a default from.

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
- **Status:** Accepted. Guards 1 to 5 built; guard 6 belongs to the validator, which is not built yet.
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
6. **Validator (planned):** every cited id must exist and be captured at or before `audit_at`; step numbers, phases and
   operation and state pairs must be valid.

### Limits

The guard catches invented ids. It does not catch a wrong explanation that uses real ids; that is why the chat's policy
knowledge comes from tools (issues 3 and 4) and why its answers should be read against `resource_evidence` output.

---

## D-016: Caching, and what the measurement showed

- **Date:** 2026-10-25
- **Status:** Accepted and built
- **Solves:** issues 6 and 10

### Decision

| Cache | What it does | State |
|---|---|---|
| Response cache | Disk cache keyed by model, reasoning effort, prompt version, schema, system prompt and text. A rerun, or the same text in another case, costs nothing. Bumping `PROMPT_VERSION` invalidates it. | Built, working |
| Single-flight | Identical simultaneous requests share one call (D-011). | Built, tested |
| Image extraction cache | One JSON file per screenshot in `code/cache/images/`. | Built |
| Provider prompt cache | The provider reuses a repeated prompt prefix. Prompts are laid out for it: fixed system prompt and tool list first, the variable text last. The ledger records `cached_input_tokens`. | Enabled but **not triggering** |

### What was measured

Two live chat turns: 800 and 911 input tokens each, `provider_cached = 0`. The provider's automatic prompt cache needs a
shared prefix of about 1,024 tokens or more. The chat prompt is below that and the intent prompt is about 380 tokens. So the
saving that is real today comes from the response cache: the first full run made the model calls, and reruns made none.

### Decision on padding

The prompts are **not** padded to cross the threshold. Extra tokens are paid for on every call, and the saving on a cache hit
is a fraction of that, so padding costs more than it saves at this volume. Revisit if a prompt grows past about 1,024
tokens; the ledger will show the cached share without any further change.

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
