import json
import threading
from collections import defaultdict
from pathlib import Path

TOTAL_KEYS = ("calls", "cache_hits", "input_tokens", "output_tokens", "cost_usd")


class UsageLedger:
    """One JSON line per model call or cache hit. summary() feeds evaluation/usage_report.md."""

    def __init__(self, path: Path):
        self._path, self._lock = path, threading.Lock()

    def record(self, role: str, model: str, version: str, input_tokens: int, output_tokens: int, cached: bool) -> None:
        row = {"role": role, "model": model, "version": version, "input_tokens": input_tokens,
               "output_tokens": output_tokens, "cached": cached}
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    def summary(self, prices: dict[str, tuple[float, float]]) -> dict:
        """prices: model -> (USD per 1M input, USD per 1M output). Returns per-model and overall totals."""
        per = defaultdict(lambda: {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                totals = per[row["model"]]
                if row["cached"]:
                    totals["cache_hits"] += 1
                    continue
                price_in, price_out = prices.get(row["model"], (0.0, 0.0))
                totals["calls"] += 1
                totals["input_tokens"] += row["input_tokens"]
                totals["output_tokens"] += row["output_tokens"]
                totals["cost_usd"] += row["input_tokens"] / 1e6 * price_in + row["output_tokens"] / 1e6 * price_out
        overall = {k: sum(m[k] for m in per.values()) for k in TOTAL_KEYS}
        return {"per_model": dict(per), "overall": overall}
