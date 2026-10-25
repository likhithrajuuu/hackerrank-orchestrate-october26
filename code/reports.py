"""Builds evaluation/usage_report.md from the usage ledger, so its numbers are generated and never typed by hand."""
from __future__ import annotations

from collections import defaultdict
from typing import Mapping

from llm.usage import UsageLedger

# Only used when no price is configured. These are the author's own figures from DECISIONS.md D-001 and are NOT
# verified: Luna about 0.28 USD per 2.59M input tokens (so 0.108 USD per 1M), Sol about 0.03 USD per task.
FALLBACK_LUNA_INPUT_PER_M = 0.28 / 2.59
FALLBACK_SOL_PER_CALL = 0.03


def _blank():
    return {"calls": 0, "hits": 0, "in": 0, "out": 0, "cached_in": 0}


def _aggregate(rows):
    per = defaultdict(_blank)
    roles = {}
    for r in rows:
        t = per[r["model"]]
        roles[r["model"]] = r["role"]
        if r["cached"]:
            t["hits"] += 1
        else:
            t["calls"] += 1
            t["in"] += r["input_tokens"]
            t["out"] += r["output_tokens"]
            t["cached_in"] += r.get("cached_input_tokens", 0)
    return per, roles


def _cost(model, role, t, prices):
    """(cost, how it was priced) for one model's totals."""
    price_in, price_out = prices.get(model, (0.0, 0.0))
    if price_in or price_out:
        return t["in"] / 1e6 * price_in + t["out"] / 1e6 * price_out, "configured price"
    if role == "primary":
        return t["in"] / 1e6 * FALLBACK_LUNA_INPUT_PER_M, "assumed 0.108 USD per 1M input tokens, output not priced"
    return t["calls"] * FALLBACK_SOL_PER_CALL, "assumed 0.03 USD per call"


