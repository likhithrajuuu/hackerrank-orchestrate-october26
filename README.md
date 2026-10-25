# HackerRank Orchestrate - October 2026

## Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must first determine whether deletion was actually requested. It then returns `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, reports any remaining data or recreation risk, and produces the ordered remediation and verification steps required to finish an authorized deletion safely.

Read [`problem_statement.md`](./problem_statement.md) for the complete challenge contract.

---

## Our solution: setup and run

A deletion-audit agent. For every request it decides whether deletion was asked for, resolves the person (current and
historical emails, hashes, phones), reconstructs what the evidence says about each record at `audit_at`, applies
`deletion_rules.csv`, and writes the ordered findings, remediation and verification steps. The decisions that matter are
made by deterministic code; a model is used only for the fuzzy parts (weak or ambiguous request wording, screenshots, and a
read-only chat). Design and reasons: [`PRD.md`](./PRD.md) and [`DECISIONS.md`](./DECISIONS.md); problems met along the way:
[`code/ISSUES.md`](./code/ISSUES.md); scores and failures: [`code/evaluation/evaluation_report.md`](./code/evaluation/evaluation_report.md);
model usage and cost: [`code/evaluation/usage_report.md`](./code/evaluation/usage_report.md).

`code.zip` also carries `PRD.md`, `DECISIONS.md`, `AGENTS.md`, `problem_statement.md` and `.env.example`, so every link in this
README resolves from inside the archive. It is built from the repository root with:

```bash
zip -r code.zip README.md PRD.md DECISIONS.md AGENTS.md problem_statement.md .env.example code \
  -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store" "*.pyc"
```

### Setup (Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r code/requirements.txt
cp .env.example .env        # then fill in .env (it is git-ignored; never commit a key)
```

`.env` needs `OPENAI_API_KEY`, `LUNA_MODEL` and `SOL_MODEL` (the primary and escalation models). Everything else in
`.env.example` is optional.

**A real key is not needed to reproduce `output.csv`.** The model answers and the screenshot extractions are cached in
`code/cache/`, so a run makes zero model calls. The settings still have to load, so `.env.example` already names the two
models; just put any placeholder after `OPENAI_API_KEY=` (for example `OPENAI_API_KEY=unused`). Checked: with a placeholder
key and an unreachable endpoint the run writes a file byte-identical to the submitted `output.csv`. With no key at all it
stops with a configuration error. A real key is only needed for text or screenshots the cache has not seen, and for `chat`.

### Produce the submission file

```bash
python code/main.py run --all --output output.csv
```

This runs all 518 requests, validates every row, writes `output.csv` at the repository root, then re-reads the file and
checks it (header, 518 rows, sorted, same ids as the requests, valid JSON). It refuses to write if any row is invalid. The
result is deterministic: the same inputs give a byte-identical file, whatever the order or the number of workers.

### Other commands

| Command | What it does |
|---|---|
| `python code/main.py classify "text"` | Is this a deletion request? Shows the regex rules and, for weak or ambiguous wording, the model's answer |
| `python code/main.py case case_18` | Explains one case: intent, person, evidence by resource, observed states, verdicts and the plan |
| `python code/main.py run --samples` | Runs the 32 labeled samples |
| `python code/main.py chat` | Ask about cases in plain language (read-only; needs a key and models). Add `--trace`, or type /trace in the chat, to see the ReAct Thought / Action / Observation behind each answer. With `EXA_API_KEY` set it can also search the public web for background (clearly labeled external; never used by the audit) |
| `python code/main.py usage-report` | Regenerates `code/evaluation/usage_report.md` from the usage ledger |
| `python code/main.py status` | Pipeline stages and model usage |

Add `--no-llm` to `classify`, `case` or `run` to work fully offline (weak or ambiguous requests then take a safe, flagged
default instead of asking a model).

### Tests and scoring

```bash
for t in ingest classifier identity evidence resilience gateway agents chat state policy determinism planner docs react; do
  python code/tests/check_$t.py; done                    # 14 check scripts, none needs a key or network
