import random
import sys
import tempfile
import threading
import time
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


class RawCached:
    usage_metadata = {"input_tokens": 2000, "output_tokens": 10, "input_token_details": {"cache_read": 1536}}


class StubChat:
    """Stands in for a LangChain chat model. Each behaviour is an Exception, None (unparseable) or a dict."""

    def __init__(self, *behaviours, raw=None, delay=0.0):
        self.behaviours, self.calls, self.schema, self.raw, self.delay = list(behaviours), 0, None, raw, delay

    def with_structured_output(self, schema, include_raw=True, method=None):
        self.schema = schema
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        item = self.behaviours.pop(0) if len(self.behaviours) > 1 else self.behaviours[0]
        if isinstance(item, Exception):
            raise item
        return {"raw": (self.raw or Raw)(), "parsed": None if item is None else self.schema(**item)}


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

    # 9) Provider-side prompt caching is recorded, separately from our own response cache.
    luna = StubChat(GOOD, raw=RawCached)
    gw, ledger = build(luna, StubChat(GOOD), root / "i")
    gw.structured(Probe, "sys", "text", "v1")
    row = ledger.summary(SETTINGS.prices())["per_model"]["luna-test"]
    check("the ledger records prompt tokens the provider served from its own cache",
          row["cached_input_tokens"] == 1536 and row["input_tokens"] == 2000)

    # 10) Single-flight: identical requests arriving together are paid for once.
    luna = StubChat(GOOD, delay=0.05)
    gw, ledger = build(luna, StubChat(GOOD), root / "j")
    results = []
    threads = [threading.Thread(target=lambda: results.append(gw.structured(Probe, "sys", "same text", "v1")))
               for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    row = ledger.summary(SETTINGS.prices())["per_model"]["luna-test"]
    check("8 identical simultaneous requests make exactly one model call", luna.calls == 1 and row["calls"] == 1)
    check("the other 7 are served from the cache and agree", row["cache_hits"] == 7 and len({r.value.answer for r in results}) == 1)

# 10b) Ledger rows carry their run, so a run is exact even when another process writes to the same ledger.
from llm.usage import UsageLedger as _Ledger

shared = Path(tempfile.mkdtemp()) / "shared.jsonl"
run_a, other = _Ledger(shared, run="final-run"), _Ledger(shared)
run_a.record("primary", "m", "intent-v3", 100, 5, cached=False)
other.record("primary", "m", "chat-v2", 900, 20, cached=False)          # a chat session in another terminal
run_a.record("primary", "m", "intent-v3", 100, 5, cached=False)
rows = [r for r in _Ledger(shared).rows() if r.get("run") == "final-run"]
check("only rows tagged with the run are counted as that run, whatever else is written meanwhile",
      len(rows) == 2 and sum(r["input_tokens"] for r in rows) == 200)

# 11) Settings: a copied .env.example has empty values, which must mean "not configured", not crash.
from llm.config import ConfigError, load_settings

empty_prices = {"OPENAI_API_KEY": "unused", "LUNA_MODEL": "m1", "SOL_MODEL": "m2", "LLM_BASE_URL": "", "LUNA_PRICE_IN": "",
                "LUNA_PRICE_OUT": "  ", "SOL_PRICE_IN": "", "SOL_PRICE_OUT": "", "LUNA_EFFORT": "high", "SOL_EFFORT": "low"}
loaded = load_settings(empty_prices)
check("empty price lines in a copied .env.example load as 'not configured'",
      loaded.prices() == {"m1": (0.0, 0.0), "m2": (0.0, 0.0)} and loaded.base_url is None)
check("a real price is read", load_settings({**empty_prices, "LUNA_PRICE_IN": "0.11"}).prices()["m1"] == (0.11, 0.0))
check("the placeholder key path loads (cache-only reproduction)", loaded.api_key_env == "OPENAI_API_KEY")
try:
    load_settings({k: v for k, v in empty_prices.items() if k != "OPENAI_API_KEY"})
    check("no key at all is a configuration error", False)
except ConfigError:
    check("no key at all is a configuration error", True)

print("gateway checks passed")
