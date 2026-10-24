import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make code/ importable

from llm.resilience import (CircuitBreaker, CircuitOpenError, PermanentLLMError, ResilientInvoker, RetryPolicy,
                            TransientLLMError, is_retryable)


class HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def check(label, ok):
    print(("OK  " if ok else "BAD "), label)
    assert ok, label


# 1) Which errors are retried.
check("429 and 503 are retryable", is_retryable(HttpError(429)) and is_retryable(HttpError(503)))
check("401, 400, 422 are not", not any(is_retryable(HttpError(s)) for s in (401, 400, 422)))
check("timeouts are retryable", is_retryable(TimeoutError()))

# 2) Full-jitter backoff stays inside [0, min(cap, base * 2^(n-1))].
policy, rng = RetryPolicy(base_delay=1.0, max_delay=30.0), random.Random(7)
check("backoff delays within bounds",
      all(0 <= policy.delay(n, rng) <= min(30.0, 2 ** (n - 1)) for n in range(1, 9) for _ in range(50)))
delays = {round(policy.delay(5, rng), 3) for _ in range(20)}
check("jitter: delays are not all identical", len(delays) > 1)

# 3) Transient errors are retried, with a sleep between attempts.
sleeps, calls = [], {"n": 0}


def flaky():
    calls["n"] += 1
    if calls["n"] < 3:
        raise HttpError(429)
    return "ok"
    


inv = ResilientInvoker("luna", policy=RetryPolicy(max_attempts=4), sleep=sleeps.append, rng=random.Random(1))
check("succeeds after two 429s", inv.call(flaky) == "ok" and calls["n"] == 3 and len(sleeps) == 2)

# 4) Permanent errors are not retried and do not trip the breaker.
calls["n"] = 0


def bad_key():
    calls["n"] += 1
    raise HttpError(401)


inv = ResilientInvoker("luna", sleep=lambda s: None)
try:
    inv.call(bad_key)
    check("401 raises PermanentLLMError", False)
except PermanentLLMError:
    check("401 raises PermanentLLMError after exactly one call", calls["n"] == 1)
check("breaker untouched by a permanent error", inv.breaker.state == "CLOSED")

# 5) The breaker opens after repeated failures, fails fast, then recovers.
clock = Clock()
inv = ResilientInvoker("luna", policy=RetryPolicy(max_attempts=2), breaker=CircuitBreaker(3, 30.0, clock),
                       sleep=lambda s: None)


def always_503():
    raise HttpError(503)


for _ in range(2):
    try:
        inv.call(always_503)
    except TransientLLMError:
        pass
check("breaker is OPEN after the threshold", inv.breaker.state == "OPEN")
try:
    inv.call(lambda: "never runs")
    check("open breaker fails fast", False)
except CircuitOpenError:
    check("open breaker fails fast without calling", True)

clock.now = 31.0
check("after the timeout the breaker lets one trial through", inv.call(lambda: "recovered") == "recovered")
check("a successful trial closes the breaker", inv.breaker.state == "CLOSED")

# 6) A failed trial reopens the breaker immediately.
inv = ResilientInvoker("sol", policy=RetryPolicy(max_attempts=1), breaker=CircuitBreaker(1, 30.0, clock),
                       sleep=lambda s: None)
try:
    inv.call(always_503)
except TransientLLMError:
    pass
clock.now += 31.0
try:
    inv.call(always_503)
except TransientLLMError:
    pass
check("failed half-open trial reopens the breaker", inv.breaker.state == "OPEN")

print("resilience checks passed")
