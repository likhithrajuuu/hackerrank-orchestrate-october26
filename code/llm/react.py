"""ReAct (reason + act) for the agents that must look things up before they answer (DECISIONS.md D-022).

Each step the model writes a short THOUGHT, then either an ACTION (one read-only tool and its input) or its FINAL answer.
The tool's OBSERVATION is added to the transcript and the model goes again, until it answers or runs out of steps.

    Thought: the record only describes the archive; is there another file that states a restore policy?
    Action: list_evidence
    Observation: evidence_08 log ... evidence_41 json ...
    Thought: ...
    Action: final   Input: {"state": "CANNOT_TELL", ...}

Every model call goes through the LLMGateway, so retries, the circuit breaker, the response cache, usage accounting and the
Luna to Sol choice are the same as everywhere else. The loop adds: a step limit, a repeat guard, tool whitelisting, a final
answer that must parse, and a full trace of Thought/Action/Observation for audit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

from pydantic import BaseModel, Field

from llm.config import ModelRole
from llm.gateway import LLMGateway

T = TypeVar("T")
FINAL = "final"
MAX_OBSERVATION = 3000


class ReActStep(BaseModel):
    """One step as the model must write it."""
    thought: str = Field(max_length=700)       # why the next action, or why it can answer now
    action: str = Field(max_length=60)         # a tool name, or "final"
    input: str = Field(max_length=4000)        # the tool's input, or the final answer


@dataclass(frozen=True)
class Tool:
    name: str
    description: str                           # what it returns and what input it takes; shown to the model
    run: Callable[[str], str]                  # read-only: it may look things up, never change anything


@dataclass(frozen=True)
class Turn:
    thought: str
    action: str
    input: str
    observation: str


@dataclass(frozen=True)
class ReActResult(Generic[T]):
    value: T
    trace: tuple[Turn, ...]
    model: str


class ReActExhausted(RuntimeError):
    """The model did not give a valid final answer within the step limit."""

    def __init__(self, trace: tuple[Turn, ...]):
        super().__init__(f"no final answer after {len(trace)} steps")
        self.trace = trace


PROTOCOL = """HOW YOU WORK (ReAct)
You work in steps. Each step you write:
- thought: one or two sentences on what you know and what you need next.
- action: the name of ONE tool below, or "final" to give your answer.
- input: the tool's input, or, for "final", your answer in the format described above.
After a tool action you will see its observation and take the next step. Use a tool only when it can change your answer;
if you can already answer from what you have been given, answer with "final" in the first step. Never repeat an action
you already took. You have a limited number of steps.

TOOLS
{tools}"""


class ReActLoop(Generic[T]):
    def __init__(self, gateway: LLMGateway, system: str, tools: Sequence[Tool], parse: Callable[[str], T], version: str,
                 max_steps: int = 5):
        """parse turns the text of a "final" input into the agent's answer, raising ValueError (its message is shown to
        the model, which gets another step) when it is not valid."""
        self._gateway, self._parse, self._version, self._max_steps = gateway, parse, version, max_steps
        self._tools = {t.name: t for t in tools}
        listing = "\n".join(f"- {t.name}: {t.description}" for t in tools) or "(none: answer with final)"
        self._system = f"{system}\n\n{PROTOCOL.format(tools=listing)}"

    def run(self, task: str, role: ModelRole) -> ReActResult[T]:
        trace: list[Turn] = []
        seen: set[tuple[str, str]] = set()
        model = ""
        for step_no in range(1, self._max_steps + 1):
            left = self._max_steps - step_no
            reply = self._gateway.structured(ReActStep, self._system, _transcript(task, trace, left), self._version,
                                             order=(role,))
            model, step = reply.model, reply.value
            action, given = step.action.strip(), step.input.strip()
            if action == FINAL:
                try:
                    value = self._parse(given)
                except ValueError as exc:
                    trace.append(Turn(step.thought, action, given, f"your final answer was not valid: {exc}"))
                    continue
                trace.append(Turn(step.thought, action, given, ""))
                return ReActResult(value, tuple(trace), model)
            if action not in self._tools:
                observation = f"unknown action {action!r}. Use one of: {', '.join(self._tools)} or {FINAL}."
            elif (action, given) in seen:
                observation = "you already took this exact action; use its earlier observation or answer with final."
            else:
                seen.add((action, given))
                try:
                    observation = self._tools[action].run(given)
                except Exception as exc:                          # a failing tool must not end the loop
                    observation = f"tool error: {type(exc).__name__}: {exc}"
            trace.append(Turn(step.thought, action, given, _clip(observation)))
        raise ReActExhausted(tuple(trace))


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OBSERVATION else text[:MAX_OBSERVATION] + "\n...(truncated)"


def _transcript(task: str, trace: Sequence[Turn], left: int) -> str:
    """The user message for the next step: the task, then what has happened so far. The system prompt is the fixed prefix."""
    lines = [task]
    for i, t in enumerate(trace, 1):
        lines += ["", f"Step {i}", f"Thought: {t.thought}", f"Action: {t.action}", f"Input: {t.input}"]
        if t.observation:
            lines.append(f"Observation: {t.observation}")
    lines += ["", f"(steps left after this one: {left})" if trace else f"(you have {left + 1} steps)"]
    return "\n".join(lines)


def render_trace(trace: Sequence[Turn]) -> str:
    """Human-readable Thought / Action / Observation, for the CLI and the audit record."""
    out = []
    for i, t in enumerate(trace, 1):
        out.append(f"{i}. Thought: {t.thought}")
        out.append(f"   Action: {t.action}" + (f"  Input: {t.input}" if t.input else ""))
        if t.observation:
            out.append(f"   Observation: {t.observation}")
    return "\n".join(out)


def parse_json(model: type[BaseModel]) -> Callable[[str], BaseModel]:
    """A final-answer parser for agents whose answer is a pydantic model written as JSON."""
    def parse(text: str):
        try:
            return model.model_validate(json.loads(text))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"expected JSON with the fields of {model.__name__}: {str(exc)[:200]}") from exc
    return parse
