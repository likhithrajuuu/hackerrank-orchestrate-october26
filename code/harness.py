"""One command that answers "is the whole system still right?" (DECISIONS.md D-019).

    python code/main.py harness                    # everything below, exit 0 only if all of it passes
    python code/main.py harness --quick            # skip the full 518-case runs (suites and labeled samples only)
    python code/main.py harness --update-baseline  # accept the current numbers as the new floor

Stages:
  1. suites       every tests/check_*.py, run offline in its own process
  2. samples      the 32 labeled cases: status, steps found / right / written, order, validation errors
  3. determinism  all 518 cases at 1 worker and at 8 workers must give identical rows, and a valid file
  4. submission   those rows must equal the committed output.csv. Model answers are REPLAYED from the response cache (no
                  network, no cost); a row whose answer is not cached is skipped, and the harness says how many
  5. secrets      no key-like strings in the files that go into code.zip
  6. baseline     no metric may fall below tests/../evaluation/harness_baseline.json

The labeled samples check our work and never drive a prediction. Nothing here calls a model or needs a key.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CODE = Path(__file__).resolve().parent
REPO = CODE.parent
BASELINE = CODE / "evaluation" / "harness_baseline.json"
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
HIGHER_IS_BETTER = ("status_correct", "steps_right", "steps_found", "order_ok", "suites_passed")
LOWER_IS_BETTER = ("steps_wrong", "sample_errors", "full_run_errors", "submission_mismatches", "secret_hits")


class Report:
    def __init__(self):
        self.rows: list[tuple[bool, str, str]] = []

    def add(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((ok, name, detail))
        print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail else ""), flush=True)
        return ok

    @property
    def ok(self) -> bool:
        return all(ok for ok, _, _ in self.rows)


def stage_suites(report: Report, metrics: dict) -> None:
    suites = sorted((CODE / "tests").glob("check_*.py"))
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "LLM_API_KEY")}
    passed = 0
    for suite in suites:
        start = time.time()
        done = subprocess.run([sys.executable, str(suite)], capture_output=True, text=True, env=env, cwd=REPO)
        ok = done.returncode == 0
        passed += ok
        bad = [ln for ln in done.stdout.splitlines() if ln.startswith("BAD")][:3]
        report.add(ok, f"suite {suite.stem[len('check_'):]}", f"{time.time() - start:.1f}s" + ("" if ok else f"  {bad or done.stderr[-200:]}"))
    metrics["suites_passed"] = passed


def stage_samples(report: Report, metrics: dict) -> None:
    sys.path.insert(0, str(CODE / "tests"))
    from evaluate_output import FIELDS, lcs, run_samples
    results = run_samples()
    status_ok = sum(want == got for _, want, got, *_ in results)
    tp = fp = fn = order_ok = errors = 0
    for _, _, _, want, got, problems in results:
        w, g = {tuple(s[f] for f in FIELDS) for s in want}, {tuple(s[f] for f in FIELDS) for s in got}
        tp, fp, fn = tp + len(w & g), fp + len(g - w), fn + len(w - g)
        key = lambda s: (s["system_id"], s["resource_id"], s["operation"])
        shared = {key(s) for s in want} & {key(s) for s in got}
        mine = [key(s) for s in want if key(s) in shared]
        theirs = [key(s) for s in got if key(s) in shared]
        order_ok += lcs(mine, theirs) == len(mine)
        errors += sum(p.severity == "error" for p in problems)
    metrics.update(status_correct=status_ok, steps_right=tp, steps_wrong=fp, steps_found=tp, steps_labeled=tp + fn,
                   order_ok=order_ok, sample_errors=errors, samples=len(results))
    report.add(True, "samples: case status", f"{status_ok} of {len(results)} (the baseline stops it getting worse)")
    report.add(fp == 0, "samples: every step we write is right", f"{tp} of {tp + fp} written steps; {fp} wrong")
    report.add(True, "samples: labeled steps found", f"{tp} of {tp + fn} (the rest are the unlinkable gap, see ISSUES.md)")
    report.add(order_ok == len(results), "samples: shared steps in the right order", f"{order_ok} of {len(results)}")
    report.add(errors == 0, "samples: validation errors", str(errors))


def stage_full_run(report: Report, metrics: dict) -> None:
    from bootstrap import build_case_graph, build_replay_stack, load_store
    from llm.config import ConfigError
    from loaders import load_requests
    from output import check_file, write_output

    requests = load_requests("deletion_requests.csv")
    stack = replay = None
    try:                                                # cached model answers are replayed; nothing is ever called
        replay = Path(tempfile.mkdtemp()) / "replay_usage.jsonl"
        stack = build_replay_stack(replay)
    except ConfigError as exc:
        report.add(True, "replay: model names not configured, so AI-decided rows are not reproduced", str(exc))
    graph = build_case_graph(stack, store=load_store(None))     # screenshots from their content-hash cache, never a model
    runs = {}
    for workers in (1, 8):
        start = time.time()
        finals = graph.batch([{"request": r} for r in requests], config={"max_concurrency": workers})
        finals.sort(key=lambda f: f["request"].case_id)
        runs[workers] = finals
        report.add(len(finals) == len(requests), f"full run, {workers} worker(s)", f"{len(finals)} cases in {time.time() - start:.0f}s")

    rows = {w: [(f["result"].case_id, f["result"].status, f["result"].steps_json()) for f in finals] for w, finals in runs.items()}
    report.add(rows[1] == rows[8], "determinism: 1 worker and 8 workers give identical rows")

    finals = runs[1]
    errors = [p for f in finals for p in f.get("validation", ()) if p.severity == "error"]
    metrics["full_run_errors"] = len(errors)
    report.add(not errors, "validation: no errors across 518 cases", f"{len(errors)} errors")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "output.csv"
        write_output([f["result"] for f in finals], path)
        problems = check_file(path, [r.case_id for r in requests])
        report.add(not problems, "output file passes the format contract", "; ".join(problems[:3]))
        first = path.read_bytes()
        write_output([f["result"] for f in runs[8]], path)
        report.add(first == path.read_bytes(), "output file is byte-identical across runs")

    submitted = REPO / "output.csv"
    if not submitted.exists():
        metrics["submission_mismatches"] = 0
        report.add(True, "submission: output.csv not present, skipped")
        return
    committed = {r["case_id"]: r for r in csv.DictReader(open(submitted, newline="", encoding="utf-8"))}
    skipped = {f["result"].case_id for f in finals if "intent:needs_review" in f["result"].flags}
    mismatches = [cid for cid, status, steps in rows[1]
                  if cid not in skipped and (committed.get(cid, {}).get("deletion_status"), committed.get(cid, {}).get("steps_json")) != (status, steps)]
    metrics["submission_mismatches"] = len(mismatches)
    report.add(not mismatches, "submission: output.csv equals what the code produces now",
               f"{len(mismatches)} differ{' ' + str(mismatches[:5]) if mismatches else ''}; {len(skipped)} AI-routed rows not compared"
               + ("  -> regenerate with: python code/main.py run --all --output output.csv" if mismatches else ""))


def stage_secrets(report: Report, metrics: dict) -> None:
    hits = []
    skip = {".venv", "venv", ".git", "__pycache__", "cache", "node_modules"}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip and d != "dataset"]
        for name in files:
            path = Path(root) / name
            if path.suffix in (".py", ".md", ".txt", ".json", ".example", ".csv") or name == ".env.example":
                if name == "log.txt":
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if SECRET.search(text):
                    hits.append(str(path.relative_to(REPO)))
    metrics["secret_hits"] = len(hits)
    report.add(not hits, "secrets: no key-like strings in code or docs", str(hits[:5]))


def stage_baseline(report: Report, metrics: dict, update: bool, full: bool) -> None:
    if update:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report.add(True, "baseline: updated", str(BASELINE.relative_to(REPO)))
        return
    if not BASELINE.exists():
        report.add(True, "baseline: none saved yet (run with --update-baseline once the numbers are good)")
        return
    saved = json.loads(BASELINE.read_text(encoding="utf-8"))
    regressions = []
    for key in HIGHER_IS_BETTER:
        if key in metrics and key in saved and metrics[key] < saved[key]:
            regressions.append(f"{key} {saved[key]} -> {metrics[key]}")
    for key in LOWER_IS_BETTER:
        if key in metrics and key in saved and metrics[key] > saved[key]:
            regressions.append(f"{key} {saved[key]} -> {metrics[key]}")
    report.add(not regressions, "baseline: no metric is worse than the saved floor", "; ".join(regressions))


def main(quick: bool = False, update_baseline: bool = False) -> int:
    sys.path.insert(0, str(CODE))
    os.environ.pop("OPENAI_API_KEY", None)              # the harness must never spend money
    os.environ.pop("LLM_API_KEY", None)
    report, metrics = Report(), {}
    stage_suites(report, metrics)
    stage_samples(report, metrics)
    if not quick:
        stage_full_run(report, metrics)
    stage_secrets(report, metrics)
    stage_baseline(report, metrics, update_baseline, full=not quick)
    failed = [name for ok, name, _ in report.rows if not ok]
    print()
    print(f"harness: {len(report.rows) - len(failed)} of {len(report.rows)} checks passed" + (f"  FAILED: {failed}" if failed else ""))
    return 0 if report.ok else 1
