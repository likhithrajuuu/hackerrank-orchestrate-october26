import os

from .config import LLMSettings, ModelRole


def build_chat_model(settings: LLMSettings, role: ModelRole):
    """One ChatOpenAI per role, pointed at an OpenAI-compatible endpoint.

    max_retries=0 on purpose: retries, backoff and the circuit breaker are owned by ResilientInvoker, so the SDK
    must not retry underneath it.
    """
    from langchain_openai import ChatOpenAI   # imported lazily so offline tests need no network or key

    m = settings.models[role]
    kwargs = {}
    if m.reasoning_effort:
        kwargs["reasoning_effort"] = m.reasoning_effort
    if m.temperature is not None:             # only sent when explicitly configured
        kwargs["temperature"] = m.temperature
    return ChatOpenAI(model=m.name, base_url=settings.base_url, api_key=os.environ[settings.api_key_env],
                      timeout=settings.timeout, max_retries=0, **kwargs)
