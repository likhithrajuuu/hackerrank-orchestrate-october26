"""Terminal agent (DECISIONS.md D-006).

    python code/cli.py classify "<text>" [--no-llm]
    python code/cli.py case <case_id> [--no-llm]
    python code/cli.py run (--all | --samples | --cases a,b) [--workers N] [--no-llm]
    python code/cli.py status

Exit codes: 0 ok, 1 unexpected failure, 2 configuration or permanent API error.
"""
import argparse
import sys
from collections import Counter, defaultdict
from types import SimpleNamespace

from bootstrap import (USAGE_LOG, build_case_graph, build_intent_agent, build_llm, load_all_requests)
from classifier import classify
from agents.intent import IntentRouter
from llm.config import ConfigError, load_settings
from llm.resilience import PermanentLLMError
from llm.usage import UsageLedger
from loaders import load_requests

STAGES = [
    ("Ingest CSV files", "done"),
    ("[1] Intent: regex for strong, AI for weak and ambiguous", "done"),
    ("[2] Identity resolution", "done"),
    ("[3] Evidence discovery (SQL, JSON, logs, text; PDFs and screenshots not read yet)", "partial"),
    ("[4] State reconstruction", "not started"),
    ("[5] Policy application", "not started"),
    ("[6] Compare state with policy", "not started"),
    ("[7] Step builder, validator, output.csv", "not started"),
]


def _stack(args):
    return None if args.no_llm else build_llm()


def _short(text: str, width: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "..."


def cmd_classify(args) -> int:
    decision = classify(args.text)
    strong = IntentRouter().is_strong(decision)
    print(f"rules   : {decision.intent}  mixed={decision.mixed}  strong={strong}")
    for reason in decision.reasons:
        print(f"          - {_short(reason, 90)}")
    agent = build_intent_agent(_stack(args))
    result = agent({"request": SimpleNamespace(request_text=args.text)})["intent"]
    print(f"answer  : {result.intent.value}  answered by {result.source.value}"
          f"{' (' + result.model + ')' if result.model else ''}  needs_review={result.needs_review}")
    if result.source.value != "rules":
        print(f"reason  : {_short(' | '.join(result.reasons), 200)}")
    return 0


def _print_case(final: dict) -> None:
    request, intent, result = final["request"], final["intent"], final["result"]
    print(f"{request.case_id}  {request.email}  audit_at={request.audit_at:%Y-%m-%d %H:%M}Z  "
          f"reported={request.reported_status}")
    print(f"text    : {_short(request.request_text, 150)}")
    print(f"intent  : {intent.intent.value} by {intent.source.value}  needs_review={intent.needs_review}")
    subject = final.get("subject")
    if subject:
        print(f"identity: {subject.user_id}  {subject.status}  via {subject.via_audit_id or 'current snapshot'}")
        print(f"          emails: {', '.join(sorted(subject.emails))}  | {len(subject.hashes)} hashes")
    discovery = final.get("discovery")
    if discovery:
        by_resource = defaultdict(list)
        for record in discovery.records:
            by_resource[record.resource_id or "(no resource id)"].append(record)
        print(f"evidence: {len(discovery.records)} records across {len(discovery.resource_ids)} resources; "
              f"{len(discovery.unread_files)} files unread")
        for resource in sorted(by_resource)[:20]:
            records = by_resource[resource]
            files = sorted({r.file_id for r in records})
            print(f"          {resource:24s} {len(records):3d} records  {','.join(files[:4])}")
    print(f"result  : {result.status}  flags={list(result.flags)}")


def cmd_case(args) -> int:
    requests = load_all_requests()
    if args.case_id not in requests:
        print(f"unknown case_id {args.case_id!r}", file=sys.stderr)
        return 1
    graph = build_case_graph(_stack(args))
    _print_case(graph.invoke({"request": requests[args.case_id]}))
    return 0


def cmd_run(args) -> int:
    requests = load_all_requests()
    if args.cases:
        wanted = [c.strip() for c in args.cases.split(",") if c.strip()]
        missing = [c for c in wanted if c not in requests]
        if missing:
            print(f"unknown case_id(s): {', '.join(missing)}", file=sys.stderr)
            return 1
        selected = [requests[c] for c in wanted]
    elif args.samples:
        selected = load_requests("sample_deletion_requests.csv")
    else:
        selected = load_requests("deletion_requests.csv")

    stack = _stack(args)
    graph = build_case_graph(stack)
    finals = graph.batch([{"request": r} for r in selected], config={"max_concurrency": args.workers})
    finals.sort(key=lambda f: f["request"].case_id)

    print(f"cases    : {len(finals)}")
    print(f"statuses : {dict(Counter(f['result'].status for f in finals))}")
    print(f"answered : {dict(Counter(f['intent'].source.value for f in finals))}")
    review = [f["request"].case_id for f in finals if f["result"].flags]
    print(f"flagged  : {len(review)} {review[:15]}{' ...' if len(review) > 15 else ''}")
    undecided = sum(f["result"].status == "UNDECIDED" for f in finals)
    if undecided:
        print(f"note     : {undecided} deletion cases are UNDECIDED because the state, policy and planner stages "
              f"are not built yet; output.csv is not written.")
    if stack:
        print("breakers : " + ", ".join(f"{role.value}={inv.breaker.state}" for role, inv in stack.invokers.items()))
    return 0


def cmd_status(args) -> int:
    print("pipeline")
    for name, state in STAGES:
        print(f"  [{'x' if state == 'done' else '~' if state == 'partial' else ' '}] {name}  ({state})")
    try:
        prices = load_settings().prices()
    except ConfigError:
        prices = {}
        print("\nmodel settings: not configured in this shell (costs below use price 0)")
    summary = UsageLedger(USAGE_LOG).summary(prices)
    print("\nmodel usage (from code/cache/usage.jsonl)")
    if not summary["per_model"]:
        print("  no model calls recorded yet")
    for model, t in sorted(summary["per_model"].items()):
        print(f"  {model:24s} calls={t['calls']:4d} cache_hits={t['cache_hits']:4d} "
              f"in={t['input_tokens']:8d} out={t['output_tokens']:7d} cost=${t['cost_usd']:.4f}")
    o = summary["overall"]
    print(f"  {'overall':24s} calls={o['calls']:4d} cache_hits={o['cache_hits']:4d} "
          f"in={o['input_tokens']:8d} out={o['output_tokens']:7d} cost=${o['cost_usd']:.4f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli", description="Ghost Data deletion-audit agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("classify", help="classify one request text")
    p.add_argument("text")
    p.add_argument("--no-llm", action="store_true", help="never call a model; weak intents take the flagged default")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("case", help="explain one case")
    p.add_argument("case_id")
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(func=cmd_case)

    p = sub.add_parser("run", help="run the flow over many cases")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="all evaluation requests (default)")
    group.add_argument("--samples", action="store_true", help="the 32 labeled samples")
    group.add_argument("--cases", help="comma-separated case ids")
    p.add_argument("--workers", type=int, default=4, help="bounded concurrency across cases")
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="pipeline stages and model usage")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, PermanentLLMError) as exc:
        print(f"configuration error: {exc}\nRun with --no-llm to work offline.", file=sys.stderr)
        return 2
    except Exception as exc:                       # noqa: BLE001 - last-resort handler for a CLI
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
