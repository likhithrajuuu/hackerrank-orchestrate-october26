import hashlib
import json
import os
from pathlib import Path


class ResponseCache:
    """Disk cache of validated model answers, so reruns are free and give identical results."""

    def __init__(self, root: Path):
        self._root = root

    @staticmethod
    def key(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        path = self._root / f"{key}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def put(self, key: str, value: dict) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._root / f"{key}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._root / f"{key}.json")       # atomic, so concurrent workers never read a half-written file
