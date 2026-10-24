from dataclasses import dataclass
from typing import Callable, Generic, Mapping, TypeVar

from pydantic import BaseModel

from .cache import ResponseCache
from .config import LLMSettings, ModelRole
from .resilience import CircuitOpenError, ResilientInvoker, TransientLLMError   # PermanentLLMError propagates on purpose
from .usage import UsageLedger

T = TypeVar("T", bound=BaseModel)

DEFAULT_ORDER = (ModelRole.PRIMARY, ModelRole.ESCALATION)


class LLMUnavailable(RuntimeError):
    """Every model in the chain failed. The caller falls back to a deterministic, flagged default."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    value: T
    role: ModelRole
    model: str
    cached: bool


class LLMGateway:
    """Structured (schema-validated) model calls with a fallback chain, a cache and a usage ledger."""

    def __init__(self, settings: LLMSettings, chats: Mapping[ModelRole, object],
                 invokers: Mapping[ModelRole, ResilientInvoker], cache: ResponseCache, ledger: UsageLedger,
                 method: str = "function_calling"):
        self._settings, self._chats, self._invokers = settings, chats, invokers
        self._cache, self._ledger, self._method = cache, ledger, method

    def structured(self, schema: type[T], system: str, user: str, version: str,
                   order: tuple[ModelRole, ...] = DEFAULT_ORDER,
                   accept: Callable[[T], bool] = lambda value: True) -> LLMResult[T]:
        """Ask order[0]; move to the next model on failure, unparseable output, or an answer `accept` rejects.

        The last model's answer is always returned, so the caller decides how to treat a low-confidence result.
        Permanent errors (bad key, bad parameter) are not caught here: they stop the run.
        """
        errors: list[str] = []
        for role in order:
            final = role == order[-1]
            model = self._settings.models[role]
            key = self._cache.key(model.name, model.reasoning_effort, version, schema.__name__, system, user)

            cached = self._cache.get(key)
            if cached is not None:
                value = schema.model_validate(cached)
                self._ledger.record(role.value, model.name, version, 0, 0, cached=True)
                if final or accept(value):
                    return LLMResult(value, role, model.name, True)
                errors.append(f"{role.value}: cached answer rejected")
                continue

            runnable = self._chats[role].with_structured_output(schema, include_raw=True, method=self._method)
            try:
                out = self._invokers[role].call(lambda: runnable.invoke([("system", system), ("human", user)]))
            except (CircuitOpenError, TransientLLMError) as exc:
                errors.append(f"{role.value}: {type(exc).__name__}")
                continue

            value = out.get("parsed")
            if value is None:
                errors.append(f"{role.value}: unparseable output")
                continue

            usage = getattr(out.get("raw"), "usage_metadata", None) or {}
            self._ledger.record(role.value, model.name, version, usage.get("input_tokens", 0),
                                usage.get("output_tokens", 0), cached=False)
            self._cache.put(key, value.model_dump())      # cached before judging, so a rerun never pays twice
            if final or accept(value):
                return LLMResult(value, role, model.name, False)
            errors.append(f"{role.value}: answer rejected (low confidence)")
        raise LLMUnavailable(errors)
