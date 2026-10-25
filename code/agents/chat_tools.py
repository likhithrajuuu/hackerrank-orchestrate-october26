"""Read-only tools the chat model may call. The tools do the deterministic work; the model only chooses and explains.

Nothing here writes files or changes data (the chat agent cannot edit the project, run deletions, or write output.csv).
"""
from types import SimpleNamespace
from typing import Callable

from langchain_core.tools import StructuredTool

from agents.intent import IntentAgent, IntentRouter
from classifier import classify
from llm.usage import UsageLedger
from loaders import Request, load_activity, load_requests, load_rules
from websearch import WebSearchError, render as render_web
from reporting import (load_sample_answers, render_case, render_run, render_sample_comparison, render_usage)

MAX_TOOL_CHARS = 3500


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TOOL_CHARS else text[:MAX_TOOL_CHARS] + "\n...(truncated)"


def make_tools(requests: dict[str, Request], get_graph: Callable, get_store: Callable, intent_agent: IntentAgent,
               ledger: UsageLedger, prices: dict[str, tuple[float, float]], web=None) -> list[StructuredTool]:
    samples = load_sample_answers()
    activity: dict[str, list] = {}
    for row in load_activity():
        activity.setdefault(row.resource_id, []).append(row)

    def classify_request_text(text: str) -> str:
        """Decide whether a piece of request text authorizes account deletion. Shows the regex rules result and the
        final answer (rules, or the AI model for weak and ambiguous wording)."""
        decision = classify(text)
        result = intent_agent({"request": SimpleNamespace(request_text=text)})["intent"]
        return _clip(f"rules={decision.intent} strong={IntentRouter().is_strong(decision)} "
                     f"reasons={list(decision.reasons)}; final={result.intent.value} answered_by={result.source.value} "
                     f"needs_review={result.needs_review}; reason={' | '.join(result.reasons)}")

    def explain_case(case_id: str) -> str:
        """Run one case (for example case_18) through intent, identity and evidence discovery and describe it: the
        intent decision, the resolved person, and the evidence found by resource."""
        if case_id not in requests:
            return f"unknown case_id {case_id!r}"
        return _clip(render_case(get_graph().invoke({"request": requests[case_id]})))

    def compare_with_sample(case_id: str) -> str:
        """For a labeled sample case (case_01 to case_32 only), compare the resources the labeled answer expects with
        what discovery found: which are FOUND, which are MISSING, and which evidence ids the answer cites."""
        if case_id not in samples:
            return f"{case_id} is not a labeled sample case; labeled answers exist only for case_01 to case_32"
        final = get_graph().invoke({"request": requests[case_id]})
        return _clip(render_sample_comparison(final, samples[case_id]))

    def resource_evidence(case_id: str, resource_id: str) -> str:
        """Show what is known about one resource (for example search_4013) as of the case's audit time: the activity-log
        rows, which are only CLAIMS about what a worker did, and the evidence records (logs, snapshots, documents),
        which show the actual state. Use it to explain why a case is complete or incomplete."""
        if case_id not in requests:
            return f"unknown case_id {case_id!r}"
        audit_at = requests[case_id].audit_at
        store = get_store()
        lines = [f"{resource_id} as of {case_id} audit_at {audit_at:%Y-%m-%d %H:%M}Z"]
        claims = [a for a in activity.get(resource_id, []) if a.event_time <= audit_at]
        lines.append("activity-log CLAIMS:" + ("" if claims else " none"))
        for a in claims:
            lines.append(f"  {a.activity_id} {a.event_time:%m-%d %H:%M} {a.event_type} -> {a.reported_result}: "
                         f"{a.details[:120]}")
        records = store.for_resource(resource_id, audit_at)
        lines.append("EVIDENCE (actual state):" + ("" if records else " none readable"))
        for r in records:
            when = (r.observed_at or r.captured_at).strftime("%m-%d %H:%M")
            body = r.fields.get("raw") or {k: v for k, v in r.fields.items() if v not in (None, "")}
            cite = "citable" if store.citable(r, audit_at) else "NOT citable (file captured after audit_at)"
            lines.append(f"  {r.file_id} {r.kind} {when} [{cite}] {str(body)[:220]}")
        return _clip("\n".join(lines))

    def deletion_policy() -> str:
        """The deletion policy: what must happen to each data category (DELETE, REDACT, ANONYMIZE or RETAIN), with its
        condition. Call this before saying what should be fixed or whether a leftover record is a problem."""
        lines = [f"{r.rule_id} {r.data_category}: {r.required_action} when: {r.condition}" for r in load_rules()]
        lines.append("Remediation operations available: DELETE_RECORD, REDACT_RECORD, ANONYMIZE_RECORD, "
                     "DISABLE_DATA_FLOW, BLOCK_RESTORE. A RETAIN category must NOT be deleted: the record stays and "
                     "is verified present (a retained backup is handled by blocking restores).")
        return _clip("\n".join(lines))

    def run_cases(selection: str) -> str:
        """Run many cases and summarize the statuses. selection is 'all' (518 evaluation requests), 'samples' (the 32
        labeled ones), or comma-separated case ids such as 'case_14,case_18'."""
        sel = selection.strip().lower()
        if sel == "all":
            chosen = load_requests("deletion_requests.csv")
        elif sel == "samples":
            chosen = load_requests("sample_deletion_requests.csv")
        else:
            ids = [c.strip() for c in selection.split(",") if c.strip()]
            missing = [c for c in ids if c not in requests]
            if missing:
                return f"unknown case_id(s): {', '.join(missing)}"
            chosen = [requests[c] for c in ids]
        finals = get_graph().batch([{"request": r} for r in chosen], config={"max_concurrency": 4})
        finals.sort(key=lambda f: f["request"].case_id)
        return _clip(render_run(finals))

    def usage_summary() -> str:
        """Model usage so far: calls, cache hits and tokens per model, and cost when prices are configured."""
        return _clip(render_usage(ledger.summary(prices)))

    def web_search(query: str) -> str:
        """Search the public web for general background, such as what a privacy regulation requires or what a term means.
        The results are EXTERNAL context, never evidence about a case or a customer, and never decide a status. Do not put
        customer details in the query: emails, hashes and phone numbers are removed before it is sent."""
        try:
            return _clip(render_web(web.search(query), query))
        except WebSearchError as exc:
            return f"web search unavailable: {exc}"

    functions = [classify_request_text, explain_case, compare_with_sample, resource_evidence, deletion_policy, run_cases,
                 usage_summary]
    if web is not None:                      # present only when EXA_API_KEY is configured
        functions.append(web_search)
    return [StructuredTool.from_function(fn) for fn in functions]
