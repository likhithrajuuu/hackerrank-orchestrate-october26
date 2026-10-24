import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class ModelRole(str, Enum):
    PRIMARY = "primary"          # Luna
    ESCALATION = "escalation"    # Sol


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSettings:
    name: str
    reasoning_effort: str | None
    temperature: float | None
    price_in_per_m: float = 0.0     # USD per 1M input tokens; 0 means "not configured", never guessed
    price_out_per_m: float = 0.0    # USD per 1M output tokens


@dataclass(frozen=True)
class LLMSettings:
    base_url: str | None
    models: Mapping[ModelRole, ModelSettings]
    timeout: float = 60.0
    api_key_env: str = "LLM_API_KEY"

    def prices(self) -> dict[str, tuple[float, float]]:
        """model name -> (USD per 1M input, USD per 1M output), for UsageLedger.summary()."""
        return {m.name: (m.price_in_per_m, m.price_out_per_m) for m in self.models.values()}


def load_settings(env: Mapping[str, str] = os.environ) -> LLMSettings:
    """Read everything from environment variables. Nothing is hardcoded and no key is ever stored."""
    def one(prefix: str, default_effort: str) -> ModelSettings:
        if f"{prefix}_MODEL" not in env:
            raise ConfigError(f"{prefix}_MODEL is not set")
        temperature = env.get(f"{prefix}_TEMPERATURE")
        return ModelSettings(
            name=env[f"{prefix}_MODEL"],
            reasoning_effort=env.get(f"{prefix}_EFFORT", default_effort) or None,
            temperature=float(temperature) if temperature else None,   # unset: do not send (reasoning models often reject it)
            price_in_per_m=float(env.get(f"{prefix}_PRICE_IN", 0)),
            price_out_per_m=float(env.get(f"{prefix}_PRICE_OUT", 0)),
        )

    if "LLM_API_KEY" not in env:
        raise ConfigError("LLM_API_KEY is not set")
    return LLMSettings(
        base_url=env.get("LLM_BASE_URL"),
        models={ModelRole.PRIMARY: one("LUNA", "high"), ModelRole.ESCALATION: one("SOL", "low")},
    )
