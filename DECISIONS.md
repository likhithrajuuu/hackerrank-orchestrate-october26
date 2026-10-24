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

### Verification needed (before relying on these numbers)

- [ ] Exact API model IDs for Luna and Sol, and the provider, base URL and key variable name.
- [ ] Current pricing from the provider's page, for **input and output** tokens (and reasoning tokens if billed separately).
- [ ] Source and date of the Artificial Analysis figures, and which model they describe.
- [ ] A real Luna run over the 23 uncertain texts, to compare measured tokens and cost against the estimate.
- [ ] How each API sets reasoning effort (parameter name and allowed values).

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
