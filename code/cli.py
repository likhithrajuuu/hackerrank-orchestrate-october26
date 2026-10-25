"""Terminal agent (DECISIONS.md D-006).

    python code/cli.py classify "<text>" [--no-llm]
    python code/cli.py case <case_id> [--no-llm]
    python code/cli.py run (--all | --samples | --cases a,b) [--workers N] [--no-llm] [--output output.csv]
    python code/cli.py chat [--once "question"] [--trace]
    python code/cli.py usage-report
    python code/cli.py status
    python code/cli.py harness [--quick] [--update-baseline]

Exit codes: 0 ok, 1 unexpected failure, 2 configuration or permanent API error.
"""
import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from bootstrap import (USAGE_LOG, build_case_graph, build_chat_agent, build_intent_agent, build_llm,
                       load_all_requests, load_env)
from classifier import classify
from agents.intent import IntentRouter
from llm.react import render_trace
from llm.config import ConfigError, load_settings
from llm.gateway import LLMUnavailable
from llm.resilience import PermanentLLMError
from llm.usage import UsageLedger
from loaders import load_requests
from output import check_file, write_output
from reports import build_usage_report
from reporting import render_case, render_run, render_usage, short

FINAL_RUN_MARK = "final-run"

STAGES = [
    ("Ingest CSV files", "done"),
    ("[1] Intent: regex for strong, AI for weak and ambiguous", "done"),
    ("[2] Identity resolution", "done"),
    ("[3] Evidence discovery (SQL, JSON, logs, text, PDFs, screenshots)", "done"),
    ("[4] State reconstruction", "done"),
    ("[5] Policy application", "done"),
    ("[6] Compare state with policy", "done"),
    ("[7] Planner, validator, output.csv", "done"),
]


def _stack(args):
    return None if args.no_llm else build_llm()


def cmd_classify(args) -> int:
    decision = classify(args.text)
    strong = IntentRouter().is_strong(decision)
    print(f"rules   : {decision.intent}  mixed={decision.mixed}  strong={strong}")
    for reason in decision.reasons:
        print(f"          - {short(reason, 90)}")
    agent = build_intent_agent(_stack(args))
    result = agent({"request": SimpleNamespace(request_text=args.text)})["intent"]
    print(f"answer  : {result.intent.value}  answered by {result.source.value}"
          f"{' (' + result.model + ')' if result.model else ''}  needs_review={result.needs_review}")
    if result.source.value != "rules":
        print(f"reason  : {short(' | '.join(result.reasons), 200)}")
    return 0


def cmd_case(args) -> int:
    requests = load_all_requests()
    if args.case_id not in requests:
        print(f"unknown case_id {args.case_id!r}", file=sys.stderr)
        return 1
    graph = build_case_graph(_stack(args))
    print(render_case(graph.invoke({"request": requests[args.case_id]})))
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

    full = args.output and not (args.cases or args.samples)
    if args.output and not full:
        print("--output writes the submission file, so it needs the full set: use --all (not --cases or --samples)",
              file=sys.stderr)
        return 1

    stack = _stack(args)
    if args.output and stack:
        stack.ledger.mark(FINAL_RUN_MARK)
        stack.ledger.run = FINAL_RUN_MARK          # every row this process writes is tagged, so another process
                                                   # (a chat session in another terminal) cannot be mistaken for the run
    graph = build_case_graph(stack)
    finals = graph.batch([{"request": r} for r in selected], config={"max_concurrency": args.workers})
    finals.sort(key=lambda f: f["request"].case_id)

    print(render_run(finals))
    if stack:
        print("breakers : " + ", ".join(f"{role.value}={inv.breaker.state}" for role, inv in stack.invokers.items()))
    if not args.output:
        return 0

    problems = [p for f in finals for p in f.get("validation", ())]
    errors, warnings = [p for p in problems if p.severity == "error"], [p for p in problems if p.severity == "warning"]
    print(f"validation: {len(errors)} errors, {len(warnings)} warnings")
    for p in (errors or warnings)[:10]:
        print(f"  {p.severity}: {p.message}")
    if errors:
        print("not writing output.csv: fix the errors above first", file=sys.stderr)
        return 1
    path = Path(args.output)
    count = write_output([f["result"] for f in finals], path)
    written = check_file(path, [r.case_id for r in selected])
    if written:
        print("the written file failed its own check:\n  " + "\n  ".join(written[:10]), file=sys.stderr)
        return 1
    print(f"wrote {count} rows to {path}; re-read and checked: header, count, order, ids, JSON")
    return 0


