"""Public web search through Exa, for the chat assistant only (DECISIONS.md D-024).

It gives background (what a regulation says, what a term means). It is NOT evidence about a case: the audit uses only the
files under dataset/ (AGENTS.md rule 3), so nothing in the case graph imports this module, and a test keeps it that way.
Queries are masked before they leave the machine, so a customer's email, hash or phone number is never sent.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable, Mapping

from agents.interpret import mask

DEFAULT_URL = "https://api.exa.ai/search"
MAX_QUERY = 300


class WebSearchError(RuntimeError):
    pass


class ExaSearch:
    def __init__(self, api_key: str, url: str = DEFAULT_URL, timeout: float = 15.0,
                 opener: Callable = urllib.request.urlopen):
        self._key, self._url, self._timeout, self._open = api_key, url, timeout, opener

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ExaSearch | None":
        """None when no key is configured, so the chat simply has no web tool. The key is never stored or printed."""
        key = (env.get("EXA_API_KEY") or "").strip().strip('"')
        if not key:
            return None
        return cls(key, (env.get("EXA_API_URL") or DEFAULT_URL).strip().strip('"'))

    def search(self, query: str, limit: int = 4) -> list[dict]:
        safe = mask(query).strip()[:MAX_QUERY]
        if not safe:
            raise WebSearchError("empty query")
        body = json.dumps({"query": safe, "numResults": max(1, min(limit, 8)), "type": "auto",
                           "contents": {"text": {"maxCharacters": 600}}}).encode()
        request = urllib.request.Request(self._url, data=body, method="POST",
                                         headers={"x-api-key": self._key, "Content-Type": "application/json"})
        try:
            with self._open(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise WebSearchError(f"search service answered HTTP {exc.code}") from None     # no body: it could echo the key
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise WebSearchError(f"search service unavailable ({type(exc).__name__})") from None
        return [{"title": r.get("title") or "", "url": r.get("url") or "", "text": " ".join((r.get("text") or "").split())}
                for r in data.get("results", [])][:limit]


def render(results: list[dict], query: str) -> str:
    if not results:
        return f"EXTERNAL web search for {query!r}: no results."
    lines = [f"EXTERNAL web search for {query!r}. This is background from the public web, not evidence about any case:"]
    for r in results:
        lines.append(f"- {r['title']} ({r['url']}): {r['text'][:400]}")
    return "\n".join(lines)