python code/tests/evaluate_output.py                      # whole rows against the 32 labeled samples
python code/main.py harness --quick              # all suites + the labeled samples in one go (drop --quick for the full 518-case runs)
```

### Layout

| Path | What |
|---|---|
| `code/main.py`, `code/cli.py` | Entry point and commands |
| `code/classifier.py`, `code/agents/intent.py` | Intent: regex for strong cues, a model for weak and ambiguous ones |
| `code/identity.py` | Current and historical identity, as of `audit_at` |
| `code/evidence/` | Readers for SQL, JSON, logs, text, PDFs and screenshots, behind one time-gated store |
| `code/state.py`, `code/policy.py`, `code/audit.py` | Observed state, required treatment, verdict and status |
| `code/planner.py`, `code/validator.py`, `code/output.py` | Ordered steps, row validation, the CSV writer |
| `code/graph.py`, `code/agents/` | The per-case flow (LangGraph) and its single-purpose agents |
| `code/llm/` | Model access: retries with backoff and jitter, a circuit breaker per model, fallback, caching, usage ledger |
| `code/cache/` | Cached model answers and screenshot extractions (no secrets) |
| `code/tests/` | Checks and scorers |

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

Start your AI coding tool from the **repository root** so it automatically reads [`AGENTS.md`](./AGENTS.md) and logs your session turns to `log.txt` in the root directory.

Build your solution in `code/main.py`, or use another language and document its setup and run command clearly in your README.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction row for every request in `dataset/deletion_requests.csv`
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file for submission must be the root-level `output.csv`.


## Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 518 requests |
| `sample_deletion_requests.csv` | 32 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, plain text, or SQL snapshot exports. SQL captures may contain a primary database, regional replicas, a reporting replica, or a failover replica so replication drift can be investigated directly. The `.sql` files are readable as text and executable with SQLite.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

---

## Output

Create `output.csv` with these columns in this exact order:

```text
case_id,deletion_status,steps_json
```

Produce exactly one row per request. `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation actions, and verification checks. Use an empty array (`[]`) for `NOT_APPLICABLE`. Evidence references may be a `file_id`, `activity_id`, or `audit_id`. Determine the resource state at `audit_at` from the available evidence; reported workflow statuses and operation results are claims. Use `dataset/sample_output.csv` as the format reference.

The final file must contain 518 data rows sorted by `case_id` (plus the header row). The grader parses `steps_json` as JSON, so object-key order and whitespace do not matter; array order does. Order findings, remediation, and verification in that phase order. Within a phase, follow the operation order in the problem statement, then sort by `system_id` and `resource_id`.

---

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
code/evaluation/evaluation_report.md
code/evaluation/usage_report.md
```

- `evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases created by you, failures found, and improvements made.
- `usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

**Important:** Place `evaluation_report.md` and `usage_report.md` inside `code/evaluation/` (matching `code/evaluation/main.py` in this starter layout), write your complete evaluation metrics, custom evaluation test cases, and token/cost breakdowns directly inside those Markdown files, and keep prompts in source code or Markdown files. Automated code evaluation ignores raw data files (`.csv`, `.tsv`, `.json`, `.jsonl`, `.sql`, `.log`), so content stored only in those formats will not be visible to the code judge or AI interviewer.

---

## Chat Transcript

This repository includes [`AGENTS.md`](./AGENTS.md), which instructs compatible AI coding tools to append conversation summaries to `log.txt` in the repository root:

| Platform | Path |
| --- | --- |
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

- Always launch your AI coding agent from the repository root (or explicitly ask it to read `./AGENTS.md` first) so every turn is logged to `log.txt` with the required `User Prompt`, `Agent Response Summary`, `Actions`, and `Context` sections.
- Upload `log.txt` directly as your `chat_transcript` submission on HackerRank.
- Do not include API keys, tokens, credentials, or secrets in prompts, code, configuration, or `log.txt`.

---

## Packaging `code.zip` and Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, and `evaluation/` reports |
| `output.csv` | One answer row for each of the 518 requests in `dataset/deletion_requests.csv` |
| `chat_transcript` | The root `log.txt` file generated by your coding agent |
| AI interview | Live explanation of your architecture, approach, and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `README.md` inside `code.zip`.

To package `code.zip` from the repository root without bundling the dataset:

**macOS / Linux:**

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

**Windows (PowerShell):**

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

When packaging `code.zip`:

- Keep your evaluation reports in `code/evaluation/` so they are bundled automatically with `code` (or add `evaluation` to the archive command above if you placed them in a top-level `evaluation/` directory).
- Do **not** include `dataset/`, virtual environments (`.venv`, `venv`, `node_modules`), or build caches (`__pycache__`). On Windows, keep `.venv` at the repository root (outside `code/`) and remove any `code/__pycache__` folders before running `Compress-Archive`.
- Do **not** include nested archives (`.zip`, `.tar`, `.gz`, `.tgz`, `.7z`, `.rar`) inside `code.zip` — submissions containing nested archive files will be rejected.

Before submitting, confirm that:

- `output.csv` has the exact header `case_id,deletion_status,steps_json` and contains all 518 data rows sorted by `case_id`;
- every `steps_json` value is a valid JSON array with consecutive step numbers starting at `1`;
- findings cite valid evidence IDs, and `COMPLETE` verification steps cite evidence proving the current state;
- proposed remediation and future verification steps use an empty `evidence_id` (`""`);
- `code.zip` includes your runnable code, `README.md`, `code/evaluation/evaluation_report.md`, and `code/evaluation/usage_report.md`;
- `log.txt` follows the `AGENTS.md` turn format and is uploaded to the `chat_transcript` slot; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.
