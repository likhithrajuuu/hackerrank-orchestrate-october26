import json
import re
import sqlite3
from pathlib import Path

from .model import flatten


def read_sql(path: Path):
    """Yield (topology, table, row_dict) for every row of every table in a snapshot."""
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"^-- Database topology: (\w+)", text, re.M)
    topology = m.group(1) if m else None
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(text)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        for row in con.execute(f'SELECT * FROM "{table}"'):
            d = dict(row)
            if "record_json" in d:  # topology snapshots keep the record as JSON
                d.update(flatten(json.loads(d.pop("record_json"))))
            yield topology, table, d
    con.close()
