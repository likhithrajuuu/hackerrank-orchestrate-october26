"""The per-case flow as a LangGraph state graph (DECISIONS.md D-004).

    intent --deletion--> identity --> discovery --> gaps --> state --> interpret --> policy --> audit --> plan --> validate --> finalize
       |
       +--not deletion--> not_applicable

Without a planner (older, smaller graphs) a deletion case ends as the audit's status with no steps.
"""
from langgraph.graph import END, START, StateGraph

from agents.base import CaseState
from contracts import CaseResult, Intent


def _flags(state: CaseState) -> tuple[str, ...]:
    flags = list(state.get("flags", []))
    if state["intent"].needs_review:
        flags.append("intent:needs_review")
    return tuple(flags)


def _not_applicable(state: CaseState) -> dict:
    return {"result": CaseResult(state["request"].case_id, "NOT_APPLICABLE", flags=_flags(state))}


def _finalize(state: CaseState) -> dict:
    audit, plan = state.get("audit"), state.get("plan")
    flags = list(_flags(state))
    if audit is not None and audit.needs_review:
        flags.append("audit:needs_review")
    for kind in ("error", "warning"):
        if any(p.severity == kind for p in state.get("validation", ())):
            flags.append(f"validation:{kind}")
    status = plan.status if plan else (audit.status if audit else "UNDECIDED")
    return {"result": CaseResult(state["request"].case_id, status, plan.steps if plan else (), tuple(flags))}


def _route(state: CaseState) -> str:
    return "deletion" if state["intent"].intent == Intent.DELETION else "not_applicable"


def build_graph(intent, identity, discovery, state=None, policy=None, audit=None, plan=None, validate=None, gaps=None,
                interpret=None):
    graph = StateGraph(CaseState)
    graph.add_node("intent", intent)
    graph.add_node("identity", identity)
    graph.add_node("discovery", discovery)
    graph.add_node("not_applicable", _not_applicable)
    graph.add_node("finalize", _finalize)
    graph.add_edge(START, "intent")
    graph.add_conditional_edges("intent", _route, {"deletion": "identity", "not_applicable": "not_applicable"})
    graph.add_edge("identity", "discovery")
    previous = "discovery"
    for name, agent in (("gaps", gaps), ("state", state), ("interpret", interpret if state is not None else None), ("policy", policy), ("audit", audit), ("plan", plan), ("validate", validate)):
        if agent is None:
            continue
        graph.add_node(name, agent)
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, "finalize")
    graph.add_edge("not_applicable", END)
    graph.add_edge("finalize", END)
    return graph.compile()
