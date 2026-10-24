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

- `code/llm.py` was planned for a single model with a `gpt-4o-mini` default, and `code/evidence/image_reader.py` uses
  `gpt-4o`. Both need two roles instead (for example `LUNA_MODEL` and `SOL_MODEL` environment variables) and an
  escalate-on-failure path.
- The usage ledger must record the model name on every call, because `evaluation/usage_report.md` requires **per-model
  and overall** totals (calls, input and output tokens, estimated cost).
- Keys come from environment variables only and never appear in code, logs or the zip.
