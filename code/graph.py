"""The per-case flow as a LangGraph state graph (DECISIONS.md D-004).

    intent --deletion--> identity --> discovery --> finalize
       |
       +--not deletion--> not_applicable

The state, policy, planner and validator agents will replace `finalize`; until then a deletion case is explicitly
UNDECIDED and is never written to output.csv.
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
    return {"result": CaseResult(state["request"].case_id, "UNDECIDED", flags=_flags(state))}


def _route(state: CaseState) -> str:
    return "deletion" if state["intent"].intent == Intent.DELETION else "not_applicable"


def build_graph(intent, identity, discovery):
    graph = StateGraph(CaseState)
    graph.add_node("intent", intent)
    graph.add_node("identity", identity)
    graph.add_node("discovery", discovery)
    graph.add_node("not_applicable", _not_applicable)
    graph.add_node("finalize", _finalize)
    graph.add_edge(START, "intent")
    graph.add_conditional_edges("intent", _route, {"deletion": "identity", "not_applicable": "not_applicable"})
    graph.add_edge("identity", "discovery")
    graph.add_edge("discovery", "finalize")
    graph.add_edge("not_applicable", END)
    graph.add_edge("finalize", END)
    return graph.compile()
