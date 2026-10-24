import random
import sys
import tempfile
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable

from pydantic import BaseModel

from llm.cache import ResponseCache
from llm.config import LLMSettings, ModelRole, ModelSettings
from llm.gateway import LLMGateway, LLMUnavailable
from llm.resilience import CircuitBreaker, PermanentLLMError, ResilientInvoker, RetryPolicy
from llm.usage import UsageLedger


class Probe(BaseModel):
    answer: Literal["yes", "no"]
    confidence: Literal["high", "low"]


class HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class Raw:
    usage_metadata = {"input_tokens": 100, "output_tokens": 10}


class StubChat:
    """Stands in for a LangChain chat model. Each behaviour is an Exception, None (unparseable) or a dict."""

    def __init__(self, *behaviours):
        self.behaviours, self.calls, self.schema = list(behaviours), 0, None

    def with_structured_output(self, schema, include_raw=True, method=None):
        self.schema = schema
        return self

    def invoke(self, messages):
        self.calls += 1
        item = self.behaviours.pop(0) if len(self.behaviours) > 1 else self.behaviours[0]
        if isinstance(item, Exception):
            raise item
        return {"raw": Raw(), "parsed": None if item is None else self.schema(**item)}


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


SETTINGS = LLMSettings(None, {
    ModelRole.PRIMARY: ModelSettings("luna-test", "high", None, 0.10, 0.40),
    ModelRole.ESCALATION: ModelSettings("sol-test", "low", None, 1.00, 4.00),
})


def build(luna, sol, root, max_attempts=2):
    def invoker(name):
        return ResilientInvoker(name, RetryPolicy(max_attempts=max_attempts), CircuitBreaker(5, 30.0),
                                sleep=lambda s: None, rng=random.Random(0))
    ledger = UsageLedger(root / "usage.jsonl")
    gateway = LLMGateway(SETTINGS, {ModelRole.PRIMARY: luna, ModelRole.ESCALATION: sol},
                         {ModelRole.PRIMARY: invoker("luna"), ModelRole.ESCALATION: invoker("sol")},
                         ResponseCache(root / "cache"), ledger)
    return gateway, ledger


GOOD = {"answer": "yes", "confidence": "high"}
LOW = {"answer": "yes", "confidence": "low"}

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)

    # 1) A transient error on Luna is retried and Luna still answers.
    luna, sol = StubChat(HttpError(503), GOOD), StubChat(GOOD)
    gw, ledger = build(luna, sol, root / "a")
    res = gw.structured(Probe, "sys", "text", "v1")
    check("retry: Luna answers after one 503", res.role == ModelRole.PRIMARY and luna.calls == 2 and sol.calls == 0)

    # 2) A second identical call is a cache hit: no model is called.
    res = gw.structured(Probe, "sys", "text", "v1")
    check("cache: second call is free", res.cached and luna.calls == 2)
    summary = ledger.summary(SETTINGS.prices())
    check("ledger: 1 call and 1 cache hit on Luna",
          summary["per_model"]["luna-test"]["calls"] == 1 and summary["per_model"]["luna-test"]["cache_hits"] == 1)
    check("ledger: cost uses the configured prices",
          abs(summary["per_model"]["luna-test"]["cost_usd"] - (100 / 1e6 * 0.10 + 10 / 1e6 * 0.40)) < 1e-12)

    # 3) Luna down for good: Sol answers.
    luna, sol = StubChat(HttpError(503)), StubChat(GOOD)
    gw, ledger = build(luna, sol, root / "b")
    res = gw.structured(Probe, "sys", "text", "v1")
    check("fallback: Sol answers when Luna keeps failing", res.role == ModelRole.ESCALATION and res.model == "sol-test")

    # 4) Luna returns something unparseable: escalate without retrying the same prompt.
    luna, sol = StubChat(None), StubChat(GOOD)
    gw, _ = build(luna, sol, root / "c")
    res = gw.structured(Probe, "sys", "text", "v1")
    check("unparseable: escalates once, no retry", res.role == ModelRole.ESCALATION and luna.calls == 1)

    # 5) Both models down: LLMUnavailable with both reasons.
    gw, _ = build(StubChat(HttpError(503)), StubChat(HttpError(429)), root / "d")
    try:
        gw.structured(Probe, "sys", "text", "v1")
        check("both down raises LLMUnavailable", False)
    except LLMUnavailable as exc:
        check("both down: LLMUnavailable names both models", len(exc.errors) == 2)

    # 6) Low confidence from Luna escalates to Sol; the last model's answer is returned even if low.
    luna, sol = StubChat(LOW), StubChat(GOOD)
    gw, _ = build(luna, sol, root / "e")
    res = gw.structured(Probe, "sys", "text", "v1", accept=lambda v: v.confidence != "low")
    check("low confidence on Luna escalates to Sol", res.role == ModelRole.ESCALATION)
    gw, _ = build(StubChat(LOW), StubChat(LOW), root / "f")
    res = gw.structured(Probe, "sys", "text", "v1", accept=lambda v: v.confidence != "low")
    check("last model's low-confidence answer is still returned", res.value.confidence == "low")

    # 7) A rejected Luna answer is cached: the rerun does not pay for Luna again.
    luna, sol = StubChat(LOW), StubChat(GOOD)
    gw, _ = build(luna, sol, root / "g")
    gw.structured(Probe, "sys", "text", "v1", accept=lambda v: v.confidence != "low")
    gw.structured(Probe, "sys", "text", "v1", accept=lambda v: v.confidence != "low")
    check("rerun reuses both cached answers", luna.calls == 1 and sol.calls == 1)

    # 8) A permanent error (bad key) stops the run instead of silently falling back.
    gw, _ = build(StubChat(HttpError(401)), StubChat(GOOD), root / "h")
    try:
        gw.structured(Probe, "sys", "text", "v1")
        check("401 propagates", False)
    except PermanentLLMError:
        check("401 propagates and is not swallowed by the fallback", True)

print("gateway checks passed")
