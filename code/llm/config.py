import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class ModelRole(str, Enum):
    PRIMARY = "primary"          # Luna
    ESCALATION = "escalation"    # Sol


class ConfigError(RuntimeError):
    pass


# By default no temperature is sent. The configured Luna and Sol models reject a custom temperature ("Unsupported
# parameter: 'temperature'"), so repeatability comes from the schema-constrained output and the disk cache instead.
# A model that does accept one can be given a value, e.g. LUNA_TEMPERATURE=0.
DEFAULT_TEMPERATURE = "none"


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
        raw = env.get(f"{prefix}_TEMPERATURE", DEFAULT_TEMPERATURE).strip().lower()
        return ModelSettings(
            name=env[f"{prefix}_MODEL"],
            reasoning_effort=env.get(f"{prefix}_EFFORT", default_effort) or None,
            temperature=None if raw in ("", "none", "off") else float(raw),   # "none" = do not send a temperature
            price_in_per_m=float(env.get(f"{prefix}_PRICE_IN", 0)),
            price_out_per_m=float(env.get(f"{prefix}_PRICE_OUT", 0)),
        )

    api_key_env = next((name for name in ("LLM_API_KEY", "OPENAI_API_KEY") if env.get(name)), None)
    if api_key_env is None:
        raise ConfigError("no API key: set OPENAI_API_KEY (or LLM_API_KEY) in the shell or in the repo-root .env")
    return LLMSettings(
        base_url=env.get("LLM_BASE_URL") or None,       # optional: unset means the provider's default endpoint
        models={ModelRole.PRIMARY: one("LUNA", "high"), ModelRole.ESCALATION: one("SOL", "low")},
        api_key_env=api_key_env,
    )
