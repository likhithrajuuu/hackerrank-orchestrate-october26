"""Terminal agent (DECISIONS.md D-006).

    python code/cli.py classify "<text>" [--no-llm]
    python code/cli.py case <case_id> [--no-llm]
    python code/cli.py run (--all | --samples | --cases a,b) [--workers N] [--no-llm]
    python code/cli.py chat [--once "question"]
    python code/cli.py status

Exit codes: 0 ok, 1 unexpected failure, 2 configuration or permanent API error.
"""
import argparse
import sys
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from bootstrap import (USAGE_LOG, build_case_graph, build_chat_agent, build_intent_agent, build_llm,
                       load_all_requests, load_env)
from classifier import classify
from agents.intent import IntentRouter
from llm.config import ConfigError, load_settings
from llm.gateway import LLMUnavailable
from llm.resilience import PermanentLLMError
from llm.usage import UsageLedger
from loaders import load_requests
from reporting import render_case, render_run, render_usage, short

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

    stack = _stack(args)
    graph = build_case_graph(stack)
    finals = graph.batch([{"request": r} for r in selected], config={"max_concurrency": args.workers})
    finals.sort(key=lambda f: f["request"].case_id)

    print(render_run(finals))
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
    print(render_usage(summary))
    return 0


CHAT_HELP = """Ask in plain language, for example:
  why is case_18 incomplete?            compare case_14 with its labeled answer
  is "please confirm the profile is gone" a deletion request?
  run the 32 samples                    how much have the models cost so far?
Commands: /help  /reset (forget the conversation)  /exit"""


def cmd_chat(args) -> int:
    agent = build_chat_agent(build_llm())             # chat needs a model; a missing key exits with code 2
    history: list = []
    if args.once:
        print(agent.ask(history, args.once))
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
        history += [HumanMessage(line), AIMessage(reply)]


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

    p = sub.add_parser("chat", help="talk to the audit agent in plain language (needs a model)")
    p.add_argument("--once", metavar="QUESTION", help="ask one question and exit instead of opening a chat")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("status", help="pipeline stages and model usage")
    p.set_defaults(func=cmd_status)
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
