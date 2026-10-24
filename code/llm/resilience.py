"""Retries with exponential backoff and full jitter, plus a per-model circuit breaker (DECISIONS.md D-003).

Standard library only. Clock, sleep and RNG are injectable so every behaviour is testable without waiting.
"""
import random
import threading
import time
from dataclasses import dataclass

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RETRYABLE_NAMES = {"APITimeoutError", "APIConnectionError", "Timeout", "ConnectError", "ReadTimeout"}


class CircuitOpenError(RuntimeError):
    """The breaker for this model is open: fail fast instead of calling it."""


class TransientLLMError(RuntimeError):
    """Retries were exhausted on errors that may clear up (rate limit, timeout, 5xx)."""


class PermanentLLMError(RuntimeError):
    """A configuration or request problem (bad key, bad parameter). Retrying cannot help, so the run should stop."""


def is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status in RETRYABLE_STATUS
    return type(exc).__name__ in RETRYABLE_NAMES or isinstance(exc, (TimeoutError, ConnectionError))


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0

    def delay(self, attempt: int, rng: random.Random) -> float:
        """Full jitter: a random wait in [0, min(cap, base * 2^(attempt-1))], so clients do not retry in lockstep."""
        return rng.uniform(0, min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))


class CircuitBreaker:
    """CLOSED -> (N consecutive failures) -> OPEN -> (after reset_timeout) -> HALF_OPEN -> one trial call."""

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0, clock=time.monotonic):
        self.failure_threshold, self.reset_timeout, self._clock = failure_threshold, reset_timeout, clock
        self.state, self._failures, self._opened_at = "CLOSED", 0, 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.state == "OPEN":
                if self._clock() - self._opened_at < self.reset_timeout:
                    return False
                self.state = "HALF_OPEN"       # let a trial call through
            return True

    def record_success(self) -> None:
        with self._lock:
            self.state, self._failures = "CLOSED", 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self.state == "HALF_OPEN" or self._failures >= self.failure_threshold:
                self.state, self._opened_at = "OPEN", self._clock()


class ResilientInvoker:
    """Runs a callable with retries and a breaker. Does not know what the callable does (single responsibility)."""

    def __init__(self, name: str, policy: RetryPolicy = RetryPolicy(), breaker: CircuitBreaker | None = None,
                 sleep=time.sleep, rng: random.Random | None = None, max_concurrent: int = 4):
        self.name, self.policy = name, policy
        self.breaker = breaker or CircuitBreaker()
        self._sleep, self._rng = sleep, rng or random.Random()
        # Bounded concurrency: at most this many calls to this model are in flight, however many workers ask.
        self._slots = threading.BoundedSemaphore(max_concurrent)

    def call(self, fn):
        if not self.breaker.allow():
            raise CircuitOpenError(self.name)
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                with self._slots:                      # backoff sleeps happen outside the slot
                    result = fn()
            except Exception as exc:
                if not is_retryable(exc):
                    raise PermanentLLMError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
                self.breaker.record_failure()
                if attempt == self.policy.max_attempts or not self.breaker.allow():
                    raise TransientLLMError(f"{self.name}: {type(exc).__name__}") from exc
                self._sleep(self.policy.delay(attempt, self._rng))
            else:
                self.breaker.record_success()
                return result
