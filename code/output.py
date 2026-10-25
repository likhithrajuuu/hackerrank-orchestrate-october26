"""Stage 7c: writes output.csv and checks the file that was actually written."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from contracts import CaseResult

HEADER = ["case_id", "deletion_status", "steps_json"]
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")


def write_output(results: Iterable[CaseResult], path: Path) -> int:
    """One row per case, sorted by case_id. csv.writer quotes the JSON, so quotes and commas are safe."""
    rows = sorted(results, key=lambda r: r.case_id)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for r in rows:
            writer.writerow([r.case_id, r.status, r.steps_json()])
    return len(rows)


def check_file(path: Path, expected_case_ids: Iterable[str]) -> list[str]:
    """Re-reads the written file and checks it against the contract. Returns problems (empty means valid)."""
    problems: list[str] = []
    expected = sorted(expected_case_ids)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = list(reader)
    if header != HEADER:
        problems.append(f"header is {header}, expected {HEADER}")
    ids = [r[0] for r in rows if r]
    if len(rows) != len(expected):
        problems.append(f"{len(rows)} rows, expected {len(expected)}")
    if ids != sorted(ids):
        problems.append("rows are not sorted by case_id")
    if sorted(ids) != expected:
        problems.append(f"case ids differ from the requests: missing {sorted(set(expected) - set(ids))[:5]}, "
                        f"extra {sorted(set(ids) - set(expected))[:5]}")
    for r in rows:
        if len(r) != 3:
            problems.append(f"{r[0] if r else '?'}: row has {len(r)} columns")
            continue
        if r[1] not in STATUSES:
            problems.append(f"{r[0]}: bad status {r[1]!r}")
        try:
            steps = json.loads(r[2])
        except ValueError:
            problems.append(f"{r[0]}: steps_json is not valid JSON")
            continue
        if not isinstance(steps, list):
            problems.append(f"{r[0]}: steps_json is not an array")
        elif r[1] == "NOT_APPLICABLE" and steps:
            problems.append(f"{r[0]}: NOT_APPLICABLE must be []")
    return problems