def cmd_harness(args) -> int:
    from harness import main as run_harness
    return run_harness(quick=args.quick, update_baseline=args.update_baseline)


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
    print(render_usage(summary))
    return 0


CHAT_HELP = """Ask in plain language, for example:
  why is case_18 incomplete?            compare case_14 with its labeled answer
  is "please confirm the profile is gone" a deletion request?
  run the 32 samples                    how much have the models cost so far?
Commands: /help  /trace (show the Thought / Action / Observation of the last answer)  /reset (forget the conversation)  /exit"""


def cmd_chat(args) -> int:
    agent = build_chat_agent(build_llm())             # chat needs a model; a missing key exits with code 2
    history: list = []
    if args.once:
        print(agent.ask(history, args.once))
        if args.trace:
            print("\n--- how it got there (ReAct) ---\n" + render_trace(agent.last_trace))
        return 0
    print("Chat with the audit agent (read-only). /help for examples, /exit to leave.")
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/help":
            print(CHAT_HELP)
            continue
        if line == "/trace":
            print(render_trace(agent.last_trace) or "nothing asked yet.")
            continue
        if line == "/reset":
            history.clear()
            print("ai> conversation cleared.")
            continue
        try:
            reply = agent.ask(history, line)
        except LLMUnavailable as exc:
            print(f"ai> the models are unavailable right now ({exc}). The classify, case and run commands still work.")
            continue
        print(f"ai> {reply}")
        if args.trace:
            print("--- how it got there (ReAct) ---\n" + render_trace(agent.last_trace))
        history += [HumanMessage(line), AIMessage(reply)]


def cmd_usage_report(args) -> int:
    try:
        settings = load_settings()
        prices, models = settings.prices(), ", ".join(m.name for m in settings.models.values())
        provider = f"an OpenAI-compatible endpoint ({settings.base_url or 'the provider default'}); models {models}"
    except ConfigError:
        prices, provider = {}, "an OpenAI-compatible endpoint (settings not loaded in this shell)"
    n = len(load_requests("deletion_requests.csv"))
    text = build_usage_report(UsageLedger(USAGE_LOG), prices, n, provider, FINAL_RUN_MARK)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} ({len(text.splitlines())} lines)")
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
    p.add_argument("--output", metavar="PATH", help="write the submission CSV (needs --all); refuses if any row is invalid")
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("chat", help="talk to the audit agent in plain language (needs a model)")
    p.add_argument("--once", metavar="QUESTION", help="ask one question and exit instead of opening a chat")
    p.add_argument("--trace", action="store_true", help="show the Thought / Action / Observation behind each answer")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("usage-report", help="write evaluation/usage_report.md from the usage ledger")
    p.add_argument("--out", default=str(Path(__file__).resolve().parent / "evaluation" / "usage_report.md"))
    p.set_defaults(func=cmd_usage_report)

    p = sub.add_parser("status", help="pipeline stages and model usage")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("harness", help="run every check, the labeled samples and a full offline run; exit 0 only if all pass")
    p.add_argument("--quick", action="store_true", help="skip the two full 518-case runs")
    p.add_argument("--update-baseline", action="store_true", help="save the current numbers as the new floor")
    p.set_defaults(func=cmd_harness)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    load_env()                                     # reads the repo-root .env before any setting is looked at
    try:
        return args.func(args)
    except (ConfigError, PermanentLLMError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        if "temperature" in str(exc).lower():
            print("hint: this model rejects a custom temperature. Set LUNA_TEMPERATURE=none and "
                  "SOL_TEMPERATURE=none.", file=sys.stderr)
        print("Run with --no-llm to work offline.", file=sys.stderr)
        return 2
    except Exception as exc:                       # noqa: BLE001 - last-resort handler for a CLI
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