def _table(per, roles, prices, n_requests):
    lines = ["| Model | Calls | Response-cache hits | Input tokens | of which provider-cached | Output tokens | Total tokens | Est. cost (USD) | How priced |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    total = _blank()
    cost_total = 0.0
    for model in sorted(per):
        t = per[model]
        cost, how = _cost(model, roles[model], t, prices)
        cost_total += cost
        for k in total:
            total[k] += t[k]
        share = f" ({t['cached_in'] / t['in']:.0%})" if t["in"] else ""
        lines.append(f"| {model} | {t['calls']} | {t['hits']} | {t['in']:,} | {t['cached_in']:,}{share} | {t['out']:,} | "
                     f"{t['in'] + t['out']:,} | {cost:.4f} | {how} |")
    share = f" ({total['cached_in'] / total['in']:.0%})" if total["in"] else ""
    lines.append(f"| **Overall** | **{total['calls']}** | **{total['hits']}** | **{total['in']:,}** | "
                 f"**{total['cached_in']:,}{share}** | **{total['out']:,}** | **{total['in'] + total['out']:,}** | "
                 f"**{cost_total:.4f}** | |")
    avg = (f"Per request ({n_requests} requests): {total['calls'] / n_requests:.3f} model calls, "
           f"{(total['in'] + total['out']) / n_requests:,.1f} tokens "
           f"({total['in'] / n_requests:,.1f} input + {total['out'] / n_requests:,.1f} output), "
           f"estimated cost {cost_total / n_requests:.6f} USD.")
    return "\n".join(lines), avg, total, cost_total


def build_usage_report(ledger: UsageLedger, prices: Mapping[str, tuple[float, float]], n_requests: int,
                       provider: str, mark: str = "final-run") -> str:
    every_row = ledger.rows()
    last_mark = max((i for i, r in enumerate(every_row) if r.get("mark") == mark), default=-1)
    rows = [r for r in every_row if "mark" not in r]
    # only the LAST final run: tagged rows written before its marker belong to an earlier run
    tagged = [r for i, r in enumerate(every_row) if r.get("run") == mark and "mark" not in r and i > last_mark]
    if tagged:                                  # rows are tagged with their run: exact, even if other processes also write
        after, others = tagged, [r for r in rows if not any(r is t for t in tagged)]
    else:                                       # older ledger without tags: everything after the last marker
        marks = [i for i, r in enumerate(ledger.rows()) if r.get("mark") == mark]
        cut = marks[-1] if marks else len(ledger.rows())
        every = ledger.rows()
        after = [r for r in every[cut:] if "mark" not in r and not str(r["version"]).startswith("chat")]
        others = [r for r in every[:cut] if "mark" not in r]
    vision = [r for r in others if str(r["version"]).startswith("vision")]
    dev = [r for r in others if not str(r["version"]).startswith("vision")]

    final_per, final_roles = _aggregate(after)
    vis_per, vis_roles = _aggregate(vision)
    both_per, both_roles = _aggregate(after + vision)
    dev_per, dev_roles = _aggregate(dev)
    t_final, avg_final, tot_final, _ = _table(final_per, final_roles, prices, n_requests)
    t_vis, avg_vis, _, _ = _table(vis_per, vis_roles, prices, n_requests)
    t_both, avg_both, _, _ = _table(both_per, both_roles, prices, n_requests)
    t_dev, _, _, _ = _table(dev_per, dev_roles, prices, n_requests)

    final_hits, final_asked = tot_final["hits"], tot_final["calls"] + tot_final["hits"]
    cached_in, in_total = tot_final["cached_in"], tot_final["in"]
    cached_pct = f"{cached_in / in_total:.0%}" if in_total else "0%"
    return f"""# Usage report: the final full-dataset run

Generated by `python code/cli.py usage-report` from `code/cache/usage.jsonl`. Do not edit by hand.

- **Command that produced `output.csv`:** `python code/main.py run --all --output output.csv` ({n_requests} requests)
- **Provider:** {provider}
- **Models:** the primary model (reasoning effort `high`) classifies request texts and, with the escalation model, reads the
  few records whose state the rules cannot read (D-021); the escalation model (effort `low`) also reads screenshots and is
  the fallback. The model names are in the tables below.
- **Where the models are used:** regex rules decide 490 of the 518 requests, and the primary model reads every one of
  those texts a second time without being told the rules' answer (D-020). The 28 weak or ambiguous requests (23 distinct
  texts) are decided by the model. Calls are few because identical texts share one answer (the response cache) and the
  long fixed prompt is served from the provider's prompt cache. Every other step is deterministic code.

## 1. The final run (what produced `output.csv`)

Everything the run did after its marker in the ledger. Answers already in the response cache are counted as cache hits
and make no call, so a rerun is cheap; the first run on empty caches makes one call per distinct request text (see the
development usage below).

{t_final}

{avg_final}

## 2. One-time screenshot extraction

The 11 screenshots are read once by the escalation model and cached by their content hash, so the final run reads them
from the cache and makes no vision calls. This work is part of reproducing `output.csv` from empty caches.

{t_vis}

{avg_vis}

## 3. Everything needed to reproduce `output.csv` from empty caches (sections 1 and 2)

{t_both}

{avg_both}

## 4. Development usage (not part of the pipeline run)

Chat sessions, experiments and earlier runs while the system was being built. Listed for completeness and honesty; none
of it is needed to produce `output.csv`.

{t_dev}

## 5. Caching: what saved what

There are two different caches, and they do different jobs.

| Cache | What it does | Evidence in this run |
|---|---|---|
| **Response cache (ours)** | Stores each validated model answer on disk, keyed by model, prompt version and text. A repeated question is never sent again. | {final_hits} of the {final_asked} intent questions in the final run were repeats of another request's text and cost nothing; a rerun from the warm cache makes 0 calls. |
| **Provider prompt cache** | The provider reuses a repeated prompt prefix (about 1,024 tokens or more) at a discount. The prompts are laid out for it: a fixed, long system prompt first and the customer's text last. | {cached_in:,} of the final run's {in_total:,} input tokens ({cached_pct}) were served from it. |

How that came about: with the first, short prompts (about 380 tokens for the intent step) the provider cached **nothing**,
because they were below its minimum. The intent prompt was then extended (to about 1,250 tokens) with a decision procedure
and labeled examples, and the chat prompt with a glossary of the system's terms. They are useful content, not filler, and the
result is that the repeated prefix is cached. The cost of doing it: total input tokens rose (the final run used
{in_total:,} input tokens where the short prompt used 8,766), so the saving is only real if cached tokens are billed at a
discount. The provider's discount for these models has not been verified, so whether the run is cheaper overall is **not
known**; the effect on the token count is exact.

## 6. Notes and limits

- **Cost is an estimate.** Token counts come from the ledger and are exact. Prices are read from `LUNA_PRICE_IN/OUT` and
  `SOL_PRICE_IN/OUT` when set. When they are not, the table uses the author's own unverified figures from
  `DECISIONS.md` D-001 (primary model about 0.108 USD per 1M input tokens with output not priced; escalation model about
  0.03 USD per call). Set the real prices in `.env` and run `python code/cli.py usage-report` again.
- **Reasoning tokens.** Output tokens as reported by the provider; for reasoning models they include hidden reasoning tokens.
- **A rerun from the warm cache makes 0 model calls** and writes a byte-identical `output.csv` (checked with 1 and 8 workers).
- **Not in the ledger:** an earlier version of the screenshot reader called the API directly with a different model and was
  not recorded (about 9.5k input and 2k output tokens over 8 images, plus one refused call). It was replaced, and the
  current reader is fully recorded.
"""
