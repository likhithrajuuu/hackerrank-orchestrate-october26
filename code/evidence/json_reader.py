import json
from pathlib import Path

from .model import flatten

# Only keys that say when something was *observed*. Snapshot/export times (captured_at, exported_at)
# describe the file, and updated_at/created_at describe the record, so none of them are observation times.
TS_KEYS = ("observed_at", "recorded_at")


def read_json(path: Path):
    """Yield (system, table, timestamp_str, flat_fields) for every dict that has a resource_id."""
    yield from _walk(json.loads(Path(path).read_text(encoding="utf-8")))


def _walk(node, ts=None, system=None, table=None, parent_key=None):
    if isinstance(node, dict):
        ts = next((node[k] for k in TS_KEYS if isinstance(node.get(k), str)), ts)
        system = node.get("data_store") or node.get("system") or system
        table = node.get("collection") or table
        if node.get("resource_id"):
            yield system, table, ts, flatten(node)
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                child_system = key if parent_key == "service_exports" else system
                child_table = key if isinstance(value, list) else table
                yield from _walk(value, ts, child_system, child_table, key)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, ts, system, table, parent_key)
