"""The ReAct engine (llm/react.py), offline with a scripted gateway: no model, no key, no network."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable

from pydantic import BaseModel

from llm.config import ModelRole
from llm.gateway import LLMResult, LLMUnavailable
from llm.react import FINAL, ReActExhausted, ReActLoop, ReActStep, Tool, parse_json, render_trace


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


class Answer(BaseModel):
    value: int


class Scripted:
    """Plays a list of steps, one per call, and records every transcript it was shown."""

    def __init__(self, *steps):
        self.steps, self.seen, self.systems = list(steps), [], []

    def structured(self, schema, system, user, version, order=None, accept=None):
        assert schema is ReActStep and len(order) == 1
        self.seen.append(user)
        self.systems.append(system)
        if not self.steps:
            raise LLMUnavailable(["script ended"])
        thought, action, text = self.steps.pop(0)
        return LLMResult(ReActStep(thought=thought, action=action, input=text), order[0], "stub", False)


calls = []
tools = [Tool("lookup", "input is a key. Returns its value.", lambda k: calls.append(k) or f"value of {k}"),
         Tool("boom", "always fails", lambda _: 1 / 0)]
loop = lambda gw, **kw: ReActLoop(gw, "SYSTEM TEXT", tools, parse_json(Answer), "react-test", **kw)
final = lambda n: ("done", FINAL, json.dumps({"value": n}))

gw = Scripted(final(7))
r = loop(gw).run("task", ModelRole.PRIMARY)
check("an agent that can already answer finishes in one step, with no tool call", r.value.value == 7 and len(r.trace) == 1 and not calls)

gw = Scripted(("need the key", "lookup", "a"), final(3))
r = loop(gw).run("task", ModelRole.PRIMARY)
check("Thought, Action, Observation, then a final answer", [t.action for t in r.trace] == ["lookup", FINAL]
      and r.trace[0].observation == "value of a" and r.trace[0].thought == "need the key" and r.value.value == 3)
check("the observation is shown to the model on the next step", "Observation: value of a" in gw.seen[1] and "Observation" not in gw.seen[0])
check("the system prompt lists the tools and the ReAct protocol", "- lookup:" in gw.systems[0] and "ReAct" in gw.systems[0]
      and gw.systems[0].startswith("SYSTEM TEXT"))
check("the system prompt is identical on every step, so it can be cached", len(set(gw.systems)) == 1)

gw = Scripted(("t", "lookup", "a"), ("again", "lookup", "a"), final(1))
calls.clear()
r = loop(gw).run("task", ModelRole.PRIMARY)
check("repeating an action is refused and the tool is not run twice", calls == ["a"] and "already took" in r.trace[1].observation)

r = loop(Scripted(("t", "delete_everything", ""), final(1))).run("task", ModelRole.PRIMARY)
check("a tool that is not whitelisted is refused", "unknown action" in r.trace[0].observation)

r = loop(Scripted(("t", "boom", ""), final(1))).run("task", ModelRole.PRIMARY)
check("a tool that fails does not end the loop", "tool error: ZeroDivisionError" in r.trace[0].observation and r.value.value == 1)

r = loop(Scripted(("t", FINAL, "not json"), final(2))).run("task", ModelRole.PRIMARY)
check("an invalid final answer is sent back and the model gets another step",
      "not valid" in r.trace[0].observation and r.value.value == 2)

try:
    loop(Scripted(*[("t", "lookup", str(i)) for i in range(10)]), max_steps=3).run("task", ModelRole.PRIMARY)
    check("a model that never answers stops at the step limit", False)
except ReActExhausted as exc:
    check("a model that never answers stops at the step limit", len(exc.trace) == 3)

try:
    loop(Scripted()).run("task", ModelRole.PRIMARY)
    check("an unavailable model surfaces as LLMUnavailable", False)
except LLMUnavailable:
    check("an unavailable model surfaces as LLMUnavailable", True)

long_tool = Tool("big", "huge output", lambda _: "x" * 9000)
r = ReActLoop(Scripted(("t", "big", ""), final(1)), "S", [long_tool], parse_json(Answer), "v").run("task", ModelRole.PRIMARY)
check("a very long observation is truncated", len(r.trace[0].observation) < 3200 and "truncated" in r.trace[0].observation)

check("the trace renders as readable Thought / Action / Observation",
      "Thought: need the key" in render_trace(loop(Scripted(("need the key", "lookup", "q"), final(1))).run("t", ModelRole.PRIMARY).trace))

print("react checks passed")
